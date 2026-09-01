"""Product session facade built on the provider-neutral Agent Harness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import AgentMessage
from pi_agent_core_py.model_client import ModelClient
from pi_agent_core_py.policy import ToolPermissionPolicy
from pi_agent_core_py.skills import SkillSelection
from pi_agent_core_py.tools import AgentTool

from .settings import CodingAgentMode
from .toolsets import CodingAgentToolset, ToolsetResolver


@dataclass(frozen=True, slots=True)
class CodingAgentRequestBinding:
    session_id: str
    mode: CodingAgentMode
    active_tool_names: tuple[str, ...]


class CodingAgentSession:
    """Own request-scoped mutations for one Harness-backed product Session.

    Transport adapters must not replace ``harness.agent.tools`` or
    ``harness.agent.client`` directly.  This facade makes those temporary
    changes atomic, rejects overlapping bindings, and restores every field on
    success, failure, or cancellation.
    """

    def __init__(
        self,
        *,
        session_id: str,
        harness: AgentHarness,
        toolsets: ToolsetResolver,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self.harness = harness
        self.toolsets = toolsets
        self._binding_lock = asyncio.Lock()
        self._closed = False

    @property
    def binding_active(self) -> bool:
        return self._binding_lock.locked()

    @asynccontextmanager
    async def bind_request(
        self,
        *,
        mode: CodingAgentMode,
        coding_tool_names: Collection[str] = (),
        override_tools: Iterable[AgentTool] | None = None,
        permission_policy: ToolPermissionPolicy | None = None,
    ) -> AsyncIterator[CodingAgentRequestBinding]:
        """Activate one mode and restore the prior Harness configuration."""

        if self._closed:
            raise RuntimeError("coding-agent session is closed")
        if self._binding_lock.locked():
            raise RuntimeError("coding-agent session already has an active request")

        await self._binding_lock.acquire()
        original_tools = self.harness.agent.tools
        original_permission_policy = self.harness.permission_policy
        effective_policy = (
            original_permission_policy
            if permission_policy is None
            else permission_policy
        )
        selection: CodingAgentToolset | None = None
        tools_are_request_scoped = mode != "direct"
        policy_is_request_scoped = effective_policy is not original_permission_policy
        try:
            selection = self.toolsets.resolve(
                mode=mode,
                available_tools=original_tools.list(),
                permission_policy=effective_policy,
                coding_tool_names=coding_tool_names,
                override_tools=override_tools,
            )
            if tools_are_request_scoped:
                self.harness.agent.tools = selection.create_registry()
            if policy_is_request_scoped:
                self.harness.set_permission_policy(selection.permission_policy)
            yield CodingAgentRequestBinding(
                session_id=self.session_id,
                mode=selection.mode,
                active_tool_names=selection.active_tool_names,
            )
        finally:
            if tools_are_request_scoped:
                self.harness.agent.tools = original_tools
            if policy_is_request_scoped:
                self.harness.set_permission_policy(original_permission_policy)
            self._binding_lock.release()

    @contextmanager
    def bind_client(self, client: ModelClient) -> Iterator[None]:
        """Temporarily bind a request wrapper around the currently selected client."""

        if self._closed:
            raise RuntimeError("coding-agent session is closed")
        original_client = self.harness.agent.client
        self.harness.agent.client = client
        try:
            yield
        finally:
            self.harness.agent.client = original_client

    async def run_prompt(
        self,
        text: str,
        *,
        mode: CodingAgentMode = "direct",
        coding_tool_names: Collection[str] = (),
        override_tools: Iterable[AgentTool] | None = None,
        permission_policy: ToolPermissionPolicy | None = None,
        skill_selection: SkillSelection | None = None,
        system_prompt_suffix: str | None = None,
    ) -> list[AgentMessage]:
        async with self.bind_request(
            mode=mode,
            coding_tool_names=coding_tool_names,
            override_tools=override_tools,
            permission_policy=permission_policy,
        ):
            return await self.harness.run_prompt(
                text,
                skill_selection=skill_selection,
                system_prompt_suffix=system_prompt_suffix,
            )

    async def run_continue(
        self,
        *,
        mode: CodingAgentMode = "direct",
        coding_tool_names: Collection[str] = (),
        override_tools: Iterable[AgentTool] | None = None,
        permission_policy: ToolPermissionPolicy | None = None,
        skill_selection: SkillSelection | None = None,
        system_prompt_suffix: str | None = None,
    ) -> list[AgentMessage]:
        async with self.bind_request(
            mode=mode,
            coding_tool_names=coding_tool_names,
            override_tools=override_tools,
            permission_policy=permission_policy,
        ):
            return await self.harness.run_continue(
                skill_selection=skill_selection,
                system_prompt_suffix=system_prompt_suffix,
            )

    async def close(self) -> None:
        if self._closed:
            return
        if self._binding_lock.locked():
            raise RuntimeError("cannot close coding-agent session during an active request")
        self._closed = True
        await self.harness.close()


def create_coding_agent_session(
    *,
    session_id: str,
    harness: AgentHarness,
    read_only_tool: Callable[[str], bool],
) -> CodingAgentSession:
    """Create a product Session around an already configured Harness."""

    return CodingAgentSession(
        session_id=session_id,
        harness=harness,
        toolsets=ToolsetResolver(read_only=read_only_tool),
    )


__all__ = [
    "CodingAgentRequestBinding",
    "CodingAgentSession",
    "create_coding_agent_session",
]
