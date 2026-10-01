"""App-scoped durable Wiki ingestion queue with bounded shutdown recovery."""

from __future__ import annotations

import asyncio
import logging
import math

from .errors import WikiStoreError
from .ingestion import WikiIngestionService
from .models import WikiParseMode
from .store import WikiStore

_logger = logging.getLogger(__name__)


class WikiIngestionWorkerManager:
    def __init__(
        self,
        *,
        store: WikiStore,
        service: WikiIngestionService,
        shutdown_timeout_seconds: float = 5.0,
        source_retention_seconds: float = 7 * 24 * 60 * 60,
        purge_interval_seconds: float = 5 * 60,
    ) -> None:
        if (
            shutdown_timeout_seconds <= 0
            or source_retention_seconds < 0
            or purge_interval_seconds <= 0
            or not math.isfinite(source_retention_seconds)
            or not math.isfinite(purge_interval_seconds)
        ):
            raise ValueError("Wiki worker durations are invalid")
        self._store = store
        self._service = service
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._source_retention_ms = int(source_retention_seconds * 1000)
        self._purge_interval_seconds = purge_interval_seconds
        self._queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        self._queued: set[str] = set()
        self._follow_up_jobs: dict[str, str] = {}
        self._active_source_id: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._purge_task: asyncio.Task[None] | None = None
        self._stop_purge = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            await self._purge_due_sources()
            self._stop_purge = asyncio.Event()
            self._queue = asyncio.Queue()
            self._queued.clear()
            self._follow_up_jobs.clear()
            await self._store.recover_interrupted_derived_jobs()
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
            for source, requested_mode in candidates.values():
                _, job = await self._store.begin_parse_job(
                    source.id, requested_mode=requested_mode,
                )
                self._queued.add(source.id)
                self._queue.put_nowait((source.id, job.id))
            self._task = asyncio.create_task(self._run(), name="wiki-ingestion-worker")
            self._purge_task = asyncio.create_task(
                self._run_purge_loop(),
                name="wiki-source-retention-worker",
            )

    async def enqueue(
        self,
        source_id: str,
        *,
        requested_mode: WikiParseMode | None = None,
    ) -> bool:
        source = await self._store.get_source(source_id)
        if not self._service.can_parse(source):
            raise WikiStoreError("invalid_configuration")
        resolved_mode = self._service.resolve_parse_mode(source, requested_mode)
        async with self._lock:
            if not self.running:
                raise WikiStoreError("invalid_configuration")
            source = await self._store.get_source(source_id)
            if source_id in self._queued:
                if (
                    source_id == self._active_source_id
                    and source.status in {"failed", "parsed"}
                    and source_id not in self._follow_up_jobs
                ):
                    _, job = await self._store.begin_parse_job(
                        source_id, requested_mode=resolved_mode,
                    )
                    self._follow_up_jobs[source_id] = job.id
                    return True
                return False
            _, job = await self._store.begin_parse_job(
                source_id, requested_mode=resolved_mode,
            )
            self._queued.add(source_id)
            self._queue.put_nowait((source_id, job.id))
            return True

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            purge_task = self._purge_task
            self._task = None
            self._purge_task = None
            self._stop_purge.set()
            if task is None and purge_task is None:
                return
            if task is not None:
                self._queue.put_nowait(None)
        try:
            if task is not None:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._shutdown_timeout_seconds,
                )
            if purge_task is not None:
                await asyncio.wait_for(
                    asyncio.shield(purge_task),
                    timeout=self._shutdown_timeout_seconds,
                )
        except TimeoutError:
            pending = tuple(item for item in (task, purge_task) if item is not None)
            for item in pending:
                item.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._queued.clear()
            self._follow_up_jobs.clear()
            self._active_source_id = None

    async def _run(self) -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                self._queue.task_done()
                return
            source_id, job_id = request
            async with self._lock:
                self._active_source_id = source_id
            try:
                job = await self._store.get_job(job_id)
                await self._service.parse_source(
                    source_id, requested_mode=job.requested_mode, queued_job_id=job_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _logger.warning(
                    "Wiki queued parse failed (source_id=%s, job_id=%s, error_type=%s)",
                    source_id, job_id, type(exc).__name__,
                )
                # Admission has already persisted this job. If execution could
                # not start, do not leave it blocking deletion until restart.
                try:
                    await self._store.set_job_status(
                        job_id, "failed", safe_error_code="unknown_error",
                    )
                    await self._store.set_source_status(
                        source_id, "failed", safe_error_code="unknown_error",
                    )
                except Exception:
                    pass
            finally:
                async with self._lock:
                    self._active_source_id = None
                    follow_up_job = self._follow_up_jobs.pop(source_id, None)
                    if follow_up_job is None:
                        self._queued.discard(source_id)
                    else:
                        self._queue.put_nowait((source_id, follow_up_job))
                self._queue.task_done()

    async def _run_purge_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stop_purge.wait(),
                    timeout=self._purge_interval_seconds,
                )
                return
            except TimeoutError:
                await self._purge_due_sources()

    async def _purge_due_sources(self) -> None:
        try:
            await self._store.purge_due_source_files(
                retention_ms=self._source_retention_ms,
            )
        except Exception as exc:
            _logger.warning(
                "Wiki Source retention cleanup failed safely (error_type=%s)",
                type(exc).__name__,
            )


__all__ = ["WikiIngestionWorkerManager"]
