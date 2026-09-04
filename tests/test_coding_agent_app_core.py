from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from coding_agent_app.core import (
    CodingAgentResourceSelection,
    CodingAgentResourceSnapshot,
    CodingAgentServices,
    CodingAgentSession,
    CodingAgentSettings,
    HarnessCodingAgentResourceLoader,
    PromptContribution,
    StaticCodingAgentResourceLoader,
    StaticCodingAgentSettingsProvider,
    ToolsetResolutionError,
    ToolsetResolver,
    clone_agent_harness,
    compose_system_prompt_suffix,
    create_coding_agent_application,
    create_coding_agent_session,
)
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.mcp import MCPAgentTool, MCPClient, MCPServerConfig, MCPToolInfo
from pi_agent_core_py.mcp.transport import FakeMCPTransport
from pi_agent_core_py.model_client import DoneEvent, FakeClient
from pi_agent_core_py.policy import (
    AllowAllToolPermissionPolicy,
    DenyAllToolPermissionPolicy,
)
from pi_agent_core_py.skills import Skill, SkillRegistry
from pi_agent_core_py.tools import (
    AgentTool,
    ToolRegistry,
    ToolResult,
    ToolUpdateCallback,
)


class _Tool(AgentTool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.label = name
        self.description = f"test tool {name}"
        self.parameters = {"type": "object", "properties": {}}

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, object],
        *,
        signal=None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        del args, signal, on_update
        return ToolResult(tool_call_id=tool_call_id, name=self.name)


def _harness(*names: str) -> AgentHarness:
    client = FakeClient([[DoneEvent(stop_reason="stop")] for _ in range(10)])
    policy = DenyAllToolPermissionPolicy()
    return AgentHarness(
        Agent(
            system_prompt="base",
            client=client,
            tools=ToolRegistry([_Tool(name) for name in names]),
        ),
        permission_policy=policy,
    )


def _read_only(name: str) -> bool:
    return name.startswith("read")


def test_toolset_resolver_builds_deterministic_mode_views() -> None:
    resolver = ToolsetResolver(read_only=_read_only)
    available = [_Tool("read_file"), _Tool("write_file"), _Tool("coding_run")]
    policy = AllowAllToolPermissionPolicy()

    direct = resolver.resolve(
        mode="direct",
        available_tools=available,
        permission_policy=policy,
    )
    read_only = resolver.resolve(
        mode="read_only",
        available_tools=available,
        permission_policy=policy,
    )
    coding = resolver.resolve(
        mode="coding",
        available_tools=available,
        permission_policy=policy,
        coding_tool_names={"coding_run"},
    )

    assert direct.active_tool_names == ("read_file", "write_file", "coding_run")
    assert read_only.active_tool_names == ("read_file",)
    assert coding.active_tool_names == ("coding_run",)


def test_toolset_resolver_fails_closed_for_incomplete_coding_tools() -> None:
    resolver = ToolsetResolver(read_only=_read_only)
    with pytest.raises(ToolsetResolutionError, match="coding_write"):
        resolver.resolve(
            mode="coding",
            available_tools=[_Tool("coding_read")],
            permission_policy=None,
            coding_tool_names={"coding_read", "coding_write"},
        )


def test_knowledge_mode_requires_explicit_tools() -> None:
    resolver = ToolsetResolver(read_only=_read_only)
    with pytest.raises(ToolsetResolutionError, match="explicit toolset"):
        resolver.resolve(
            mode="knowledge",
            available_tools=[_Tool("read_file")],
            permission_policy=None,
        )


def test_explicit_tools_cannot_bypass_a_restricted_mode() -> None:
    resolver = ToolsetResolver(read_only=_read_only)
    with pytest.raises(ToolsetResolutionError, match="not allowed"):
        resolver.resolve(
            mode="read_only",
            available_tools=[_Tool("read_file")],
            permission_policy=None,
            override_tools=[_Tool("write_file")],
        )


