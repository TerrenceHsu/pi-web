"""Unified resource snapshot and diagnostics contracts.

The Web product currently persists Skills and MCP configuration in separate
stores.  This module provides the product-neutral boundary consumed by a
coding-agent runtime without moving storage policy into the Agent core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pi_agent_core_py.skills import Skill

ResourceDiagnosticLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class ResourceDiagnostic:
    level: ResourceDiagnosticLevel
    code: str
    message: str
    source: str | None = None


@dataclass(frozen=True, slots=True)
class CodingAgentResourceSnapshot:
    """Resources visible to one Session after precedence and trust decisions."""

    skills: tuple[Skill, ...] = ()
    mcp_tool_names: tuple[str, ...] = ()
    context_fragments: tuple[str, ...] = ()
    diagnostics: tuple[ResourceDiagnostic, ...] = ()


class CodingAgentResourceLoader(Protocol):
    async def load(self, session_id: str | None) -> CodingAgentResourceSnapshot: ...


@dataclass(slots=True)
class StaticCodingAgentResourceLoader:
    """Deterministic loader used by SDK embedders and compatibility paths."""

    snapshot: CodingAgentResourceSnapshot = field(
        default_factory=CodingAgentResourceSnapshot
    )

    async def load(self, session_id: str | None) -> CodingAgentResourceSnapshot:
        del session_id
        return self.snapshot


__all__ = [
    "CodingAgentResourceLoader",
    "CodingAgentResourceSnapshot",
    "ResourceDiagnostic",
    "ResourceDiagnosticLevel",
    "StaticCodingAgentResourceLoader",
]
