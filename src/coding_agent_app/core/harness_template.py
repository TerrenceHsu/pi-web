"""Create isolated Agent Harness instances from a product template."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import copy

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.agent.harness import AgentHarness
from pi_agent_core_py.ai.providers.base import ProviderAdapter, ProviderRequest
from pi_agent_core_py.ai.stream_events import StreamEvent


class _BorrowedProviderAdapter(ProviderAdapter):
    """Delegate requests without transferring transport ownership."""

    def __init__(self, delegate: ProviderAdapter) -> None:
        self._delegate = delegate
        self.provider_id = delegate.provider_id
        self.api_id = delegate.api_id
        self.model = delegate.model
        self.supports_images = delegate.supports_images

    def normalize_tool_call_id(self, tool_call_id: str) -> str:
        return self._delegate.normalize_tool_call_id(tool_call_id)

    def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        return self._delegate.stream(request)

    async def aclose(self) -> None:
        # The template Harness owns the underlying provider transport.
        return None


def clone_agent_harness(prototype: AgentHarness) -> AgentHarness:
    """Clone mutable Agent/Harness state while borrowing shared resources.

    Tool implementations may be shared because request-local Workspace and
    sandbox selection is carried by their ContextVar-backed bindings.  Tool
    registries, Agent state, Harness context, snapshots, Skills and permission
    audit logs are always independent.
    """

    source = prototype.agent
    # Preserve host-defined ModelClient subclasses.  Some product/test clients
    # deliberately override ``stream()`` to add routing, delay or recording;
    # rebuilding a plain ModelClient around their adapter would silently erase
    # that behaviour.  The shallow copy keeps the public client implementation
    # while the borrowed adapter prevents a Session clone from closing the
    # application-owned transport.
    client = copy(source.client)
    client.adapter = _BorrowedProviderAdapter(source.client.adapter)
    agent = Agent(
        system_prompt=source.system_prompt,
        client=client,
        tools=source.tools.list(),
        transform_context_fn=source.transform_context_fn,
        convert_to_llm_fn=source.convert_to_llm_fn,
        before_tool_call=source.before_tool_call,
        after_tool_call=source.after_tool_call,
        tool_execution=source.tool_execution,
        permission_policy=prototype.permission_policy,
        should_stop_after_turn=source.should_stop_after_turn,
        prepare_next_turn=source.prepare_next_turn,
        before_model_call=source.before_model_call,
        steering_mode=source.steering_mode,
        follow_up_mode=source.follow_up_mode,
        thinking_level=source.state.thinking_level,
        max_turns=source.max_turns,
    )
    cloned = AgentHarness(
        agent,
        before_request=list(prototype.before_request_hooks),
        after_request=list(prototype.after_request_hooks),
        on_event=list(prototype.on_event_hooks),
        on_error=list(prototype.on_error_hooks),
        metadata=dict(prototype.context.metadata),
        permission_policy=prototype.permission_policy,
        tool_approval_handler=prototype.tool_approval_handler,
    )
    if prototype.skill_registry is not None:
        cloned.attach_skills(
            [skill.model_copy(deep=True) for skill in prototype.skill_registry.list()],
            injection_config=prototype.skill_injection_config.model_copy(deep=True),
        )
    return cloned


__all__ = ["clone_agent_harness"]