@pytest.mark.asyncio
async def test_request_binding_restores_tools_policy_and_public_state() -> None:
    harness = _harness("read_file", "write_file", "coding_run")
    session = create_coding_agent_session(
        session_id="s1",
        harness=harness,
        read_only_tool=_read_only,
    )
    original_registry = harness.agent.tools
    original_policy = harness.permission_policy
    coding_policy = AllowAllToolPermissionPolicy()

    async with session.bind_request(
        mode="coding",
        coding_tool_names={"coding_run"},
        permission_policy=coding_policy,
    ) as binding:
        assert session.binding_active
        assert binding.session_id == "s1"
        assert binding.active_tool_names == ("coding_run",)
        assert harness.agent.tools.names() == ["coding_run"]
        assert harness.agent.state.active_tool_names == ("coding_run",)
        assert harness.permission_policy is coding_policy

    assert not session.binding_active
    assert harness.agent.tools is original_registry
    assert harness.agent.state.active_tool_names == (
        "read_file",
        "write_file",
        "coding_run",
    )
    assert harness.permission_policy is original_policy


@pytest.mark.asyncio
async def test_direct_request_does_not_swap_the_session_registry() -> None:
    harness = _harness("read_file", "write_file")
    session = create_coding_agent_session(
        session_id="s1",
        harness=harness,
        read_only_tool=_read_only,
    )
    original_registry = harness.agent.tools

    async with session.bind_request(mode="direct"):
        assert harness.agent.tools is original_registry

    assert harness.agent.tools is original_registry


@pytest.mark.asyncio
async def test_request_binding_restores_after_exception_and_rejects_overlap() -> None:
    harness = _harness("read_file", "write_file")
    session = create_coding_agent_session(
        session_id="s1",
        harness=harness,
        read_only_tool=_read_only,
    )
    original_registry = harness.agent.tools

    with pytest.raises(RuntimeError, match="boom"):
        async with session.bind_request(mode="read_only"):
            with pytest.raises(RuntimeError, match="active request"):
                async with session.bind_request(mode="direct"):
                    pass
            raise RuntimeError("boom")

    assert harness.agent.tools is original_registry
    assert not session.binding_active


def test_client_binding_restores_selected_client() -> None:
    harness = _harness("read_file")
    session = create_coding_agent_session(
        session_id="s1",
        harness=harness,
        read_only_tool=_read_only,
    )
    original = harness.agent.client
    replacement = FakeClient([])

    with session.bind_client(replacement):
        assert harness.agent.client is replacement
    assert harness.agent.client is original


@pytest.mark.asyncio
async def test_session_run_prompt_exposes_only_selected_tools() -> None:
    harness = _harness("read_file", "write_file")
    observed: list[tuple[str, ...]] = []

    async def before_request(_context) -> None:
        observed.append(tuple(harness.agent.tools.names()))

    harness.before_request_hooks.append(before_request)
    session = create_coding_agent_session(
        session_id="s1",
        harness=harness,
        read_only_tool=_read_only,
    )

    await session.run_prompt("inspect", mode="read_only")

    assert observed == [("read_file",)]
    assert harness.agent.tools.names() == ["read_file", "write_file"]


@pytest.mark.asyncio
async def test_application_runtime_reuses_replaces_and_closes_sessions() -> None:
    created: list[CodingAgentSession] = []

    def factory(session_id: str) -> CodingAgentSession:
        session = create_coding_agent_session(
            session_id=session_id,
            harness=_harness("read_file"),
            read_only_tool=_read_only,
        )
        created.append(session)
        return session

    app = create_coding_agent_application(session_factory=factory)
    first = await app.session("s1")
    assert await app.session("s1") is first

    replacement = await app.runtime.replace("s1")
    assert replacement is not first
    assert first.harness._unsubscribe is None

    await app.close()
    assert replacement.harness._unsubscribe is None
    assert len(created) == 2


