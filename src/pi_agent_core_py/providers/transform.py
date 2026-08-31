"""Provider-aware, immutable normalization of replayed LLM history.

Provider reasoning signatures are only valid for the exact provider/API/model
that produced them.  This module centralizes that boundary and also repairs
tool-call pairs before an adapter builds a wire payload.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from ..llm_messages import (
    LLMAssistantMessage,
    LLMMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from ..messages import ImageContent, TextContent, ThinkingContent, ToolCall

IMAGE_OMITTED_TEXT = "[Image omitted: the selected model does not support images.]"
MISSING_TOOL_RESULT_TEXT = "No result was provided for this tool call."


def _transform_images(
    content: Sequence[TextContent | ImageContent],
    *,
    supports_images: bool,
) -> list[TextContent | ImageContent]:
    if supports_images:
        return [item.model_copy(deep=True) for item in content]

    transformed: list[TextContent | ImageContent] = []
    for item in content:
        if isinstance(item, ImageContent):
            if not (
                transformed
                and isinstance(transformed[-1], TextContent)
                and transformed[-1].text == IMAGE_OMITTED_TEXT
            ):
                transformed.append(TextContent(text=IMAGE_OMITTED_TEXT))
        else:
            transformed.append(item.model_copy(deep=True))
    return transformed


def transform_messages_for_provider(
    messages: Sequence[LLMMessage],
    *,
    target_provider: str,
    target_api: str,
    target_model: str,
    supports_images: bool,
    normalize_tool_call_id: Callable[[str], str] | None = None,
) -> list[LLMMessage]:
    """Return a provider-safe copy of ``messages``.

    Rules mirror pi-ai's important replay invariants:

    * signed/redacted thinking is replayed only to the exact source model;
    * cross-model thinking becomes ordinary text and redacted blocks vanish;
    * unsupported images become an explicit placeholder;
    * tool-call IDs and results are normalized together;
    * aborted/error assistant turns and orphaned tool results are omitted;
    * missing results are synthesized before the next non-result message.
    """

    normalize_id = normalize_tool_call_id or (lambda value: value)
    normalized: list[LLMMessage] = []
    id_map: dict[str, str] = {}

    for message in messages:
        if isinstance(message, LLMUserMessage):
            normalized.append(message.model_copy(
                update={
                    "content": _transform_images(
                        message.content,
                        supports_images=supports_images,
                    ),
                },
                deep=True,
            ))
            continue

        if isinstance(message, LLMAssistantMessage):
            if message.stop_reason in {"error", "aborted"}:
                continue

            same_model = (
                message.provider == target_provider
                and message.api == target_api
                and message.model == target_model
            )
            content: list[TextContent | ThinkingContent] = []
            for item in message.content:
                if isinstance(item, ThinkingContent) and not same_model:
                    if not item.redacted and item.thinking:
                        content.append(TextContent(text=item.thinking))
                else:
                    content.append(item.model_copy(deep=True))

            calls: list[ToolCall] = []
            for call in message.tool_calls:
                normalized_id = normalize_id(call.id)
                id_map[call.id] = normalized_id
                calls.append(call.model_copy(
                    update={"id": normalized_id},
                    deep=True,
                ))

            normalized.append(message.model_copy(
                update={"content": content, "tool_calls": calls},
                deep=True,
            ))
            continue

        if isinstance(message, LLMToolResultMessage):
            result_id = id_map.get(message.tool_call_id)
            # A provider cannot safely replay a result whose call was removed
            # (for example because its assistant turn was aborted).
            if result_id is None:
                continue
            normalized.append(message.model_copy(
                update={
                    "tool_call_id": result_id,
                    "content": _transform_images(
                        message.content,
                        supports_images=supports_images,
                    ),
                },
                deep=True,
            ))

    repaired: list[LLMMessage] = []
    pending: dict[str, ToolCall] = {}

    def flush_missing_results() -> None:
        for call in pending.values():
            repaired.append(LLMToolResultMessage(
                tool_call_id=call.id,
                name=call.name,
                content=[TextContent(text=MISSING_TOOL_RESULT_TEXT)],
                is_error=True,
            ))
        pending.clear()

    for message in normalized:
        if isinstance(message, LLMToolResultMessage):
            if message.tool_call_id in pending:
                repaired.append(message)
                pending.pop(message.tool_call_id, None)
            continue

        if pending:
            flush_missing_results()
        repaired.append(message)
        if isinstance(message, LLMAssistantMessage):
            pending.update({call.id: call for call in message.tool_calls})

    if pending:
        flush_missing_results()
    return repaired


__all__ = [
    "IMAGE_OMITTED_TEXT",
    "MISSING_TOOL_RESULT_TEXT",
    "transform_messages_for_provider",
]
