"""Source-bound LLM context views: bounded generation, rollback and auto policy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_workspace.structured_memory import (
    MemoryEvidence,
    MemoryItem,
    memory_prompt_text,
    render_memory,
)
from coding_agent_app.core.context_management import ContextManagementService
from coding_agent_app.core.tool_context import ToolContextBudgeter
from pi_agent_core_py.agent.context import ContextTransformInfo, convert_to_llm
from pi_agent_core_py.agent.harness.compaction.budget import estimate_context
from pi_agent_core_py.agent.harness.compaction.projection import effective_input_budget
from pi_agent_core_py.agent.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from pi_agent_core_py.ai.model_client import ModelClient
from pi_agent_core_py.ai.providers.base import ProviderAdapter, ProviderRequest
from pi_agent_core_py.ai.stream_events import DoneEvent, ErrorEvent, TextDeltaEvent, TextEndEvent
from pi_agent_core_py.ai.tooling import ToolDef
from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore
from pi_agent_core_py.session_backends.sqlite.context_store import SQLiteContextStore


class SummaryAdapter(ProviderAdapter):
    provider_id = "fake"
    api_id = "fake"
    model = "summary-test"

    def __init__(self, store, modes=None):
        self.store = store
        self.modes = list(modes or ["good"])
        self.calls = []
        self.entered = asyncio.Event()
        self.closed = False
        self.hook = None

    async def stream(self, request: ProviderRequest):
        self.calls.append(request)
        assert not request.tools
        assert request.thinking_level == "off"
        assert request.metadata["operation"] == "context_compaction"
        assert any(
            r["id"] == request.metadata["projection_id"] and r["status"] == "prepared"
            for r in await self.store.records("mine")
        )
        data = json.loads(request.messages[0].content[0].text)
        self.entered.set()
        mode = self.modes.pop(0) if self.modes else "good"
        if self.hook:
            await self.hook()
        try:
            if mode == "wait":
                await asyncio.Event().wait()
            if mode == "provider_error":
                yield ErrorEvent(message="C:/private/provider.sqlite password=secret")
                return
            if mode == "invalid":
                yield TextDeltaEvent(delta="not JSON")
            elif mode == "huge":
                yield TextDeltaEvent(delta="x" * 80_000)
            else:
                source = data["source_entries"][0]["entry_id"]
                if mode == "foreign":
                    source = "entry-from-another-session"
                summary = {
                    "current_goal": "Continue the user's task",
                    "facts": [
                        {"text": f"Fact from call {len(self.calls)}", "source_entry_ids": [source]}
                    ],
                    "decisions": [],
                    "failed_attempts": [],
                    "open_questions": [],
                    "next_steps": [],
                    "artifacts": [],
                    "memory_item_ids": [],
                }
                raw = json.dumps(summary)
                if mode == "text_end":
                    yield TextDeltaEvent(delta=raw[:30], content_index=0)
                    yield TextEndEvent(content_index=0, content=raw)
                else:
                    yield TextDeltaEvent(delta=raw)
            if mode != "missing_done":
                yield DoneEvent(stop_reason="length" if mode == "length" else "stop")
        finally:
            self.closed = True


def _assistant(text):
    return AssistantMessage(
        api="fake", provider="fake", model="fake", content=[TextContent(text=text)]
    )


async def _seed(repository, turns=4, size=4000):
    for index in range(turns):
        await repository.append_message(
            "mine", UserMessage(content=[TextContent(text=f"Goal {index}:" + "u" * size)])
        )
        await repository.append_message("mine", _assistant(f"Answer {index}:" + "a" * size))


def _info(adapter, *, system="Actual system", tools=()):
    return ContextTransformInfo(
        system_prompt=system, tools=tools, client=ModelClient(adapter), turn_index=0
    )


@pytest.fixture
async def management(tmp_path: Path) -> AsyncIterator[tuple]:
    repository = SQLiteSessionStore(tmp_path / "management.sqlite")
    await repository.init()
    store = SQLiteContextStore(repository)
    await store.initialize()
    await repository.create_session(session_id="mine")
    try:
        yield repository, store, ContextManagementService(store)
    finally:
        await repository.close()


async def test_manual_summary_journal_preserves_memory_ids_and_actual_budget(management):
    repository, store, manager = management
    await _seed(repository)
    before = await store.snapshot("mine")
    memory = render_memory(
        [
            MemoryItem(
                id="mem-1",
                kind="constraint",
                text="Execution requires confirmation",
                updated_at=1,
                sources=[MemoryEvidence(entry_id=before.entry_ids[0], role="user")],
            )
        ],
        "",
    )
    adapter = SummaryAdapter(store, ["text_end"])
    info = _info(
        adapter,
        system="System and Workspace " * 200,
        tools=[
            ToolDef(
                name="heavy",
                label="Heavy",
                description="A long definition " * 200,
                parameters={"type": "object"},
            )
        ],
    )
    result = await manager.compact("mine", model_context=info, context_window=64000, keep_turns=1)
    assert result["applied"]
    assert (
        result["token_stats"]["estimated_input_tokens_after"]
        < result["token_stats"]["estimated_input_tokens_before"]
    )
    assert (
        result["token_stats"]["estimated_input_tokens_before"]
        > result["token_stats"]["message_tokens_before"]
    )
    assert (await store.snapshot("mine")) == before
    projected = await manager.project("mine", before.messages)
    assert projected[0].role == "summary" and projected[-2:] == before.messages[-2:]
    assert "pending_confirmation" not in memory_prompt_text(
        memory, set((await store.snapshot("mine")).entry_ids)
    )
    assert (await manager.status("mine", include_summary=True))[
        "source_entry_ids"
    ] == before.entry_ids[:-2]
    await repository.branch("mine", before.entry_ids[0])
    assert await manager.active_record("mine") is None


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("invalid", "invalid_working_summary"),
        ("foreign", "invalid_working_summary"),
        ("provider_error", "summary_provider_error"),
        ("missing_done", "summary_missing_done"),
        ("length", "summary_incomplete_response"),
        ("huge", "summary_output_too_large"),
    ],
)
async def test_invalid_generation_preserves_original_view(management, mode, reason):
    repository, store, manager = management
    await _seed(repository)
    before = await store.snapshot("mine")
    adapter = SummaryAdapter(store, [mode])
    result = await manager.compact(
        "mine", model_context=_info(adapter), context_window=64000, keep_turns=1
    )
    assert not result["applied"] and result["reason"] == reason
    assert (await store.records("mine"))[0]["status"] == "failed"
    assert await manager.project("mine", before.messages) == before.messages
    assert await store.snapshot("mine") == before
    assert adapter.closed


async def test_compaction_source_cas_survives_concurrent_append(management):
    repository, store, manager = management
    await _seed(repository)
    adapter = SummaryAdapter(store)

    async def mutate():
        await repository.append_message(
            "mine", UserMessage(content=[TextContent(text="new user turn")])
        )

    adapter.hook = mutate
    result = await manager.compact(
        "mine", model_context=_info(adapter), context_window=64000, keep_turns=1
    )
    assert result["reason"] == "context_source_changed"
    assert await manager.active_record("mine") is None
    assert (await store.snapshot("mine")).messages[-1].content[0].text == "new user turn"


async def test_auto_circuit_breaks_after_two_failures_and_manual_retry_recovers(management):
    repository, store, manager = management
    await _seed(repository)
    adapter = SummaryAdapter(store, ["invalid", "invalid", "good"])
    info = _info(adapter)
    for _ in range(2):
        assert not (
            await manager.compact("mine", model_context=info, context_window=64000, keep_turns=1)
        )["applied"]
    assert (await manager.status("mine"))["circuit_open"]
    original = (await store.snapshot("mine")).messages
    assert (
        await manager.prepare(
            "mine", original, model_context=info, context_window=8000, request_state={}
        )
        == original
    )
    assert len(adapter.calls) == 2
    result = await manager.compact("mine", model_context=info, context_window=64000, keep_turns=1)
    assert result["applied"] and not (await manager.status("mine"))["circuit_open"]


async def test_auto_uses_eighty_percent_trigger_and_two_call_request_limit(management):
    repository, store, _ = management
    await _seed(repository, turns=6)
    manager = ContextManagementService(store, summary_input_tokens=4300)
    adapter = SummaryAdapter(store)
    original = (await store.snapshot("mine")).messages
    state = {}
    info = _info(adapter)
    result = await manager.prepare(
        "mine", original, model_context=info, context_window=16000, request_state=state
    )
    assert result[0].role == "summary"
    assert 1 <= state["context_summary_calls"] <= 2
    assert len(state["context_compaction_outcomes"]) == state["context_summary_calls"]
    assert all(outcome["duration_ms"] >= 0 for outcome in state["context_compaction_outcomes"])
    await manager.prepare(
        "mine", original, model_context=info, context_window=8000, request_state=state
    )
    assert len(adapter.calls) <= 2


async def test_unknown_window_skips_auto_but_allows_bounded_manual(management):
    repository, store, manager = management
    await _seed(repository)
    adapter = SummaryAdapter(store)
    info = _info(adapter)
    original = (await store.snapshot("mine")).messages
    assert (
        await manager.prepare(
            "mine", original, model_context=info, context_window=None, request_state={}
        )
        == original
    )
    assert adapter.calls == []
    assert (await manager.compact("mine", model_context=info, context_window=None, keep_turns=1))[
        "applied"
    ]


async def test_large_input_selects_complete_prefix_or_explicitly_refuses(management):
    repository, store, _ = management
    await _seed(repository, turns=6, size=2000)
    manager = ContextManagementService(store, summary_input_tokens=4300)
    adapter = SummaryAdapter(store)
    result = await manager.compact(
        "mine", model_context=_info(adapter), context_window=64000, keep_turns=1
    )
    assert result["applied"]
    covered = len(result["record"]["payload"]["covered_entry_ids"])
    assert 0 < covered < 10 and covered % 2 == 0
    data = json.loads(adapter.calls[0].messages[0].content[0].text)
    assert all(len(e["content"][0]["text"]) >= 2000 for e in data["source_entries"])
    await repository.replace_messages(
        "mine",
        [
            UserMessage(content=[TextContent(text="u" * 300_000)]),
            _assistant("a"),
            UserMessage(content=[TextContent(text="last turn")]),
        ],
    )
    result = await manager.compact(
        "mine", model_context=_info(adapter), context_window=64000, keep_turns=1
    )
    assert result["reason"] == "summary_input_budget_exceeded" and len(adapter.calls) == 1


async def test_repeated_compaction_preserves_prior_facts_and_uses_only_new_evidence(management):
    repository, store, manager = management
    await _seed(repository, turns=3)
    adapter = SummaryAdapter(store)
    info = _info(adapter)
    first = await manager.compact("mine", model_context=info, context_window=64000, keep_turns=1)
    assert first["applied"]
    previous_ids = first["record"]["payload"]["covered_entry_ids"]
    await _seed(repository, turns=2)
    second = await manager.compact("mine", model_context=info, context_window=64000, keep_turns=1)
    assert second["applied"]
    assert {fact["text"] for fact in second["record"]["payload"]["summary"]["facts"]} == {
        "Fact from call 1",
        "Fact from call 2",
    }
    second_input = json.loads(adapter.calls[-1].messages[0].content[0].text)
    assert second_input["prior_summary"]
    assert not set(previous_ids).intersection(e["entry_id"] for e in second_input["source_entries"])


@pytest.mark.parametrize("cancel", [True, False])
async def test_timeout_and_cancel_close_stream_and_finish_journal(management, cancel):
    repository, store, _ = management
    await _seed(repository)
    manager = ContextManagementService(store, summary_timeout_seconds=2 if cancel else 0.02)
    adapter = SummaryAdapter(store, ["wait"])
    signal = asyncio.Event()
    task = asyncio.create_task(
        manager.compact(
            "mine", model_context=_info(adapter), context_window=64000, keep_turns=1, signal=signal
        )
    )
    await asyncio.wait_for(adapter.entered.wait(), timeout=2)
    if cancel:
        signal.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task)["reason"] == "summary_timeout"
    assert adapter.closed
    assert (await store.records("mine"))[0]["status"] == "failed"
    assert await manager.active_record("mine") is None


async def test_no_savings_never_commits(management):
    repository, store, manager = management
    await _seed(repository, turns=2, size=1)
    result = await manager.compact(
        "mine", model_context=_info(SummaryAdapter(store)), context_window=64000, keep_turns=1
    )
    assert result["reason"] == "summary_does_not_reduce_context"
    assert await manager.active_record("mine") is None


async def test_existing_tool_offload_counts_before_auto_trigger(management):
    repository, store, manager = management
    await _seed(repository, turns=2, size=1)
    await repository.append_message("mine", UserMessage(content=[TextContent(text="current")]))
    await repository.append_message(
        "mine",
        AssistantMessage(
            api="fake",
            provider="fake",
            model="fake",
            content=[ToolCall(id="big", name="analysis", arguments={})],
        ),
    )
    await repository.append_message(
        "mine",
        ToolResultMessage(
            tool_call_id="big", name="analysis", content=[TextContent(text="result" * 20000)]
        ),
    )
    original = (await store.snapshot("mine")).messages
    await ToolContextBudgeter(store).project(
        "mine", original, request_id="request", allow_write=True
    )
    adapter = SummaryAdapter(store)
    await manager.prepare(
        "mine",
        original,
        model_context=_info(adapter),
        context_window=16000,
        request_state={"request_id": "request"},
    )
    assert adapter.calls == []


async def test_circuit_persists_across_repository_restart(management, tmp_path):
    repository, store, manager = management
    await _seed(repository)
    adapter = SummaryAdapter(store, ["invalid", "invalid"])
    for _ in range(2):
        await manager.compact(
            "mine",
            model_context=_info(adapter),
            context_window=64000,
            keep_turns=1,
        )
    await repository.close()
    reopened = SQLiteSessionStore(tmp_path / "management.sqlite")
    await reopened.init()
    try:
        restored_store = SQLiteContextStore(reopened)
        await restored_store.initialize()
        restored = ContextManagementService(restored_store)
        status = await restored.status("mine")
        assert status["circuit_open"] and not status["can_auto_compact"]
        assert (
            await restored.compact(
                "mine",
                model_context=_info(SummaryAdapter(restored_store)),
                context_window=64000,
                keep_turns=1,
            )
        )["applied"]
        assert not (await restored.status("mine"))["circuit_open"]
    finally:
        await reopened.close()


async def test_single_long_turn_is_not_reported_as_auto_recoverable(management):
    repository, _, manager = management
    await _seed(repository, turns=1, size=40000)
    status = await manager.status("mine")
    assert not status["can_auto_compact"] and not status["can_compact_prefix"]


async def test_auto_threshold_counts_actual_system_and_waits_until_eighty_percent(
    management,
    monkeypatch,
):
    repository, store, manager = management
    await _seed(repository, turns=2, size=1)
    original = (await store.snapshot("mine")).messages
    window = 20000
    budget = effective_input_budget(window)
    base = estimate_context(
        system_prompt="",
        messages=convert_to_llm(original),
        tools=[],
        context_window=window,
    ).estimated_input_tokens
    attempts = []

    async def attempted(*args, **kwargs):
        attempts.append(kwargs)
        return {"applied": False, "record": None, "reason": "probe", "token_stats": None}

    monkeypatch.setattr(manager, "compact", attempted)
    for ratio in (0.75, 0.82):
        system_size = int((budget * ratio - base) * 4 / 1.12)
        info = _info(SummaryAdapter(store), system="s" * system_size)
        await manager.prepare(
            "mine",
            original,
            model_context=info,
            context_window=window,
            request_state={},
        )
        assert len(attempts) == (0 if ratio < 0.8 else 1)


async def test_auto_success_reaches_sixty_percent_when_complete_prefix_fits(management):
    repository, store, manager = management
    await _seed(repository, turns=6, size=6000)
    original = (await store.snapshot("mine")).messages
    state = {}
    projected = await manager.prepare(
        "mine",
        original,
        model_context=_info(SummaryAdapter(store)),
        context_window=24000,
        request_state=state,
    )
    assert projected[0].role == "summary" and projected[-2:] == original[-2:]
    assert state["context_compaction"]["token_stats"]["target_reached"]
    assert state["context_compaction"]["token_stats"]["budget_ratio_after"] <= 0.6


async def test_unpersisted_latest_turn_is_budgeted_but_never_cited_or_compacted(management):
    repository, store, manager = management
    await _seed(repository, turns=3)
    snapshot = await store.snapshot("mine")
    pending = UserMessage(content=[TextContent(text="pending evidence " * 3000)])
    messages = [*snapshot.messages, pending]
    info = _info(SummaryAdapter(store))
    expected = estimate_context(
        system_prompt=info.system_prompt,
        messages=convert_to_llm(messages),
        tools=info.tools,
        context_window=64000,
    ).estimated_input_tokens
    outcome = await manager.compact(
        "mine",
        model_context=info,
        context_window=64000,
        keep_turns=1,
        messages=messages,
    )
    assert outcome["applied"]
    assert outcome["token_stats"]["estimated_input_tokens_before"] == expected
    assert (await manager.project("mine", messages))[-1] == pending
    assert await store.snapshot("mine") == snapshot


async def test_summary_marks_offloaded_tool_evidence_as_partial_from_canonical_comparison(
    management,
):
    repository, store, manager = management
    await repository.append_message(
        "mine",
        UserMessage(content=[TextContent(text="Inspect data " * 500)]),
    )
    await repository.append_message(
        "mine",
        AssistantMessage(
            api="fake",
            provider="fake",
            model="fake",
            content=[ToolCall(id="big", name="analysis", arguments={})],
        ),
    )
    original_body = "large raw data " * 10000
    await repository.append_message(
        "mine",
        ToolResultMessage(
            tool_call_id="big",
            name="analysis",
            content=[TextContent(text=original_body)],
        ),
    )
    await repository.append_message("mine", _assistant("Initial findings " * 500))
    await _seed(repository, turns=2)
    snapshot = await store.snapshot("mine")
    await ToolContextBudgeter(store).project(
        "mine",
        snapshot.messages,
        request_id="request",
        allow_write=True,
    )
    adapter = SummaryAdapter(store)
    outcome = await manager.compact(
        "mine",
        model_context=_info(adapter),
        context_window=64000,
        keep_turns=1,
        request_state={"request_id": "request"},
    )
    assert outcome["applied"]
    evidence = json.loads(adapter.calls[0].messages[0].content[0].text)
    tool_source = next(e for e in evidence["source_entries"] if e["role"] == "toolResult")
    assert tool_source["entry_id"] == snapshot.entry_ids[2]
    assert tool_source["content_complete"] is False
    assert tool_source["content_representation"] == "stored_tool_output_preview"
    assert "read_tool_output" in tool_source["content"][0]["text"]
    assert "not proof" in evidence["scope"]
    assert "never claim complete analysis" in adapter.calls[0].system_prompt
    assert (await store.snapshot("mine")).messages[2].content[0].text == original_body
    assert "content_complete" not in evidence["source_entries"][0]
