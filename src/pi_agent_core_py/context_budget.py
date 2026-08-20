"""Deterministic, conservative preflight context-budget estimation."""
from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .llm_messages import (
    LLMAssistantMessage,
    LLMMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from .messages import ThinkingContent
from .tools import ToolDef

ContextBudgetLevel = Literal["unknown", "normal", "warning", "compact", "blocked"]

ESTIMATOR_VERSION = "mixed-char-v1"
SAFETY_MARGIN = 1.12


@dataclass(frozen=True)
class ContextEstimate:
    system_prompt_tokens: int
    message_tokens: int
    tool_definition_tokens: int
    estimated_input_tokens: int
    reserved_output_tokens: int
    projected_tokens: int
    context_window: int | None
    input_ratio: float | None
    projected_ratio: float | None
    level: ContextBudgetLevel
    approximate: bool = True
    estimator_version: str = ESTIMATOR_VERSION

    @property
    def can_send(self) -> bool:
        return self.level != "blocked"

    def to_dict(self) -> dict[str, object]:
        return {
            "system_prompt_tokens": self.system_prompt_tokens,
            "message_tokens": self.message_tokens,
            "tool_definition_tokens": self.tool_definition_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "projected_tokens": self.projected_tokens,
            "context_window": self.context_window,
            "input_ratio": self.input_ratio,
            "projected_ratio": self.projected_ratio,
            "level": self.level,
            "can_send": self.can_send,
            "approximate": self.approximate,
            "estimator_version": self.estimator_version,
        }


def estimate_text_tokens(value: str) -> int:
    """Estimate mixed English/CJK text without pretending tokenizer precision.

    ASCII is grouped at roughly four characters/token.  Non-ASCII code points
    count one token each, which is intentionally conservative for CJK text.
    """
    if not value:
        return 0
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    non_ascii_chars = len(value) - ascii_chars
    return math.ceil(ascii_chars / 4) + non_ascii_chars


def _compact_json_tokens(value: object) -> int:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return estimate_text_tokens(rendered)


def _message_payload(message: LLMMessage) -> dict[str, object]:
    if isinstance(message, LLMUserMessage):
        return {
            "role": "user",
            "content": [item.text for item in message.content],
        }
    if isinstance(message, LLMAssistantMessage):
        return {
            "role": "assistant",
            "content": [
                item.thinking if isinstance(item, ThinkingContent) else item.text
                for item in message.content
            ],
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in message.tool_calls
            ],
        }
    if isinstance(message, LLMToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "content": [item.text for item in message.content],
            "is_error": message.is_error,
        }
    return {"role": getattr(message, "role", "unknown")}


def _with_margin(value: int) -> int:
    return math.ceil(value * SAFETY_MARGIN)


def estimate_message_tokens(messages: Sequence[LLMMessage]) -> int:
    """Estimate the provider-visible message portion of a context.

    Keeping this calculation public lets compaction use the exact same
    conservative estimator as preflight instead of maintaining a second token
    heuristic.
    """
    raw = sum(_compact_json_tokens(_message_payload(message)) + 4 for message in messages)
    return _with_margin(raw)


def classify_context_budget(
    input_ratio: float | None,
    projected_ratio: float | None,
) -> ContextBudgetLevel:
    if input_ratio is None:
        return "unknown"
    if input_ratio >= 0.95 or (
        projected_ratio is not None and projected_ratio > 1.0
    ):
        return "blocked"
    if input_ratio >= 0.85:
        return "compact"
    if input_ratio >= 0.70:
        return "warning"
    return "normal"


def estimate_context(
    *,
    system_prompt: str,
    messages: Sequence[LLMMessage],
    tools: Sequence[ToolDef] | None = None,
    context_window: int | None,
    reserved_output_tokens: int | None = None,
) -> ContextEstimate:
    system_tokens = _with_margin(estimate_text_tokens(system_prompt) + 4)
    message_tokens = estimate_message_tokens(messages)
    tool_raw = sum(
        _compact_json_tokens({
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }) + 12
        for tool in (tools or ())
    )
    tool_tokens = _with_margin(tool_raw)
    estimated_input = system_tokens + message_tokens + tool_tokens
    reserve = max(0, int(reserved_output_tokens or 0))
    projected = estimated_input + reserve
    input_ratio = (
        estimated_input / context_window
        if context_window is not None and context_window > 0
        else None
    )
    projected_ratio = (
        projected / context_window
        if context_window is not None and context_window > 0
        else None
    )
    return ContextEstimate(
        system_prompt_tokens=system_tokens,
        message_tokens=message_tokens,
        tool_definition_tokens=tool_tokens,
        estimated_input_tokens=estimated_input,
        reserved_output_tokens=reserve,
        projected_tokens=projected,
        context_window=context_window,
        input_ratio=input_ratio,
        projected_ratio=projected_ratio,
        level=classify_context_budget(input_ratio, projected_ratio),
    )


__all__ = [
    "ContextBudgetLevel",
    "ContextEstimate",
    "ESTIMATOR_VERSION",
    "SAFETY_MARGIN",
    "classify_context_budget",
    "estimate_context",
    "estimate_message_tokens",
    "estimate_text_tokens",
]
