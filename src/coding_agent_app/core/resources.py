"""Unified resource snapshot and diagnostics contracts.

The Web product currently persists Skills and MCP configuration in separate
stores.  This module provides the product-neutral boundary consumed by a
coding-agent runtime without moving storage policy into the Agent core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pi_agent_core_py.agent.harness import AgentHarness
from pi_agent_core_py.agent.harness.skills import Skill
from pi_agent_core_py.agent.tooling import AgentTool

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
    tools: tuple[AgentTool, ...] = ()
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


@dataclass(slots=True)
class HarnessCodingAgentResourceLoader:
    """Project the live extension surface of a configured Harness.

    The Harness remains the owner of MCP transports and application-managed
    extension mutation.  Each product Session receives an immutable view of
    the currently enabled Skills and registered tools for one request, so a
    Session never needs to share another Session's mutable registries.
    """

    harness: AgentHarness

    async def load(self, session_id: str | None) -> CodingAgentResourceSnapshot:
        del session_id
        skill_registry = self.harness.skill_registry
        skills = (
            tuple(skill.model_copy(deep=True) for skill in skill_registry.list())
            if skill_registry is not None
            else ()
        )
        tools = tuple(self.harness.agent.tools.list())
        return CodingAgentResourceSnapshot(
            skills=skills,
            tools=tools,
            mcp_tool_names=tuple(self.harness.list_registered_mcp_tool_names()),
        )


__all__ = [
    "CodingAgentResourceLoader",
    "CodingAgentResourceSnapshot",
    "HarnessCodingAgentResourceLoader",
    "ResourceDiagnostic",
    "ResourceDiagnosticLevel",
    "StaticCodingAgentResourceLoader",
]
