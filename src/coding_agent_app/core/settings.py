"""Product-level settings contracts for coding-agent composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

CodingAgentMode = Literal[
    "direct",
    "read_only",
    "coding",
    "plan",
    "knowledge",
    "checkpointer",
]


@dataclass(frozen=True, slots=True)
class CodingAgentSettings:
    """Secret-free settings that affect product composition.

    Provider credentials and model bindings remain owned by their dedicated
    services.  This snapshot only contains policy choices needed while
    assembling one coding-agent session.
    """

    default_mode: CodingAgentMode = "direct"
    block_images: bool = False
    max_image_bytes: int = 10 * 1024 * 1024
    compaction_enabled: bool = True

    def __post_init__(self) -> None:
        if self.max_image_bytes <= 0:
            raise ValueError("max_image_bytes must be positive")


class CodingAgentSettingsProvider(Protocol):
    """Resolve an immutable settings snapshot for one durable Session."""

    async def get(self, session_id: str | None) -> CodingAgentSettings: ...


class StaticCodingAgentSettingsProvider:
    """Settings provider for embedders and backwards-compatible composition."""

    def __init__(self, settings: CodingAgentSettings | None = None) -> None:
        self._settings = settings or CodingAgentSettings()

    async def get(self, session_id: str | None) -> CodingAgentSettings:
        del session_id
        return self._settings


__all__ = [
    "CodingAgentMode",
    "CodingAgentSettings",
    "CodingAgentSettingsProvider",
    "StaticCodingAgentSettingsProvider",
]