@pytest.mark.asyncio
async def test_runtime_coalesces_concurrent_session_creation() -> None:
    created: list[CodingAgentSession] = []
    release_factory = asyncio.Event()

    async def factory(session_id: str) -> CodingAgentSession:
        await release_factory.wait()
        session = create_coding_agent_session(
            session_id=session_id,
            harness=_harness("read_file"),
            read_only_tool=_read_only,
        )
        created.append(session)
        return session

    app = create_coding_agent_application(session_factory=factory)
    first_task = asyncio.create_task(app.session("s1"))
    second_task = asyncio.create_task(app.session("s1"))
    await asyncio.sleep(0)
    release_factory.set()

    first, second = await asyncio.gather(first_task, second_task)

    assert first is second
    assert len(created) == 1
    await app.close()


@pytest.mark.asyncio
async def test_runtime_closes_session_created_during_shutdown() -> None:
    created: list[CodingAgentSession] = []
    release_factory = asyncio.Event()

    async def factory(session_id: str) -> CodingAgentSession:
        await release_factory.wait()
        session = create_coding_agent_session(
            session_id=session_id,
            harness=_harness("read_file"),
            read_only_tool=_read_only,
        )
        created.append(session)
        return session

    app = create_coding_agent_application(session_factory=factory)
    creation = asyncio.create_task(app.session("s1"))
    await asyncio.sleep(0)
    await app.close()
    release_factory.set()

    with pytest.raises(RuntimeError, match="runtime is closed"):
        await creation
    assert len(created) == 1
    assert created[0].harness._unsubscribe is None


@pytest.mark.asyncio
async def test_static_services_are_deterministic_and_secret_free() -> None:
    settings = CodingAgentSettings(block_images=True, max_image_bytes=1024)
    settings_provider = StaticCodingAgentSettingsProvider(settings)
    resources = CodingAgentResourceSnapshot(
        mcp_tool_names=("mcp__docs__search",),
        context_fragments=("workspace context",),
    )
    resource_loader = StaticCodingAgentResourceLoader(resources)
    services = CodingAgentServices(
        settings=settings_provider,
        resources=resource_loader,
    )

    assert await services.settings.get("s1") == settings
    assert await services.resources.load("s1") == resources


def test_prompt_contributions_are_ordered_trimmed_and_deduplicated() -> None:
    suffix = compose_system_prompt_suffix(
        PromptContribution(source="mode", text="  read only  "),
        PromptContribution(source="workspace", text="workspace"),
        PromptContribution(source="duplicate", text="read only"),
        None,
    )
    assert suffix == "read only\n\nworkspace"


@pytest.mark.asyncio
async def test_request_composition_binds_all_resources_and_restores_harness() -> None:
    harness = _harness("old_tool")
    harness.attach_skills(SkillRegistry([Skill(
        name="old",
        description="old skill",
        prompt="old prompt",
    )]))
    provider_client = FakeClient([])
    provider_events: list[tuple[str, str]] = []

    class _ProviderRuntime:
        async def resolve_selection(self, session_id: str) -> object:
            provider_events.append(("resolve", session_id))
            return {"profile": "test"}

        @asynccontextmanager
        async def bind_to_harness(self, *, harness: AgentHarness, selection: object):
            del selection
            original = harness.agent.client
            harness.agent.client = provider_client
            provider_events.append(("bind", harness.agent.client.provider_id))
            try:
                yield
            finally:
                harness.agent.client = original

    original_tools = harness.agent.tools
    original_skills = harness.skill_registry
    original_client = harness.agent.client
    resources = CodingAgentResourceSnapshot(
        skills=(Skill(
            name="request",
            description="request skill",
            prompt="request prompt",
        ),),
        tools=(_Tool("read_request"), _Tool("write_request")),
        mcp_tool_names=("read_request",),
        context_fragments=("workspace prompt",),
    )
    services = CodingAgentServices(provider_runtime=_ProviderRuntime())
    session = create_coding_agent_session(
        session_id="s1",
        harness=harness,
        read_only_tool=_read_only,
        services=services,
    )

    async with session.compose_request(
        mode="read_only",
        resources=resources,
        prompt_contributions=(PromptContribution("route", "route prompt"),),
    ) as composition:
        assert composition.binding.active_tool_names == ("read_request",)
        assert composition.system_prompt_suffix == "workspace prompt\n\nroute prompt"
        assert harness.agent.tools.names() == ["read_request"]
        assert harness.skill_registry is not None
        assert harness.skill_registry.names() == ["request"]
        assert harness.agent.client is provider_client
        with pytest.raises(RuntimeError, match="active request"):
            async with session.compose_request(
                mode="direct",
                resources=CodingAgentResourceSnapshot(
                    skills=(Skill(
                        name="overlap",
                        description="must never leak",
                        prompt="overlap prompt",
                    ),),
                    tools=(_Tool("overlap_tool"),),
                ),
            ):
                pass
        assert harness.skill_registry.names() == ["request"]

    assert harness.agent.tools is original_tools
    assert harness.skill_registry is original_skills
    assert harness.agent.client is original_client
    assert provider_events == [("resolve", "s1"), ("bind", "fake")]


