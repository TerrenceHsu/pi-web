"""Mode-aware, deterministic coding-agent toolset resolution."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass

from pi_agent_core_py.policy import ToolPermissionPolicy
from pi_agent_core_py.tools import AgentTool, ToolRegistry

from .settings import CodingAgentMode

ReadOnlyToolPredicate = Callable[[str], bool]


class ToolsetResolutionError(RuntimeError):
    """Raised before a request when its required toolset cannot be assembled."""


@dataclass(frozen=True, slots=True)
class CodingAgentToolset:
    mode: CodingAgentMode
    tools: tuple[AgentTool, ...]
    permission_policy: ToolPermissionPolicy | None

    @property
    def active_tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def create_registry(self) -> ToolRegistry:
        return ToolRegistry(list(self.tools))


class ToolsetResolver:
    """Resolve a complete request toolset without mutating the live Harness."""

    def __init__(self, *, read_only: ReadOnlyToolPredicate) -> None:
        self._read_only = read_only

    def resolve(
        self,
        *,
        mode: CodingAgentMode,
        available_tools: Iterable[AgentTool],
        permission_policy: ToolPermissionPolicy | None,
        coding_tool_names: Collection[str] = (),
        override_tools: Iterable[AgentTool] | None = None,
    ) -> CodingAgentToolset:
        available = self._unique(available_tools)
        override = None if override_tools is None else self._unique(override_tools)

        if override is not None and mode != "knowledge":
            raise ToolsetResolutionError(
                f"explicit tool override is not allowed for {mode} mode"
            )

        if override is not None:
            selected = override
        elif mode == "direct":
            selected = available
        elif mode == "read_only":
            selected = tuple(tool for tool in available if self._read_only(tool.name))
        elif mode in {"coding", "plan"}:
            required = set(coding_tool_names)
            selected = tuple(tool for tool in available if tool.name in required)
            missing = sorted(required - {tool.name for tool in selected})
            if missing or not selected:
                detail = ", ".join(missing) if missing else "all coding tools"
                raise ToolsetResolutionError(
                    f"required coding toolset is unavailable: {detail}"
                )
        elif mode == "knowledge":
            raise ToolsetResolutionError("knowledge mode requires an explicit toolset")
        elif mode == "checkpointer":
            selected = ()
        else:
            raise ToolsetResolutionError(f"unsupported coding-agent mode: {mode}")

        return CodingAgentToolset(
            mode=mode,
            tools=selected,
            permission_policy=permission_policy,
        )

    @staticmethod
    def _unique(tools: Iterable[AgentTool]) -> tuple[AgentTool, ...]:
        result: list[AgentTool] = []
        seen: set[str] = set()
        for tool in tools:
            if not tool.name:
                raise ToolsetResolutionError("tool name must not be empty")
            if tool.name in seen:
                raise ToolsetResolutionError(f"duplicate tool name: {tool.name}")
            seen.add(tool.name)
            result.append(tool)
        return tuple(result)


__all__ = [
    "CodingAgentToolset",
    "ReadOnlyToolPredicate",
    "ToolsetResolutionError",
    "ToolsetResolver",
]
