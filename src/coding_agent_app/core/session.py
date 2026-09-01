"""Product session facade built on the provider-neutral Agent Harness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass

from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import AgentMessage
from pi_agent_core_py.model_client import ModelClient
from pi_agent_core_py.policy import ToolPermissionPolicy
from pi_agent_core_py.skills import SkillRegistry, SkillSelection
from pi_agent_core_py.tools import AgentTool

from .prompts import PromptContribution, compose_system_prompt_suffix
from .resources import CodingAgentResourceSnapshot, HarnessCodingAgentResourceLoader
from .services import CodingAgentServices, create_coding_agent_services
from .settings import CodingAgentMode
from .toolsets import CodingAgentToolset, ToolsetResolver


@dataclass(frozen=True, slots=True)
class CodingAgentRequestBinding:
    session_id: str
    mode: CodingAgentMode
    active_tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodingAgentRequestComposition:
    """One immutable, request-scoped product resource composition."""

    binding: CodingAgentRequestBinding
    resources: CodingAgentResourceSnapshot
    system_prompt_suffix: str | None
    provider_selection: object | None = None


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
        services: CodingAgentServices | None = None,
        close_harness: bool = True,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self.harness = harness
        self.toolsets = toolsets
        self.services = services or create_coding_agent_services(
            resources=HarnessCodingAgentResourceLoader(harness)
        )
        self._close_harness = close_harness
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
        available_tools: Iterable[AgentTool] | None = None,
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
        resolved_available_tools = (
            list(available_tools)
            if available_tools is not None
            else original_tools.list()
        )
        tools_are_request_scoped = mode != "direct" or available_tools is not None
        policy_is_request_scoped = effective_policy is not original_permission_policy
        try:
            selection = self.toolsets.resolve(
                mode=mode,
                available_tools=resolved_available_tools,
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

    @asynccontextmanager
    async def compose_request(
        self,
        *,
        mode: CodingAgentMode,
        resources: CodingAgentResourceSnapshot | None = None,
        coding_tool_names: Collection[str] = (),
        override_tools: Iterable[AgentTool] | None = None,
        permission_policy: ToolPermissionPolicy | None = None,
        prompt_contributions: Iterable[PromptContribution] = (),
    ) -> AsyncIterator[CodingAgentRequestComposition]:
        """Resolve and bind all product resources for one Session request.

        Provider, Skills, MCP/tools and Prompt context are frozen before the
        Agent starts.  The mutable Harness fields are restored together when
        the request exits, including cancellation and error paths.
        """

        resolved_resources = resources or await self.services.resources.load(
            self.session_id
        )
        resource_contributions = tuple(
            PromptContribution(source=f"resource:{index}", text=text)
            for index, text in enumerate(resolved_resources.context_fragments)
        )
        suffix = compose_system_prompt_suffix(
            *resource_contributions,
            *tuple(prompt_contributions),
        )
        provider_selection: object | None = None
        async with AsyncExitStack() as stack:
            binding = await stack.enter_async_context(
                self.bind_request(
                    mode=mode,
                    coding_tool_names=coding_tool_names,
                    override_tools=override_tools,
                    available_tools=resolved_resources.tools,
                    permission_policy=permission_policy,
                )
            )
            original_skills = self.harness.skill_registry
            original_injection_config = self.harness.skill_injection_config

            def restore_skills() -> None:
                self.harness.skill_registry = original_skills
                self.harness.skill_injection_config = original_injection_config

            # Register restoration before mutation.  AsyncExitStack unwinds it
            # before bind_request releases the Session lock, so a waiting request
            # can never observe another request's Skill snapshot.
            stack.callback(restore_skills)
            if resolved_resources.skills:
                self.harness.attach_skills(
                    SkillRegistry(resolved_resources.skills),
                    injection_config=original_injection_config,
                )
            elif original_skills is not None:
                self.harness.detach_skills()

            provider_runtime = self.services.provider_runtime
            if provider_runtime is not None:
                provider_selection = await provider_runtime.resolve_selection(
                    self.session_id
                )
                if provider_selection is not None:
                    await stack.enter_async_context(
                        provider_runtime.bind_to_harness(
                            harness=self.harness,
                            selection=provider_selection,
                        )
                    )
            yield CodingAgentRequestComposition(
                binding=binding,
                resources=resolved_resources,
                system_prompt_suffix=suffix,
                provider_selection=provider_selection,
            )

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
        async with self.compose_request(
            mode=mode,
            coding_tool_names=coding_tool_names,
            override_tools=override_tools,
            permission_policy=permission_policy,
            prompt_contributions=(
                (PromptContribution(source="caller", text=system_prompt_suffix),)
                if system_prompt_suffix is not None
                else ()
            ),
        ) as composition:
            return await self.harness.run_prompt(
                text,
                skill_selection=skill_selection,
                system_prompt_suffix=composition.system_prompt_suffix,
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
        async with self.compose_request(
            mode=mode,
            coding_tool_names=coding_tool_names,
            override_tools=override_tools,
            permission_policy=permission_policy,
            prompt_contributions=(
                (PromptContribution(source="caller", text=system_prompt_suffix),)
                if system_prompt_suffix is not None
                else ()
            ),
        ) as composition:
            return await self.harness.run_continue(
                skill_selection=skill_selection,
                system_prompt_suffix=composition.system_prompt_suffix,
            )

    async def close(self) -> None:
        if self._closed:
            return
        if self._binding_lock.locked():
            raise RuntimeError("cannot close coding-agent session during an active request")
        self._closed = True
        if self._close_harness:
            await self.harness.close()


def create_coding_agent_session(
    *,
    session_id: str,
    harness: AgentHarness,
    read_only_tool: Callable[[str], bool],
    services: CodingAgentServices | None = None,
    close_harness: bool = True,
) -> CodingAgentSession:
    """Create a product Session around an already configured Harness."""

    return CodingAgentSession(
        session_id=session_id,
        harness=harness,
        toolsets=ToolsetResolver(read_only=read_only_tool),
        services=services,
        close_harness=close_harness,
    )


__all__ = [
    "CodingAgentRequestBinding",
    "CodingAgentRequestComposition",
    "CodingAgentSession",
    "create_coding_agent_session",
]
