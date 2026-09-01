"""Session runtime registry for coding-agent products and embedders."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable

from .services import CodingAgentServices
from .session import CodingAgentSession

CodingAgentSessionFactory = Callable[
    [str],
    CodingAgentSession | Awaitable[CodingAgentSession],
]


class CodingAgentRuntime:
    """Own independently replaceable product Sessions and shared services."""

    def __init__(
        self,
        *,
        services: CodingAgentServices,
        session_factory: CodingAgentSessionFactory,
        sessions: Iterable[CodingAgentSession] = (),
    ) -> None:
        self.services = services
        self._session_factory = session_factory
        self._sessions: dict[str, CodingAgentSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        for session in sessions:
            self.register(session)

    def register(self, session: CodingAgentSession) -> None:
        if self._closed:
            raise RuntimeError("coding-agent runtime is closed")
        if session.session_id in self._sessions:
            raise ValueError(f"coding-agent session already registered: {session.session_id}")
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> CodingAgentSession | None:
        return self._sessions.get(session_id)

    async def get_or_create(self, session_id: str) -> CodingAgentSession:
        if self._closed:
            raise RuntimeError("coding-agent runtime is closed")
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if self._closed:
                raise RuntimeError("coding-agent runtime is closed")
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            created = await self._create_session(session_id)
            if self._closed:
                await created.close()
                raise RuntimeError("coding-agent runtime is closed")
            self._sessions[session_id] = created
            return created

    async def replace(self, session_id: str) -> CodingAgentSession:
        """Create a replacement first, then atomically swap and close the old one."""

        if self._closed:
            raise RuntimeError("coding-agent runtime is closed")
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if self._closed:
                raise RuntimeError("coding-agent runtime is closed")
            replacement = await self._create_session(session_id)
            if self._closed:
                await replacement.close()
                raise RuntimeError("coding-agent runtime is closed")
            previous = self._sessions.get(session_id)
            self._sessions[session_id] = replacement
            if previous is not None:
                await previous.close()
            return replacement

    async def remove(self, session_id: str) -> bool:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            await session.close()
            return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sessions = list(self._sessions.values())
        self._sessions.clear()
        errors: list[Exception] = []
        for session in sessions:
            try:
                await session.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("coding-agent runtime close failed", errors)

    async def _create_session(self, session_id: str) -> CodingAgentSession:
        created_or_awaitable = self._session_factory(session_id)
        created = (
            await created_or_awaitable
            if inspect.isawaitable(created_or_awaitable)
            else created_or_awaitable
        )
        if created.session_id != session_id:
            await created.close()
            raise ValueError("session factory returned a different session_id")
        return created


def create_coding_agent_runtime(
    *,
    services: CodingAgentServices,
    session_factory: CodingAgentSessionFactory,
    sessions: Iterable[CodingAgentSession] = (),
) -> CodingAgentRuntime:
    return CodingAgentRuntime(
        services=services,
        session_factory=session_factory,
        sessions=sessions,
    )


__all__ = [
    "CodingAgentRuntime",
    "CodingAgentSessionFactory",
    "create_coding_agent_runtime",
]
