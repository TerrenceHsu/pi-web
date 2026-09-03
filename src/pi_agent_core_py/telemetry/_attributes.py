"""Defensive Telemetry attribute copying and persistent redaction."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from .types import AttributeValue, SpanAttributes

_MAX_ATTRIBUTE_COUNT = 64
_MAX_KEY_LENGTH = 96
_MAX_STRING_LENGTH = 512
_MAX_SEQUENCE_LENGTH = 32
_SECRET_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)",
    re.IGNORECASE,
)
_CONTENT_KEY_SUFFIXES = (
    "prompt",
    "content",
    "message",
    "arguments",
    "argument",
    "output",
    "response",
    "text",
)


def _is_sensitive_key(name: str) -> bool:
    normalized = name.casefold().replace("-", "_").replace(".", "_")
    return bool(_SECRET_KEY.search(normalized)) or any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _CONTENT_KEY_SUFFIXES
    )


def _copy_value(value: object) -> AttributeValue | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        if isinstance(value, str):
            return value[:_MAX_STRING_LENGTH]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        copied = list(value[:_MAX_SEQUENCE_LENGTH])
        if all(isinstance(item, bool) for item in copied):
            return tuple(bool(item) for item in copied)
        if all(isinstance(item, int) and not isinstance(item, bool) for item in copied):
            return tuple(int(item) for item in copied)
        if all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in copied
        ):
            return tuple(float(item) for item in copied)
        if all(isinstance(item, str) for item in copied):
            return tuple(str(item)[:_MAX_STRING_LENGTH] for item in copied)
    return None


def copy_attributes(
    attributes: SpanAttributes | Mapping[str, object] | None,
    *,
    persistent: bool = False,
) -> dict[str, AttributeValue]:
    """Return a detached, bounded attribute map.

    Persistent adapters redact content-bearing keys.  Unknown values and
    unreadable mappings are ignored because Telemetry must remain passive.
    """

    copied: dict[str, AttributeValue] = {}
    if attributes is None:
        return copied
    try:
        items = attributes.items()
    except Exception:
        return copied
    try:
        for raw_name, raw_value in items:
            if len(copied) >= _MAX_ATTRIBUTE_COUNT:
                break
            if not isinstance(raw_name, str) or not raw_name:
                continue
            name = raw_name[:_MAX_KEY_LENGTH]
            if raw_value is None:
                continue
            if persistent and _is_sensitive_key(name):
                copied[name] = "[redacted]"
                continue
            value = _copy_value(raw_value)
            if value is not None:
                copied[name] = value
    except Exception:
        return copied
    return copied


def merge_attributes(
    current: Mapping[str, AttributeValue],
    attributes: SpanAttributes,
    *,
    persistent: bool = False,
) -> dict[str, AttributeValue]:
    merged = dict(current)
    merged.update(copy_attributes(attributes, persistent=persistent))
    return dict(list(merged.items())[-_MAX_ATTRIBUTE_COUNT:])


__all__ = ["copy_attributes", "merge_attributes"]
