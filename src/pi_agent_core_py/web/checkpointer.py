"""Agent/Web adapters for provider-neutral Workspace continuity.

The durable state machine and Markdown rendering live in
``agent_workspace.continuity``. This module only translates canonical agent
messages and ``ModelClient`` stream events at the application boundary.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_workspace.continuity import (
    CHECKPOINTER_COMMAND,
    SESSION_MEMORY_PATH,
    SLASH_COMMANDS,
    CheckpointerError,
    CheckpointerRecoverySummary,
    CheckpointSource,
    extract_checkpoint_source_hash,
    parse_slash_command,
    render_memory_document,
)
from agent_workspace.continuity import (
    build_checkpoint_source as _build_checkpoint_source,
)
from agent_workspace.continuity import (
    generate_checkpoint_memory as _generate_checkpoint_memory,
)
from agent_workspace.continuity import (
    recover_checkpointer_operations as _recover_checkpointer_operations,
)

from ..agent.harness.session.memory import serialize_messages
from ..agent.messages import TextContent
from ..ai.llm_messages import LLMUserMessage
from ..ai.model_client import ModelClient
from ..ai.stream_events import DoneEvent, ErrorEvent, TextDeltaEvent
from ..session_backends.sqlite import SessionOperationConflictError


def build_checkpoint_source(messages: list[Any]) -> CheckpointSource:
    """Translate canonical Core messages into a Workspace checkpoint source."""
    return _build_checkpoint_source(
        serialize_messages(messages),
        original_messages=messages,
    )


async def recover_checkpointer_operations(
    session_store: Any,
    file_store: Any,
    *,
    session_id: str | None = None,
) -> CheckpointerRecoverySummary:
    """Bind the Core session conflict type to Workspace recovery."""
    return await _recover_checkpointer_operations(
        session_store,
        file_store,
        operation_conflict_error=SessionOperationConflictError,
        session_id=session_id,
    )


async def generate_checkpoint_memory(
    client: ModelClient,
    *,
    source: CheckpointSource,
    prior_memory: str | None,
    operation: str = "checkpointer",
    signal: asyncio.Event | None = None,
) -> str:
    """Adapt a Core ``ModelClient`` to the Workspace text-generator port."""

    async def generate_text(
        *,
        system_prompt: str,
        user_text: str,
        signal: asyncio.Event | None,
        metadata: dict[str, Any],
    ) -> str:
        return await _collect_text(
            client,
            system_prompt=system_prompt,
            user_text=user_text,
            signal=signal,
            metadata=metadata,
        )

    return await _generate_checkpoint_memory(
        generate_text,
        source=source,
        prior_memory=prior_memory,
        provider_id=getattr(client, "provider_id", ""),
        model_id=getattr(client, "model", ""),
        operation=operation,
        signal=signal,
    )


async def _collect_text(
    client: ModelClient,
    *,
    system_prompt: str,
    user_text: str,
    signal: asyncio.Event | None,
    metadata: dict[str, Any],
) -> str:
    parts: list[str] = []
    async for event in client.stream(
        system_prompt=system_prompt,
        messages=[LLMUserMessage(content=[TextContent(text=user_text)])],
        tools=None,
        signal=signal,
        metadata=metadata,
    ):
        if isinstance(event, TextDeltaEvent):
            parts.append(event.delta)
        elif isinstance(event, ErrorEvent):
            raise CheckpointerError("checkpoint_provider_error", event.message)
        elif isinstance(event, DoneEvent):
            if signal is not None and signal.is_set():
                raise asyncio.CancelledError
            break
    return "".join(parts).strip()


__all__ = [
    "CHECKPOINTER_COMMAND",
    "SESSION_MEMORY_PATH",
    "SLASH_COMMANDS",
    "CheckpointerError",
    "CheckpointSource",
    "CheckpointerRecoverySummary",
    "build_checkpoint_source",
    "extract_checkpoint_source_hash",
    "generate_checkpoint_memory",
    "parse_slash_command",
    "recover_checkpointer_operations",
    "render_memory_document",
]
