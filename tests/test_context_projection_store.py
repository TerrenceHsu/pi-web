"""Context publications never mutate the canonical Session evidence tree."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import pytest

from pi_agent_core_py.agent.messages import SummaryMessage, TextContent, UserMessage
from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore
from pi_agent_core_py.session_backends.sqlite.context_store import (
    ContextStoreError,
    SQLiteContextStore,
)
from pi_agent_core_py.session_backends.sqlite.storage.writer_leases import WriterLeaseError


@pytest.fixture
async def context_stores(
    tmp_path: Path,
) -> AsyncIterator[tuple[SQLiteSessionStore, SQLiteContextStore]]:
    repository = SQLiteSessionStore(tmp_path / "context.sqlite")
    await repository.init()
    context = SQLiteContextStore(repository)
    await context.initialize()
    for session_id in ("mine", "other"):
        await repository.create_session(session_id=session_id)
    try:
        yield repository, context
    finally:
        await repository.close()


async def test_context_publication_keeps_stable_evidence_and_settings(context_stores):
    repository, context = context_stores
    await repository.replace_messages(
        "mine",
        [
            SummaryMessage(content=[TextContent(text="legacy summary")]),
            UserMessage(content=[TextContent(text="必须执行前确认")]),
        ],
    )
    before = await context.snapshot("mine")
    entries_before = await repository.list_all_entries("mine")
    record_id = await context.begin("mine", before.leaf_id, {"source_ids": before.entry_ids})
    prepared = (await context.records("mine"))[0]
    assert prepared["status"] == "prepared"
    assert prepared["lane"] == before.lane
    payload = {"summary": "用户要求执行前确认", "source_ids": before.entry_ids}
    committed = await context.commit("mine", record_id, payload)
    assert committed["status"] == "committed"
    assert await context.commit("mine", record_id, payload) == committed
    await context.fail("mine", record_id, "late_error")
    assert (await context.records("mine"))[0] == committed
    assert await context.snapshot("mine") == before
    assert await repository.list_all_entries("mine") == entries_before
    assert await context.settings("mine") == {"auto_compact": True}
    assert await context.set_settings("mine", False) == {"auto_compact": False}
    assert await context.settings("mine") == {"auto_compact": False}
    assert await context.settings("other") == {"auto_compact": True}


async def test_context_commit_rejects_changed_leaf_and_lane(context_stores):
    repository, context = context_stores
    initial = await context.snapshot("mine")
    assert initial.leaf_id is None and initial.entry_ids == [] and initial.messages == []
    record_id = await context.begin("mine", None, {})
    await repository.append_message("mine", UserMessage(content=[TextContent(text="new")]))
    with pytest.raises(ContextStoreError, match="context_source_changed"):
        await context.commit("mine", record_id, {"summary": "stale"})
    with pytest.raises(ContextStoreError, match="context_source_changed"):
        await context.begin("mine", None, {})
    await context.fail("mine", record_id, "source_changed")
    assert (await context.records("mine"))[0]["status"] == "failed"
    current = await context.snapshot("mine")
    next_record = await context.begin("mine", current.leaf_id, {})
    await repository.create_lane("mine", "alternative", current.leaf_id)
    await repository.set_active_lane("mine", "alternative")
    assert (await context.snapshot("mine")).leaf_id == current.leaf_id
    with pytest.raises(ContextStoreError, match="context_source_changed"):
        await context.commit("mine", next_record, {})
    assert (await context.records("mine"))[0]["status"] == "prepared"
    with pytest.raises(ContextStoreError, match="not_prepared"):
        await context.commit("mine", record_id, {})


async def test_context_cross_session_and_delete_cascade(context_stores):
    repository, context = context_stores
    record_id = await context.begin("mine", None, {"secret": "only mine"})
    output = await context.put_tool_output("mine", "request", "call", "only mine")
    await context.set_settings("mine", False)
    assert await context.records("other") == []
    with pytest.raises(ContextStoreError, match="record_not_found"):
        await context.commit("other", record_id, {})
    with pytest.raises(ContextStoreError, match="record_not_found"):
        await context.fail("other", record_id, "test")
    with pytest.raises(ContextStoreError, match="output_not_found"):
        await context.read_tool_output("other", output)
    await repository.delete_session("mine")
    for table in ("session_context_records", "session_context_settings", "session_tool_outputs"):
        async with repository.connection.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
            assert (await cursor.fetchone())[0] == 0


async def test_context_restart_marks_only_prepared_interrupted(context_stores, tmp_path):
    repository, context = context_stores
    success = await context.begin("mine", None, {"first": True})
    await context.commit("mine", success, {"summary": "ready"})
    pending = await context.begin("mine", None, {"pending": True})
    await context.initialize()  # Same-instance idempotence is not a restart.
    assert (await context.records("mine"))[0]["status"] == "prepared"
    await repository.close()
    reopened = SQLiteSessionStore(tmp_path / "context.sqlite")
    await reopened.init()
    try:
        restored = SQLiteContextStore(reopened)
        await restored.initialize()
        records = {record["id"]: record for record in await restored.records("mine")}
        assert records[success]["status"] == "committed"
        assert records[pending]["status"] == "interrupted"
        assert records[pending]["payload"] == {"pending": True}
        with pytest.raises(ContextStoreError, match="not_prepared"):
            await restored.commit("mine", pending, {})
    finally:
        await reopened.close()


async def test_context_recovery_and_writes_respect_live_writer_lease(context_stores, tmp_path):
    _, context = context_stores
    await context.begin("mine", None, {})
    other_repository = SQLiteSessionStore(tmp_path / "context.sqlite")
    await other_repository.init()
    try:
        outsider = SQLiteContextStore(other_repository)
        with pytest.raises(WriterLeaseError):
            await outsider.initialize()
        assert (await context.records("mine"))[0]["status"] == "prepared"
        assert not other_repository.connection.in_transaction
    finally:
        await other_repository.close()


async def test_tool_outputs_are_immediately_recoverable_and_immutable(context_stores):
    repository, context = context_stores
    body = "结果📊第一段\n第二段abcd"
    ref = await context.put_tool_output("mine", "req-1", "call-1", body)
    before_changes = repository.connection.total_changes
    digest = sha256(body.encode()).hexdigest()
    assert await context.find_tool_output("mine", "req-1", "call-1", digest) == ref
    assert await context.find_tool_output("other", "req-1", "call-1", digest) is None
    assert await context.find_tool_output("mine", "req-1", "different-call", digest) is None
    assert repository.connection.total_changes == before_changes
    assert ref == await context.put_tool_output("mine", "req-1", "call-1", body)
    different = await context.put_tool_output("mine", "req-1", "call-1", "changed body")
    assert different != ref
    assert ref != await context.put_tool_output("other", "req-1", "call-1", body)
    first = await context.read_tool_output("mine", ref, max_chars=5)
    assert first["text"] == body[:5] and first["next_offset"] == 5
    rest = await context.read_tool_output("mine", ref, offset=5)
    assert first["text"] + rest["text"] == body
    assert rest["next_offset"] is None
    assert rest["sha256"] == sha256(body.encode()).hexdigest()
    assert (await context.read_tool_output("mine", ref, offset=100))["text"] == ""
    assert (await context.snapshot("mine")).entry_ids == []
    for kwargs in ({"offset": -1}, {"max_chars": 0}, {"max_chars": 12_001}, {"offset": True}):
        with pytest.raises(ContextStoreError, match="invalid_tool_output_page"):
            await context.read_tool_output("mine", ref, **kwargs)


async def test_context_parallel_sessions_share_connection_safely(context_stores):
    repository, context = context_stores

    async def worker(session_id):
        for number in range(8):
            await repository.append_message(
                session_id, UserMessage(content=[TextContent(text=f"{session_id}:{number}")])
            )
            snapshot = await context.snapshot(session_id)
            record = await context.begin(session_id, snapshot.leaf_id, {"number": number})
            await context.commit(session_id, record, {"number": number})
            ref = await context.put_tool_output(session_id, f"req-{number}", "call", session_id)
            assert (await context.read_tool_output(session_id, ref))["text"] == session_id

    await asyncio.gather(worker("mine"), worker("other"))
    for session_id in ("mine", "other"):
        assert len((await context.snapshot(session_id)).entry_ids) == 8
        assert len(await context.records(session_id)) == 8
    assert not repository.connection.in_transaction


async def test_context_cancellation_rolls_back_journal_before_releasing_connection(
    context_stores,
    monkeypatch,
):
    repository, context = context_stores
    db = repository.connection
    inserted = asyncio.Event()
    original_commit = db.commit

    async def stalled_commit():
        inserted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(db, "commit", stalled_commit)
    task = asyncio.create_task(context.begin("mine", None, {"cancelled": True}))
    await asyncio.wait_for(inserted.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(db, "commit", original_commit)
    assert not db.in_transaction
    assert await context.records("mine") == []
    assert await context.set_settings("other", False) == {"auto_compact": False}


async def test_context_validation_and_initialization_are_explicit(context_stores):
    repository, context = context_stores
    with pytest.raises(ContextStoreError, match="uninitialized"):
        await SQLiteContextStore(repository).snapshot("mine")
    with pytest.raises(ContextStoreError, match="invalid_context_payload"):
        await context.begin("mine", None, {"value": float("nan")})
    with pytest.raises(ContextStoreError, match="invalid_context_payload"):
        await context.begin("mine", None, {"value": object()})
    with pytest.raises(ContextStoreError, match="invalid_auto_compact"):
        await context.set_settings("mine", 1)
    with pytest.raises(ContextStoreError, match="invalid_tool_output"):
        await context.put_tool_output("mine", "", "call", "body")
    with pytest.raises(ContextStoreError, match="invalid_context_error_code"):
        await context.fail("mine", "missing", "")
    for body in ("prefix\x00suffix", "invalid\ud800"):
        with pytest.raises(ContextStoreError, match="invalid_tool_output"):
            await context.put_tool_output("mine", "req", "call", body)


async def test_context_cancellation_during_begin_leaves_no_open_transaction(
    context_stores,
    monkeypatch,
):
    repository, context = context_stores
    db = repository.connection
    begun = asyncio.Event()
    original_execute = db.execute

    async def paused_execute(sql, *args, **kwargs):
        cursor = await original_execute(sql, *args, **kwargs)
        if sql == "BEGIN IMMEDIATE":
            begun.set()
            await asyncio.Event().wait()
        return cursor

    monkeypatch.setattr(db, "execute", paused_execute)
    task = asyncio.create_task(context.begin("mine", None, {}))
    await asyncio.wait_for(begun.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(db, "execute", original_execute)
    assert not db.in_transaction
    assert await context.records("mine") == []
    await context.begin("other", None, {"unaffected": True})


async def test_context_schema_fault_rolls_back_and_can_retry(tmp_path, monkeypatch):
    repository = SQLiteSessionStore(tmp_path / "schema.sqlite")
    await repository.init()
    db = repository.connection
    original_execute = db.execute

    async def fail_index(sql, *args, **kwargs):
        cursor = await original_execute(sql, *args, **kwargs)
        if sql.startswith("CREATE INDEX IF NOT EXISTS idx_context_session"):
            raise RuntimeError("injected schema failure")
        return cursor

    try:
        context = SQLiteContextStore(repository)
        monkeypatch.setattr(db, "execute", fail_index)
        with pytest.raises(RuntimeError, match="injected schema failure"):
            await context.initialize()
        monkeypatch.setattr(db, "execute", original_execute)
        assert not db.in_transaction
        async with db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_context_records'"
        ) as cursor:
            assert await cursor.fetchone() is None
        await context.initialize()
        await repository.create_session(session_id="mine")
        assert await context.settings("mine") == {"auto_compact": True}
    finally:
        monkeypatch.setattr(db, "execute", original_execute)
        await repository.close()
