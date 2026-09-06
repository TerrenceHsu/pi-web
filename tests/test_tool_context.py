"""Tool projection preserves evidence, readback, and per-Session isolation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from coding_agent_app.core.tool_context import ToolContextBudgeter
from pi_agent_core_py.agent.context import convert_to_llm
from pi_agent_core_py.agent.harness.compaction.budget import estimate_message_tokens
from pi_agent_core_py.agent.messages import (
    AgentMessage,
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore
from pi_agent_core_py.session_backends.sqlite.context_store import (
    ContextStoreError,
    SQLiteContextStore,
)


@pytest.fixture
async def stores(tmp_path: Path) -> AsyncIterator[tuple[SQLiteSessionStore, SQLiteContextStore]]:
    repository = SQLiteSessionStore(tmp_path / "tool-context.sqlite")
    await repository.init()
    context = SQLiteContextStore(repository)
    await context.initialize()
    for sid in ("mine", "other"):
        await repository.create_session(session_id=sid)
    try:
        yield repository, context
    finally:
        await repository.close()


def _result(
    body: str, call: str = "call-1", *, error: bool = False, name: str = "test"
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call,
        name=name,
        content=[TextContent(text=body)],
        is_error=error,
        details={"execution": "verified", "value": {"a": 1}},
        usage=Usage(input=13),
        timestamp=1,
    )


def _metadata(message: AgentMessage) -> dict[str, Any]:
    assert isinstance(message, ToolResultMessage)
    assert isinstance(message.content[0], TextContent)
    return json.loads(message.content[0].text.splitlines()[1])


async def test_readonly_preview_reuses_prior_request_ref_without_crossing_session(stores):
    repository, context = stores
    budgeter = ToolContextBudgeter(context)
    raw = [_result("retained evidence\n" * 4000)]
    runtime = await budgeter.project("mine", raw, request_id="request-original")
    before = repository.connection.total_changes
    preview = await budgeter.project("mine", raw, request_id=None, allow_write=False)
    assert _metadata(preview[0])["ref"] == _metadata(runtime[0])["ref"]
    assert await budgeter.project("other", raw, request_id=None, allow_write=False) == raw
    assert repository.connection.total_changes == before


async def test_current_turn_offload_preserves_original_fields_and_pairing(stores) -> None:
    repository, context = stores
    body = "important data\n" * 4_000
    result = _result(body)
    messages: list[AgentMessage] = [
        UserMessage(content=[TextContent(text="inspect")]),
        AssistantMessage(
            api="test",
            provider="test",
            model="test",
            content=[
                ToolCall(id="call-1", name="test", arguments={"query": "x"}),
            ],
        ),
        result,
    ]
    original = [m.model_dump(mode="json") for m in messages]
    budgeter = ToolContextBudgeter(context)
    projected = await budgeter.project("mine", messages, request_id="current-request")
    assert [m.model_dump(mode="json") for m in messages] == original
    assert projected[:2] == messages[:2]
    assert isinstance(projected[-1], ToolResultMessage)
    assert projected[-1].tool_call_id == result.tool_call_id
    assert projected[-1].details == result.details and projected[-1].details is not result.details
    for field in ("name", "usage", "is_error", "terminate", "timestamp"):
        assert getattr(projected[-1], field) == getattr(result, field)
    assert estimate_message_tokens(convert_to_llm([projected[-1]])) <= 2000
    ref = _metadata(projected[-1])["ref"]
    first = await context.read_tool_output("mine", ref, max_chars=12000)
    rest = []
    offset = 0
    while True:
        page = await context.read_tool_output("mine", ref, offset, 12000)
        rest.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(rest) == body
    assert first["sha256"] == _metadata(projected[-1])["sha256"]
    assert (await context.snapshot("mine")).entry_ids == []  # Not yet persisted as a message.
    with pytest.raises(ContextStoreError, match="not_found"):
        await context.read_tool_output("other", ref)
    assert await budgeter.project("mine", messages, request_id="current-request") == projected
    assert await repository.list_messages("mine") == []


async def test_read_only_preview_never_writes_and_does_not_invent_references(stores) -> None:
    repository, context = stores
    messages = [_result("解析内容 " * 4_000)]
    budgeter = ToolContextBudgeter(context)
    changes = repository.connection.total_changes
    assert (
        await budgeter.project("mine", messages, request_id="request", allow_write=False)
        == messages
    )
    assert repository.connection.total_changes == changes
    runtime = await budgeter.project("mine", messages, request_id="request")
    changes = repository.connection.total_changes
    assert (
        await budgeter.project("mine", messages, request_id="request", allow_write=False) == runtime
    )
    assert repository.connection.total_changes == changes
    assert (
        await budgeter.project("other", messages, request_id="request", allow_write=False)
        == messages
    )
    assert repository.connection.total_changes == changes


async def test_duplicate_bodies_share_ref_without_dropping_calls(stores) -> None:
    _, context = stores
    body = "duplicated report " * 3_000
    messages = [_result(body, f"call-{i}") for i in range(3)]
    projected = await ToolContextBudgeter(context).project("mine", messages, request_id="request")
    refs = [_metadata(item)["ref"] for item in projected]
    assert len(set(refs)) == 1
    assert [item.tool_call_id for item in projected] == [item.tool_call_id for item in messages]
    assert _metadata(projected[1])["duplicate_body"] is True
    assert estimate_message_tokens(convert_to_llm([projected[1]])) <= 512
    assert (await context.read_tool_output("mine", refs[0]))["text"] == body[:6000]


async def test_batch_budget_shortens_oldest_first(stores) -> None:
    _, context = stores
    messages = [_result(f"unique-{i} " * 180, f"call-{i}") for i in range(6)]
    assert all(estimate_message_tokens(convert_to_llm([message])) < 2000 for message in messages)
    projected = await ToolContextBudgeter(context).project(
        "mine",
        messages,
        request_id="request",
        batch_tokens=1800,
    )
    assert estimate_message_tokens(convert_to_llm(projected)) <= 1800
    assert projected[0] != messages[0]
    assert projected[-1] == messages[-1]


async def test_error_head_tail_and_images_survive_projection(stores) -> None:
    _, context = stores
    result = _result(
        "Traceback: important location\n" + "middle noise " * 4000 + "\nValueError: exact failure",
        error=True,
    )
    image = ImageContent(data="aGVsbG8=", mime_type="image/png")
    result.content.insert(1, image)
    result.content.append(TextContent(text="Second text block failure detail"))
    projected = await ToolContextBudgeter(context).project("mine", [result], request_id="request")
    assert isinstance(projected[0], ToolResultMessage)
    assert projected[0].is_error is True
    assert projected[0].content[1] == image and projected[0].content[1] is not image
    assert "Traceback: important location" in projected[0].content[0].text
    assert "ValueError: exact failure" in projected[0].content[0].text
    assert "Second text block failure detail" in projected[0].content[0].text
    assert _metadata(projected[0])["status"] == "error"


async def test_storage_failure_and_oversize_keep_original(stores, monkeypatch) -> None:
    _, context = stores
    budgeter = ToolContextBudgeter(context)
    messages = [_result("long " * 5000)]

    async def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(context, "put_tool_output", fail)
    assert await budgeter.project("mine", messages, request_id="request") == messages
    for body in ("x" * (16 * 1024 * 1024 + 1), "x" * 20_000 + "\x00", "x" * 20_000 + "\ud800"):
        oversized = [_result(body)]
        assert await budgeter.project("mine", oversized, request_id="request") == oversized


async def test_history_namespace_and_image_only_are_stable(stores) -> None:
    repository, context = stores
    budgeter = ToolContextBudgeter(context)
    messages = [_result("history " * 4000)]
    projected = await budgeter.project("mine", messages, request_id=None)
    changes = repository.connection.total_changes
    assert await budgeter.project("mine", messages, request_id=None, allow_write=False) == projected
    assert repository.connection.total_changes == changes
    image_only = _result("")
    image_only.content = [ImageContent(data="aGVsbG8=", mime_type="image/png")]
    assert await budgeter.project("mine", [image_only], request_id=None) == [image_only]


async def test_read_tool_output_reuses_page_ref_without_recursive_storage(stores) -> None:
    repository, context = stores
    ref = await context.put_tool_output("mine", "request", "original-call", "数据 " * 8000)
    page = await context.read_tool_output("mine", ref, 20, 12000)
    messages = [_result(json.dumps(page, ensure_ascii=False), name="read_tool_output")]
    budgeter = ToolContextBudgeter(context)
    changes = repository.connection.total_changes
    projected = await budgeter.project(
        "mine", messages, request_id="request", per_result_tokens=700
    )
    assert projected != messages
    assert repository.connection.total_changes == changes
    metadata = _metadata(projected[0])
    assert metadata["ref"] == ref
    assert metadata["page_offset"] == 20
    assert 20 < metadata["next_offset"] < page["next_offset"]
    assert estimate_message_tokens(convert_to_llm(projected)) <= 700
    assert (
        await budgeter.project(
            "mine", messages, request_id="request", per_result_tokens=700, allow_write=False
        )
        == projected
    )
    assert (
        await budgeter.project("other", messages, request_id="request", per_result_tokens=700)
        == messages
    )
    page["text"] = "forged " * 3000
    unverified = [_result(json.dumps(page), name="read_tool_output")]
    assert await budgeter.project("mine", unverified, request_id="request") == unverified
    assert repository.connection.total_changes == changes


async def test_cancel_and_invalid_configuration_do_not_write(stores, monkeypatch) -> None:
    repository, context = stores
    budgeter = ToolContextBudgeter(context)
    signal = asyncio.Event()
    signal.set()
    changes = repository.connection.total_changes
    with pytest.raises(asyncio.CancelledError):
        await budgeter.project(
            "mine", [_result("large " * 4000)], request_id="request", signal=signal
        )
    assert repository.connection.total_changes == changes
    for kwargs in ({"per_result_tokens": 0}, {"batch_tokens": -1}, {"batch_tokens": True}):
        with pytest.raises(ValueError):
            await budgeter.project("mine", [], request_id=None, **kwargs)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(context, "find_tool_output", cancel)
    with pytest.raises(asyncio.CancelledError):
        await budgeter.project("mine", [_result("large " * 4000)], request_id="request")


@pytest.mark.parametrize("kind", ["invalid_json", "array", "missing", "wrong_hash", "wrong_text"])
async def test_reader_invalid_or_changed_page_is_never_offloaded(stores, kind: str) -> None:
    repository, context = stores
    ref = await context.put_tool_output("mine", "request", "original", "report " * 3000)
    value = await context.read_tool_output("mine", ref, max_chars=12000)
    if kind == "invalid_json":
        body = "invalid " * 2000
    elif kind == "array":
        body = json.dumps(["value " * 2000])
    elif kind == "missing":
        body = json.dumps({"text": "value " * 2000})
    else:
        if kind == "wrong_hash":
            value["sha256"] = "0" * 64
        else:
            value["text"] = "changed " * 1000
        body = json.dumps(value)
    message = _result(body, name="read_tool_output")
    changes = repository.connection.total_changes
    assert await ToolContextBudgeter(context).project(
        "mine",
        [message],
        request_id="request",
        per_result_tokens=200,
    ) == [message]
    assert repository.connection.total_changes == changes


async def test_tiny_budget_keeps_error_diagnostics_and_large_images(stores) -> None:
    _, context = stores
    message = _result(
        "Failure at analysis.py:1\n" + "noise " * 3000 + "\nTypeError: important", error=True
    )
    image = ImageContent(data="a" * 16000, mime_type="image/png")
    message.content.append(image)
    projected = await ToolContextBudgeter(context).project(
        "mine",
        [message],
        request_id="request",
        per_result_tokens=100,
        batch_tokens=100,
    )
    assert isinstance(projected[0], ToolResultMessage)
    assert projected[0].content[1] == image
    assert "Failure at analysis.py:1" in projected[0].content[0].text
    assert "TypeError: important" in projected[0].content[0].text
    assert estimate_message_tokens(convert_to_llm(projected)) > 100
    # This case is intentionally not advertised as meeting budget: admission
    # must reject it rather than silently dropping the image or failure status.


async def test_small_output_and_lookup_failure_leave_content_unchanged(stores, monkeypatch) -> None:
    repository, context = stores
    budgeter = ToolContextBudgeter(context)
    changes = repository.connection.total_changes
    message = _result("small successful result")
    assert await budgeter.project("mine", [message], request_id="request") == [message]
    assert await budgeter.project("mine", [], request_id="request") == []
    assert repository.connection.total_changes == changes

    async def fail(*args, **kwargs):
        raise RuntimeError("lookup unavailable")

    monkeypatch.setattr(context, "find_tool_output", fail)
    message = _result("report " * 3000)
    assert await budgeter.project("mine", [message], request_id="request") == [message]
    assert repository.connection.total_changes == changes
