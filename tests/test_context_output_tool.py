"""The offloaded-output tool exposes bounded data, never SQL or Session selection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore
from pi_agent_core_py.session_backends.sqlite.context_store import (
    ContextStoreError,
    SQLiteContextStore,
)
from pi_agent_core_py.tools.context_output import ReadToolOutputTool


@pytest.fixture
async def output_store(
    tmp_path: Path,
) -> AsyncIterator[tuple[SQLiteSessionStore, SQLiteContextStore]]:
    repository = SQLiteSessionStore(tmp_path / "outputs.sqlite")
    await repository.init()
    store = SQLiteContextStore(repository)
    await store.initialize()
    for sid in ("mine", "other"):
        await repository.create_session(session_id=sid)
    try:
        yield repository, store
    finally:
        await repository.close()


def _payload(result):
    return json.loads(result.content[0].text)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"ref": ""},
        {"ref": "x" * 129},
        {"ref": 123},
        {"ref": "x", "offset": -1},
        {"ref": "x", "offset": True},
        {"ref": "x", "offset": "1"},
        {"ref": "x", "offset": 16 * 1024 * 1024 + 1},
        {"ref": "x", "max_chars": 0},
        {"ref": "x", "max_chars": 12001},
        {"ref": "x", "session_id": "other"},
        {"ref": "x", "sql": "SELECT * FROM sessions"},
        {"ref": "x", "path": "D:/private.sqlite"},
        {"ref": "x", "approved": True},
    ],
)
async def test_output_tool_strict_request_does_not_touch_store(output_store, args):
    repository, store = output_store
    before = repository.connection.total_changes
    tool = ReadToolOutputTool(store, lambda: "mine")
    result = await tool.execute("call", args)
    assert result.is_error
    assert _payload(result) == {"error_code": "invalid_tool_output_request"}
    assert repository.connection.total_changes == before
    assert set(tool.parameters["properties"]) == {"ref", "offset", "max_chars"}
    assert tool.parameters["additionalProperties"] is False
    assert tool.execution_mode == "parallel"


async def test_output_tool_pages_are_session_scoped_readonly_data(output_store):
    repository, store = output_store
    body = "结果📊第一段\n第二段内容"
    ref = await store.put_tool_output("mine", "req", "call", body)
    before = repository.connection.total_changes
    tool = ReadToolOutputTool(store, lambda: "mine")
    first_result = await tool.execute("tool-call", {"ref": ref, "max_chars": 5})
    assert not first_result.is_error
    assert first_result.tool_call_id == "tool-call"
    assert first_result.name == "read_tool_output"
    first = _payload(first_result)
    assert first["text"] == body[:5] and first["next_offset"] == 5
    second = _payload(await tool.execute("next", {"ref": ref, "offset": 5}))
    assert first["text"] + second["text"] == body
    assert second["next_offset"] is None
    assert first["trust"] == "untrusted_tool_output"
    assert repository.connection.total_changes == before
    denied = await ReadToolOutputTool(store, lambda: "other").execute("call", {"ref": ref})
    assert denied.is_error and _payload(denied) == {"error_code": "tool_output_not_found"}
    missing = await ReadToolOutputTool(store, lambda: None).execute("call", {"ref": ref})
    assert _payload(missing) == {"error_code": "no_active_session"}


async def test_output_tool_redacts_credentials_across_page_boundaries(output_store):
    _, store = output_store
    body = 'prefix api_key=abcdefghijklm tail\n{"password":"private-value"}\nBearer abc.xyz-123'
    ref = await store.put_tool_output("mine", "req", "call", body)
    tool = ReadToolOutputTool(store, lambda: "mine")
    complete = _payload(await tool.execute("call", {"ref": ref}))
    assert "abcdefghijklm" not in complete["text"]
    assert "private-value" not in complete["text"]
    assert "abc.xyz-123" not in complete["text"]
    assert complete["text"].count("[REDACTED]") == 3
    start = body.index("abcdefghijklm") + 3
    inside = _payload(await tool.execute("call", {"ref": ref, "offset": start, "max_chars": 4}))
    assert inside["text"] == "[REDACTED]"
    assert inside["offset"] == start and inside["next_offset"] == start + 4
    assert inside["total_chars"] == len(body)
    assert (await store.read_tool_output("mine", ref))["text"] == body


@pytest.mark.parametrize("pre_cancel", [False, True])
async def test_output_tool_cancellation_propagates_without_orphan_reads(
    output_store,
    monkeypatch,
    pre_cancel,
):
    _, store = output_store
    signal = asyncio.Event()
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def pending_read(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(store, "read_tool_output", pending_read)
    if pre_cancel:
        signal.set()
    task = asyncio.create_task(
        ReadToolOutputTool(store, lambda: "mine").execute("call", {"ref": "x"}, signal=signal)
    )
    if not pre_cancel:
        await asyncio.wait_for(entered.wait(), timeout=2)
        signal.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entered.is_set() is not pre_cancel
    assert cleaned.is_set() is not pre_cancel


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("D:/private/account.sqlite contains password"),
        OSError("C:/private/key-file"),
        ContextStoreError("D:/private/account.sqlite"),
    ],
)
async def test_output_tool_storage_errors_hide_physical_paths(output_store, monkeypatch, error):
    _, store = output_store

    async def broken_read(*args, **kwargs):
        raise error

    monkeypatch.setattr(store, "read_tool_output", broken_read)
    result = await ReadToolOutputTool(store, lambda: "mine").execute("call", {"ref": "x"})
    assert result.is_error
    assert _payload(result) == {"error_code": "tool_output_storage_unavailable"}
