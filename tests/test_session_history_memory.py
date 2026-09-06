"""Read-only history and provenance-backed incremental memory acceptance tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_workspace.structured_memory import (
    MemoryEvidence,
    MemoryValidationError,
    generate_memory_update,
    memory_prompt_text,
    parse_memory,
    render_memory,
)
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.agent.messages import AssistantMessage, TextContent, ToolCall, UserMessage
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent, ToolCallEvent
from pi_agent_core_py.policy import DefaultToolPermissionPolicy
from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore
from pi_agent_core_py.session_backends.sqlite.history import HistoryReadError, SQLiteHistoryReader
from pi_agent_core_py.tools.session_history import ReadSessionHistoryTool, SearchSessionHistoryTool
from pi_agent_core_py.web.app import create_app, dispose_app


@pytest.mark.asyncio
async def test_history_readonly_scoped_body_search_archives_pagination(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite"
    store = SQLiteSessionStore(db_path)
    await store.init()
    try:
        for sid in ("mine", "other"):
            await store.create_session(session_id=sid, title=sid)
        first = UserMessage(content=[TextContent(text="记忆采用 MinerU，不用 Docling")])
        await store.replace_messages(
            "mine",
            [
                first,
                AssistantMessage(
                    content=[TextContent(text="旧方案")],
                    api="fake",
                    provider="fake",
                    model="fake",
                ),
            ],
        )
        old_entries = await store.list_entries("mine")
        await store.replace_messages(
            "other", [UserMessage(content=[TextContent(text="记忆 OTHER_SECRET")])]
        )
        await store.replace_messages(
            "mine",
            [
                first,
                AssistantMessage(
                    content=[TextContent(text="新方案")],
                    api="fake",
                    provider="fake",
                    model="fake",
                ),
            ],
        )
        reader = SQLiteHistoryReader(db_path)
        hits = await reader.search("mine", "记忆")
        assert len(hits["hits"]) == 1
        assert "OTHER_SECRET" not in json.dumps(hits)
        assert (await reader.search("mine", "MinerU"))["hits"]
        assert not (await reader.search("mine", "timestamp"))["hits"]
        assert (await reader.search("mine", "旧方案"))["hits"][0][
            "branch_status"
        ] == "archived_or_superseded"
        page = await reader.read("mine", old_entries[0].id, max_chars=3)
        assert page["next_offset"] == 3
        assert (await reader.read("mine", old_entries[0].id, offset=3))["text"] == first.content[
            0
        ].text[3:]
        other = (await store.list_entries("other"))[0]
        with pytest.raises(HistoryReadError, match="not_found"):
            await reader.read("mine", other.id)
        async with reader.connection() as connection:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                await connection.execute("DELETE FROM sessions")
        before = store.connection.total_changes
        await reader.search("mine", "记忆")
        assert store.connection.total_changes == before
        for tool_type in (SearchSessionHistoryTool, ReadSessionHistoryTool):
            tool = tool_type(reader, lambda: "mine")
            rejected = await tool.execute("t", {"session_id": "other", "sql": "SELECT 1"})
            assert rejected.is_error
            assert (await tool_type(reader, lambda: None).execute("t", {})).is_error
        await store.replace_messages("mine", [])
        assert (await reader.search("mine", "MinerU"))["hits"]
        await store.delete_session("mine")
        assert not (await reader.search("mine", "MinerU"))["hits"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_history_cancel_missing_database_no_bootstrap(tmp_path: Path) -> None:
    reader = SQLiteHistoryReader(tmp_path / "absent.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        await reader.search("a", "hello")
    assert not (tmp_path / "absent.sqlite").exists()
    store = SQLiteSessionStore(tmp_path / "real.sqlite")
    await store.init()
    try:
        signal = asyncio.Event()
        signal.set()
        with pytest.raises(asyncio.CancelledError):
            await SQLiteHistoryReader(tmp_path / "real.sqlite").search("a", "hello", signal=signal)
    finally:
        await store.close()


def _evidence(text: str = "必须执行前确认", **kwargs) -> MemoryEvidence:
    return MemoryEvidence(
        entry_id="entry-user", request_id="request-1", role="user", text=text, **kwargs
    )


async def _update(evidence, *, prior=None, changes=None, no_change=False):
    async def generate(**kwargs):
        assert "schema" in json.loads(kwargs["user_text"])
        return json.dumps({"no_change": no_change, "changes": changes or []})

    return await generate_memory_update(
        generate,
        prior_memory=prior,
        evidence=evidence,
        operation="auto_memory",
    )


def _change(text="必须执行前确认", **kwargs):
    return {"kind": "constraint", "text": text, "source_entry_ids": ["entry-user"], **kwargs}


@pytest.mark.asyncio
async def test_memory_provenance_noop_correction_and_branch_revalidation() -> None:
    body = await _update([_evidence()], changes=[_change()])
    items, notes = parse_memory(body)
    assert items[0].sources[0].request_id == "request-1"
    assert render_memory(items, notes) == body
    assert await _update([_evidence()], prior=body, changes=[_change()]) is None
    assert await _update([_evidence()], prior=body, no_change=True) is None
    corrected = await _update(
        [_evidence("改为只允许只读代码")],
        prior=body,
        changes=[
            _change("只允许只读代码", replaces=items[0].id, correction_quote="改为只允许只读代码")
        ],
    )
    updated, _ = parse_memory(corrected)
    assert updated[0].status == "superseded"
    assert "pending_confirmation" in memory_prompt_text(corrected, set())
    assert "pending_confirmation" not in memory_prompt_text(corrected, {"entry-user"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,error",
    [
        (_change(source_entry_ids=["entry-other"]), "unknown_memory_source"),
        (_change(kind="achievement"), "execution_evidence"),
        (_change(replaces="missing", correction_quote="必须"), "correction_requires"),
    ],
)
async def test_memory_rejects_unproven_claims(change, error) -> None:
    with pytest.raises(MemoryValidationError, match=error):
        await _update([_evidence()], changes=[change])


@pytest.mark.asyncio
async def test_memory_user_edits_pinned_notes_and_retrieval_not_fresh_evidence() -> None:
    body = await _update([_evidence()], changes=[_change()])
    edited = body.replace("- 必须执行前确认", "- 用户固定：禁止联网")
    items, _ = parse_memory(edited)
    assert items[0].pinned and not items[0].sources
    with pytest.raises(MemoryValidationError, match="pinned_memory"):
        await _update(
            [_evidence()],
            prior=edited,
            changes=[
                _change(replaces=items[0].id, correction_quote="必须执行前确认"),
            ],
        )
    result = await _update([_evidence()], prior="# Memory\n\n我的手工笔记", changes=[_change()])
    assert "我的手工笔记" in result
    retrieval = MemoryEvidence(
        entry_id="tool",
        role="toolResult",
        text="old facts",
        tool_name="read_session_history",
    )
    assert await _update([retrieval], changes=[_change()]) is None
    assert await _update([_evidence(active=False)], changes=[_change()]) is None
    assert await _update([
        MemoryEvidence(entry_id="old-summary", role="summary", text="旧摘要不是原始证据"),
    ], changes=[_change()]) is None


@pytest.mark.asyncio
async def test_memory_invalid_json_and_capacity_preserve_old_document() -> None:
    async def generate(**kwargs):
        return "# Memory\nUnstructured invented result"

    with pytest.raises(MemoryValidationError, match="invalid_memory_delta"):
        await generate_memory_update(
            generate, prior_memory=None, evidence=[_evidence()], operation="auto_memory"
        )
    with pytest.raises(MemoryValidationError, match="legacy_memory_requires_review"):
        await _update([_evidence()], prior="x" * 16001, changes=[_change()])


class _MemoryClient(FakeClient):
    def __init__(self, scripts, *, remember=False):
        super().__init__(scripts)
        self.remember = remember
        self.memory_calls = []

    async def stream(self, **kwargs):
        if (kwargs.get("metadata") or {}).get("memory_schema"):
            assert kwargs.get("tools") is None
            data = json.loads(kwargs["messages"][0].content[0].text)
            self.memory_calls.append(data)
            response = {"no_change": True, "changes": []}
            if self.remember and len(self.memory_calls) == 1:
                source = next(e for e in data["turn_evidence"] if e["role"] == "user")
                response = {
                    "no_change": False,
                    "changes": [
                        {
                            "kind": "decision",
                            "text": "使用 MinerU 解析文档",
                            "source_entry_ids": [source["entry_id"]],
                        }
                    ],
                }
            yield TextDeltaEvent(delta=json.dumps(response))
            yield DoneEvent(stop_reason="stop")
        else:
            async for event in super().stream(**kwargs):
                yield event


def _answer(text="好的"):
    return [TextDeltaEvent(delta=text), DoneEvent(stop_reason="stop")]


def _wait(client, request_id):
    for _ in range(200):
        result = client.get(f"/api/requests/{request_id}").json()
        if result["status"] in {"completed", "error", "aborted"}:
            return result
        time.sleep(0.01)
    pytest.fail("request did not finish")


def _memory_file(client, sid):
    files = client.get(f"/api/sessions/{sid}/files").json()["files"]
    ref = next(f for f in files if f["logical_path"] == "Memory.md")
    return ref, client.get(f"/api/sessions/{sid}/files/{ref['id']}").text


def test_web_agent_history_memory_noop_and_restart(tmp_path):
    call = ToolCall(id="history-call", name="search_session_history", arguments={"query": "MinerU"})
    fake = _MemoryClient(
        [
            _answer(),
            [ToolCallEvent(tool_call=call), DoneEvent(stop_reason="tool_use")],
            _answer("查到历史来源"),
        ],
        remember=True,
    )

    def build(client):
        return create_app(
            AgentHarness(
                Agent(system_prompt="", client=client),
                permission_policy=DefaultToolPermissionPolicy(),
            ),
            db_path=tmp_path / "web.sqlite",
            uploads_dir=tmp_path / "uploads",
            enable_auto_memory=True,
        )

    app = build(fake)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "Mine"}).json()["id"]
        other = client.post("/api/sessions", json={"title": "Other"}).json()["id"]
        client.portal.call(
            app.state.web.session_store.replace_messages,
            other,
            [
                UserMessage(content=[TextContent(text="MinerU OTHER_SECRET")]),
            ],
        )
        started = client.post(
            "/api/prompt/async", json={"session_id": sid, "text": "使用 MinerU 解析文档"}
        )
        request_id = started.json()["request_id"]
        result = _wait(client, request_id)
        assert result["status"] == "completed", result
        assert result["result_summary"]["continuity"]["status"] == "updated", result
        ref, body = _memory_file(client, sid)
        items, _ = parse_memory(body)
        assert items[0].sources[0].request_id == request_id
        result = client.post(
            "/api/prompt", json={"session_id": sid, "text": "查看之前为什么选择 MinerU"}
        )
        assert result.status_code == 200, result.text
        assert result.json()["continuity"]["status"] == "no_change", result.json()
        assert _memory_file(client, sid)[0]["sha256"] == ref["sha256"]
        history_result = next(
            m for m in fake.last_messages if getattr(m, "tool_call_id", None) == "history-call"
        )
        payload = json.loads(history_result.content[0].text)
        assert payload["hits"] and "OTHER_SECRET" not in json.dumps(payload)
        assert not any(
            e["tool_name"] == "search_session_history"
            for e in fake.memory_calls[-1]["turn_evidence"]
        )
        # Moving away from the source branch must not inject its facts as current.
        client.portal.call(app.state.web.session_store.replace_messages, sid, [])
    dispose_app(app)

    restarted_fake = _MemoryClient([_answer("继续")])
    restarted = build(restarted_fake)
    with TestClient(restarted) as client:
        response = client.post("/api/prompt", json={"session_id": sid, "text": "继续"})
        assert response.status_code == 200, response.text
        assert "pending_confirmation" in restarted_fake.last_system_prompt
        assert "使用 MinerU" in restarted_fake.last_system_prompt
        assert _memory_file(client, sid)[0]["sha256"] == ref["sha256"]
    dispose_app(restarted)


@pytest.mark.asyncio
async def test_no_change_journal_recovery_and_corrupt_evidence(tmp_path):
    from agent_workspace.continuity import (
        CheckpointerError,
        build_checkpoint_source,
        checkpoint_source_from_operation_payload,
        checkpoint_source_to_operation_payload,
        recover_auto_memory_operations,
    )
    from agent_workspace.store import WorkspaceStore

    store = SQLiteSessionStore(tmp_path / "recovery.sqlite")
    await store.init()
    files = WorkspaceStore(tmp_path / "files")
    await files.init()
    try:
        await store.create_session(session_id="one", title="One")
        source = build_checkpoint_source(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        )
        payload = checkpoint_source_to_operation_payload(source)
        operation = await store.start_operation(
            "one", kind="auto_memory", dedupe_key=source.source_sha256, payload=payload
        )
        await store.mark_operation_effect_committed(operation.id, {"no_change": True})
        recovered = await recover_auto_memory_operations(store, files, session_id="one")
        assert recovered.completed == 1
        assert (await store.get_operation(operation.id)).outcome == "completed"
        with pytest.raises(CheckpointerError, match="Invalid memory sources"):
            checkpoint_source_from_operation_payload({**payload, "entries": ["bad"]})
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_history_initialization_is_atomic_and_retryable(tmp_path, monkeypatch):
    from pi_agent_core_py.session_backends.sqlite import history

    original = history.initialize_history_index

    async def crash(db):
        await original(db)
        raise RuntimeError("crash after index population before commit")

    path = tmp_path / "crash.sqlite"
    store = SQLiteSessionStore(path)
    monkeypatch.setattr(history, "initialize_history_index", crash)
    with pytest.raises(RuntimeError, match="crash"):
        await store.init()
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='session_history_text'").fetchone()
            is None
        )
    monkeypatch.setattr(history, "initialize_history_index", original)
    await store.init()
    try:
        await store.create_session(session_id="recover", title="Recover")
        await store.replace_messages(
            "recover", [UserMessage(content=[TextContent(text="重启恢复")])]
        )
        assert (await SQLiteHistoryReader(path).search("recover", "恢复"))["hits"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_read_history_tool_binds_scope_limits_and_hides_thinking(tmp_path):
    from pi_agent_core_py.agent.messages import ThinkingContent

    path = tmp_path / "hidden.sqlite"
    store = SQLiteSessionStore(path)
    await store.init()
    try:
        await store.create_session(session_id="mine", title="Mine")
        await store.replace_messages(
            "mine",
            [
                AssistantMessage(
                    api="fake",
                    provider="fake",
                    model="fake",
                    content=[
                        ThinkingContent(thinking="PRIVATE_REASONING"),
                        TextContent(text="public text api_key=TEST_ONLY_SECRET"),
                    ],
                )
            ],
        )
        entry = (await store.list_entries("mine"))[0]
        reader = SQLiteHistoryReader(path)
        tool = ReadSessionHistoryTool(reader, lambda: "mine")
        result = await tool.execute("read", {"entry_id": entry.id})
        assert not result.is_error
        assert "TEST_ONLY_SECRET" not in result.content[0].text
        assert "PRIVATE_REASONING" not in result.content[0].text
        assert not (await reader.search("mine", "PRIVATE_REASONING"))["hits"]
        assert (await tool.execute("read", {"entry_id": entry.id, "offset": -1})).is_error
        assert (await tool.execute("read", {"entry_id": entry.id, "max_chars": 12001})).is_error
    finally:
        await store.close()
