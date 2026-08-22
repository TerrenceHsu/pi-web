"""Read-only message content integrity detection for Web API responses.

The Unicode replacement character (U+FFFD) is evidence that decoding may have
failed before a message was persisted.  It is not proof: users can type the
character intentionally.  Detection therefore emits a *suspected* warning and
never mutates or guesses the original content.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

UNICODE_REPLACEMENT_CHARACTER: Final = "\ufffd"
UNICODE_REPLACEMENT_WARNING_CODE: Final = "unicode_replacement_character"
DEFAULT_MAX_AFFECTED_PATHS: Final = 32


def _json_pointer(parts: Sequence[str]) -> str:
    """Return an RFC 6901 JSON Pointer without exposing any string value."""
    if not parts:
        return ""
    escaped = (part.replace("~", "~0").replace("/", "~1") for part in parts)
    return "/" + "/".join(escaped)


def detect_content_warnings(
    value: Any,
    *,
    max_affected_paths: int = DEFAULT_MAX_AFFECTED_PATHS,
) -> list[dict[str, Any]]:
    """Detect U+FFFD recursively in a JSON-safe message without changing it.

    Paths identify affected fields but never include content snippets.  The
    path list is bounded while aggregate counts remain accurate.
    """
    if max_affected_paths < 1:
        raise ValueError("max_affected_paths must be >= 1")

    replacement_count = 0
    affected_value_count = 0
    affected_paths: list[str] = []

    def visit(current: Any, path: tuple[str, ...]) -> None:
        nonlocal replacement_count, affected_value_count
        if isinstance(current, str):
            count = current.count(UNICODE_REPLACEMENT_CHARACTER)
            if count:
                replacement_count += count
                affected_value_count += 1
                if len(affected_paths) < max_affected_paths:
                    affected_paths.append(_json_pointer(path))
            return
        if isinstance(current, Mapping):
            for key, child in current.items():
                visit(child, (*path, str(key)))
            return
        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            for index, child in enumerate(current):
                visit(child, (*path, str(index)))

    visit(value, ())
    if replacement_count == 0:
        return []
    return [
        {
            "code": UNICODE_REPLACEMENT_WARNING_CODE,
            "suspected": True,
            "replacement_character_count": replacement_count,
            "affected_value_count": affected_value_count,
            "affected_paths": affected_paths,
            "paths_truncated": affected_value_count > len(affected_paths),
            "auto_repairable": False,
        }
    ]


def summarize_content_integrity(
    serialized_messages: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Aggregate message warnings for one API response without rescanning text."""
    suspected_message_count = 0
    replacement_character_count = 0
    for serialized in serialized_messages:
        message = serialized.get("message", serialized)
        if not isinstance(message, Mapping):
            continue
        warnings = message.get("content_warnings")
        if not isinstance(warnings, list) or not warnings:
            continue
        message_has_warning = False
        for warning in warnings:
            if not isinstance(warning, Mapping):
                continue
            if warning.get("code") != UNICODE_REPLACEMENT_WARNING_CODE:
                continue
            count = warning.get("replacement_character_count")
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                replacement_character_count += count
                message_has_warning = True
        if message_has_warning:
            suspected_message_count += 1
    return {
        "suspected_message_count": suspected_message_count,
        "replacement_character_count": replacement_character_count,
    }


__all__ = [
    "DEFAULT_MAX_AFFECTED_PATHS",
    "UNICODE_REPLACEMENT_CHARACTER",
    "UNICODE_REPLACEMENT_WARNING_CODE",
    "detect_content_warnings",
    "summarize_content_integrity",
]
