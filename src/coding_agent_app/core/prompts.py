"""Deterministic product prompt composition helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptContribution:
    source: str
    text: str


def compose_system_prompt_suffix(
    *contributions: PromptContribution | None,
) -> str | None:
    """Join non-empty contributions once while preserving source order."""

    parts: list[str] = []
    seen: set[str] = set()
    for contribution in contributions:
        if contribution is None:
            continue
        normalized = contribution.text.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parts.append(normalized)
    return "\n\n".join(parts) or None


__all__ = ["PromptContribution", "compose_system_prompt_suffix"]
