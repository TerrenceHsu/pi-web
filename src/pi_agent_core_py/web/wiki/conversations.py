"""Cross-store lifecycle for trusted Wiki Knowledge conversations."""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import WikiStoreError
from .models import WikiConversation, WikiConversationStatus
from .store import WikiStore


class WikiConversationService:
    """Create a durable Agent Session and Wiki binding as one compensated saga."""

    def __init__(
        self,
        store: WikiStore,
        *,
        session_store: Any,
        file_store: Any = None,
        provider_config_runtime: Any = None,
    ) -> None:
        self._store = store
        self._session_store = session_store
        self._file_store = file_store
        self._provider_config_runtime = provider_config_runtime

    async def create(self, space_id: str, *, title: str) -> WikiConversation:
        space = await self._store.get_space(space_id)
        if space.status != "active":
            raise WikiStoreError("space_conflict")
        session = await self._session_store.create_session(
            title=title,
            metadata={"mode": "knowledge", "wiki_space_id": space.id},
        )
        try:
            if self._provider_config_runtime is not None:
                await self._provider_config_runtime.service.initialize_new_session_binding(
                    session_id=session.id,
                )
            if self._file_store is not None:
                await self._file_store.ensure_session_workspace(session.id)
            return await self._store.create_conversation(
                space.id,
                session_id=session.id,
                title=title,
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._compensate_session(session.id))
            raise
        except BaseException:
            await asyncio.shield(self._compensate_session(session.id))
            raise

    async def _compensate_session(self, session_id: str) -> None:
        file_error: BaseException | None = None
        if self._file_store is not None:
            try:
                await self._file_store.delete_session_files(session_id)
            except BaseException as exc:
                file_error = exc
        session = await self._session_store.get_session(session_id)
        if session is not None:
            await self._session_store.delete_session(session_id)
        if file_error is not None:
            raise file_error

    async def set_status(
        self,
        conversation_id: str,
        status: WikiConversationStatus,
    ) -> WikiConversation:
        conversation = await self._store.get_conversation(conversation_id)
        if status == "active":
            session = await self._session_store.get_session(conversation.session_id)
            if session is None:
                raise WikiStoreError("conversation_conflict")
        if status not in {"active", "archived"}:
            raise WikiStoreError("invalid_conversation")
        return await self._store.set_conversation_status(
            conversation.id,
            status,
        )


__all__ = ["WikiConversationService"]
