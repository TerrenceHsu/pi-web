"""App-scoped durable Wiki ingestion queue with bounded shutdown recovery."""

from __future__ import annotations

import asyncio

from .errors import WikiStoreError
from .ingestion import WikiIngestionService
from .models import WikiParseMode
from .store import WikiStore


class WikiIngestionWorkerManager:
    def __init__(
        self,
        *,
        store: WikiStore,
        service: WikiIngestionService,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        self._store = store
        self._service = service
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._queue: asyncio.Queue[tuple[str, WikiParseMode] | None] = asyncio.Queue()
        self._queued: set[str] = set()
        self._follow_up_modes: dict[str, WikiParseMode] = {}
        self._active_source_id: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            recovered = await self._store.recover_interrupted_parses()
            uploaded = await self._store.list_sources_by_status(("uploaded",))
            candidates = {
                source.id: (source, self._service.resolve_parse_mode(source))
                for source in uploaded
                if self._service.can_parse(source)
            }
            for source_id, recovered_mode in recovered:
                try:
                    source = await self._store.get_source(source_id)
                except WikiStoreError:
                    continue
                if self._service.can_parse(source):
                    try:
                        resolved_mode = self._service.resolve_parse_mode(
                            source,
                            recovered_mode,
                        )
                    except WikiStoreError:
                        # A deployment may switch provider contracts between
                        # restarts. The stale job is already failed safely; an
                        # unsupported mode remains available for explicit retry.
                        continue
                    candidates[source.id] = (source, resolved_mode)
            self._task = asyncio.create_task(self._run(), name="wiki-ingestion-worker")
            for source, requested_mode in candidates.values():
                self._queued.add(source.id)
                self._queue.put_nowait((source.id, requested_mode))

    async def enqueue(
        self,
        source_id: str,
        *,
        requested_mode: WikiParseMode | None = None,
    ) -> bool:
        source = await self._store.get_source(source_id)
        if source.status not in {"uploaded", "failed", "parsed"}:
            raise WikiStoreError("source_conflict")
        if not self._service.can_parse(source):
            raise WikiStoreError("invalid_configuration")
        resolved_mode = self._service.resolve_parse_mode(source, requested_mode)
        async with self._lock:
            if not self.running:
                raise WikiStoreError("invalid_configuration")
            if source_id in self._queued:
                if (
                    source_id == self._active_source_id
                    and source.status in {"failed", "parsed"}
                    and source_id not in self._follow_up_modes
                ):
                    self._follow_up_modes[source_id] = resolved_mode
                    return True
                return False
            self._queued.add(source_id)
            self._queue.put_nowait((source_id, resolved_mode))
            return True

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
            if task is None:
                return
            self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._queued.clear()
            self._follow_up_modes.clear()
            self._active_source_id = None

    async def _run(self) -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                self._queue.task_done()
                return
            source_id, requested_mode = request
            async with self._lock:
                self._active_source_id = source_id
            try:
                await self._service.parse_source(source_id, requested_mode=requested_mode)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                async with self._lock:
                    self._active_source_id = None
                    follow_up_mode = self._follow_up_modes.pop(source_id, None)
                    if follow_up_mode is None:
                        self._queued.discard(source_id)
                    else:
                        self._queue.put_nowait((source_id, follow_up_mode))
                self._queue.task_done()


__all__ = ["WikiIngestionWorkerManager"]
