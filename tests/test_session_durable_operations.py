"""Durable SQLite lane-operation contracts."""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.messages import AssistantMessage, TextContent, UserMessage
from pi_agent_core_py.session_sqlite import (
    SessionOperationConflictError,
    SQLiteSessionStore,
)


def _messages() -> list[UserMessage | AssistantMessage]:
    return [
        UserMessage(content=[TextContent(text="remember")]),
        AssistantMessage(
            content=[TextContent(text="done")],
            api="test",
            provider="test",
            model="test-model",
        ),
    ]


async def test_operation_intent_is_append_only_and_retry_reuses_open_intent(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "operations.sqlite")
    await store.init()
    session = await store.create_session()
    await store.replace_messages(session.id, _messages())
    source_leaf = await store.get_active_leaf(session.id)

    first = await store.start_operation(
        session.id,
        kind="checkpointer",
        dedupe_key="a" * 64,
        payload={"source_sha256": "a" * 64},
    )
    retried = await store.start_operation(
        session.id,
        kind="checkpointer",
        dedupe_key="a" * 64,
        payload={"source_sha256": "a" * 64},
    )

    assert retried.id == first.id
    assert first.source_leaf_id == source_leaf
    assert [record.record_type for record in first.records] == [
        "operation_started"
    ]
    with pytest.raises(SessionOperationConflictError):
        await store.start_operation(
            session.id,
            kind="checkpointer",
            dedupe_key="b" * 64,
        )
    assert len(await store.list_open_operations()) == 1
    await store.close()


async def test_effect_and_lane_reset_finish_in_one_sqlite_commit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operations.sqlite"
    store = SQLiteSessionStore(database)
    await store.init()
    session = await store.create_session()
    await store.replace_messages(session.id, _messages())
    operation = await store.start_operation(
        session.id,
        kind="checkpointer",
        dedupe_key="c" * 64,
        payload={"source_sha256": "c" * 64},
    )
    await store.mark_operation_effect_committed(
        operation.id, {"file_id": "file-1"}
    )
    completed = await store.complete_operation_and_reset_lane(operation.id)

    assert completed.outcome == "completed"
    assert await store.list_messages(session.id) == []
    assert await store.get_active_leaf(session.id) is None
    assert await store.list_open_operations() == []
    await store.close()

    reopened = SQLiteSessionStore(database)
    await reopened.init()
    restored = await reopened.get_operation(operation.id)
    assert restored is not None
    assert restored.outcome == "completed"
    assert [record.record_type for record in restored.records] == [
        "operation_started",
        "effect_committed",
        "operation_finished",
    ]
    assert await reopened.list_messages(session.id) == []
    await reopened.close()


async def test_recovery_refuses_to_clear_messages_after_source_leaf_moves(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "operations.sqlite")
    await store.init()
    session = await store.create_session()
    await store.replace_messages(session.id, _messages())
    operation = await store.start_operation(
        session.id,
        kind="checkpointer",
        dedupe_key="d" * 64,
        payload={"source_sha256": "d" * 64},
    )
    await store.mark_operation_effect_committed(operation.id)
    await store.append_entry(
        session.id, UserMessage(content=[TextContent(text="newer message")])
    )

    with pytest.raises(SessionOperationConflictError):
        await store.complete_operation_and_reset_lane(operation.id)

    assert len(await store.list_messages(session.id)) == 3
    still_open = await store.get_operation(operation.id)
    assert still_open is not None and still_open.is_open
    await store.close()