@pytest.mark.asyncio
async def test_cloned_harnesses_isolate_state_and_do_not_close_template_client() -> None:
    class _CustomFakeClient(FakeClient):
        pass

    prototype = AgentHarness(
        Agent(system_prompt="test", client=_CustomFakeClient([[DoneEvent()]])),
    )
    prototype.agent.tools.register(_Tool("read_file"))
    first = clone_agent_harness(prototype)
    second = clone_agent_harness(prototype)

    first.agent.state.messages.append("first")  # type: ignore[arg-type]
    first.agent.tools.register(_Tool("first_only"))

    assert second.agent.state.messages == []
    assert second.agent.tools.names() == ["read_file"]
    assert prototype.agent.tools.names() == ["read_file"]
    assert isinstance(first.agent.client, _CustomFakeClient)
    assert first.agent.client.adapter is not prototype.agent.client.adapter

    await first.close()
    assert prototype.agent.client.adapter is not None
    await second.close()
    await prototype.close()


@pytest.mark.asyncio
async def test_harness_resource_loader_returns_detached_skill_and_tool_snapshot() -> None:
    harness = _harness("read_file", "mcp__docs__search")
    harness.attach_skills([Skill(
        name="review",
        description="review skill",
        prompt="review prompt",
    )])
    harness._mcp_tool_names.add("mcp__docs__search")

    snapshot = await HarnessCodingAgentResourceLoader(harness).load("s1")

    assert tuple(tool.name for tool in snapshot.tools) == (
        "read_file",
        "mcp__docs__search",
    )
    assert snapshot.mcp_tool_names == ("mcp__docs__search",)
    assert tuple(skill.name for skill in snapshot.skills) == ("review",)
    assert snapshot.skills[0] is not harness.skill_registry.get("review")


@pytest.mark.asyncio
async def test_harness_resource_loader_filters_mcp_and_skills_per_session() -> None:
    harness = _harness("read_file")
    harness.attach_skills(
        [
            Skill(name="alpha", description="alpha", prompt="alpha"),
            Skill(name="beta", description="beta", prompt="beta"),
        ]
    )
    client = MCPClient(
        MCPServerConfig(name="docs", command="unused"),
        FakeMCPTransport(),
    )
    mcp_tool = MCPAgentTool(
        server_name="docs",
        client=client,
        tool_info=MCPToolInfo(name="search"),
    )
    harness.agent.tools.register(mcp_tool)
    harness._mcp_tool_names.add(mcp_tool.name)

    async def selection(_: str) -> CodingAgentResourceSelection:
        return CodingAgentResourceSelection(skill_names=frozenset({"alpha"}))

    snapshot = await HarnessCodingAgentResourceLoader(
        harness,
        selection_loader=selection,
    ).load("session-1")

    assert tuple(skill.name for skill in snapshot.skills) == ("alpha",)
    assert tuple(tool.name for tool in snapshot.tools) == ("read_file",)
    assert snapshot.mcp_tool_names == ()
