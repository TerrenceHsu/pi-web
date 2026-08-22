"""Step 20 — Web serializers 单元测试。

覆盖：
- to_json_safe：BaseModel / dict / list / tuple / Exception / fallback
- serialize_message / serialize_event
- serialize_snapshot_summary / serialize_snapshot_full
- serialize_session（None / 真实 SessionMemory）
- serialize_skill（默认不含 prompt / include_prompt=True）
- serialize_policy_audit_record
"""
from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from pi_agent_core_py import (
    DoneEvent,
    SessionMemory,
    Skill,
    TextContent,
    ToolPermissionAuditRecord,
    UserMessage,
)
from pi_agent_core_py.snapshot import RequestSnapshot, SnapshotBuilder
from pi_agent_core_py.web.content_integrity import (
    detect_content_warnings,
    summarize_content_integrity,
)
from pi_agent_core_py.web.serializers import (
    serialize_event,
    serialize_mcp_server_state,
    serialize_message,
    serialize_persisted_message,
    serialize_policy_audit_record,
    serialize_session,
    serialize_skill,
    serialize_snapshot_full,
    serialize_snapshot_summary,
    to_json_safe,
)

# ============================================================================
# to_json_safe
# ============================================================================


def test_to_json_safe_primitives() -> None:
    assert to_json_safe(None) is None
    assert to_json_safe(True) is True
    assert to_json_safe(42) == 42
    assert to_json_safe(3.14) == 3.14
    assert to_json_safe("hello") == "hello"


def test_to_json_safe_dict_and_list() -> None:
    assert to_json_safe({"a": 1, "b": [1, 2]}) == {"a": 1, "b": [1, 2]}
    assert to_json_safe([1, "x", None]) == [1, "x", None]
    assert to_json_safe((1, 2, 3)) == [1, 2, 3]  # tuple 转 list


def test_to_json_safe_nested_basemodel() -> None:
    class Inner(BaseModel):
        x: int

    class Outer(BaseModel):
        inner: Inner
        name: str

    out = to_json_safe(Outer(inner=Inner(x=42), name="o"))
    assert out == {"inner": {"x": 42}, "name": "o"}


def test_to_json_safe_exception() -> None:
    out = to_json_safe(ValueError("boom"))
    assert out["type"] == "ValueError"
    assert out["message"] == "boom"


def test_to_json_safe_fallback_repr() -> None:
    class Weird:
        def __repr__(self):
            return "<Weird>"

    out = to_json_safe(Weird())
    assert out == "<Weird>"


# ============================================================================
# serialize_message / serialize_event
# ============================================================================


def test_serialize_message_user_message() -> None:
    msg = UserMessage(content=[TextContent(text="hi")])
    out = serialize_message(msg)
    assert out["role"] == "user"
    assert out["content"][0]["text"] == "hi"
    assert "content_warnings" not in out


def test_serialize_message_marks_suspected_unicode_replacement_characters() -> None:
    msg = UserMessage(
        content=[
            TextContent(text="first \ufffd value and second \ufffd"),
        ]
    )

    out = serialize_message(msg)

    warning = out["content_warnings"][0]
    assert warning == {
        "code": "unicode_replacement_character",
        "suspected": True,
        "replacement_character_count": 2,
        "affected_value_count": 1,
        "affected_paths": ["/content/0/text"],
        "paths_truncated": False,
        "auto_repairable": False,
    }
    # Detection is metadata only; the serialized message is not rewritten.
    assert out["content"][0]["text"] == "first \ufffd value and second \ufffd"


def test_serialize_persisted_message_keeps_warning_with_stable_row_metadata() -> None:
    stored = SimpleNamespace(
        id="msg-corrupt",
        session_id="sess-corrupt",
        idx=3,
        role="user",
        created_at=123,
        message=UserMessage(content=[TextContent(text="legacy \ufffd text")]),
    )

    out = serialize_persisted_message(stored)

    assert out["message_id"] == "msg-corrupt"
    assert out["message"]["content_warnings"][0][
        "replacement_character_count"
    ] == 1
    assert out["content"][0]["text"] == "legacy \ufffd text"


def test_content_warning_scans_thinking_and_tool_details_with_bounded_paths() -> None:
    payload = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "reason \ufffd"}],
        "details": {"tool_output": "first \ufffd second \ufffd"},
    }

    warnings = detect_content_warnings(payload, max_affected_paths=1)

    assert warnings[0]["replacement_character_count"] == 3
    assert warnings[0]["affected_value_count"] == 2
    assert warnings[0]["affected_paths"] == ["/content/0/thinking"]
    assert warnings[0]["paths_truncated"] is True
    assert summarize_content_integrity(
        [{**payload, "content_warnings": warnings}]
    ) == {
        "suspected_message_count": 1,
        "replacement_character_count": 3,
    }


def test_serialize_event_done_event() -> None:
    ev = DoneEvent(stop_reason="stop", usage={"input": 0, "output": 0, "total_tokens": 0})
    out = serialize_event(ev)
    assert out["type"] == "done"
    assert out["stop_reason"] == "stop"


# ============================================================================
# serialize_snapshot_summary / serialize_snapshot_full
# ============================================================================


def _build_snapshot() -> RequestSnapshot:
    builder = SnapshotBuilder()
    builder.start(
        request_type="prompt",
        user_text="hello",
        messages_before=[UserMessage(content=[TextContent(text="hello")])],
        metadata={"k": "v"},
    )
    builder.observe_event(DoneEvent(
        stop_reason="stop",
        usage={"input": 0, "output": 0, "total_tokens": 0},
    ))
    return builder.finish(
        status="completed",
        messages_after=[
            UserMessage(content=[TextContent(text="hello")]),
        ],
    )


def test_serialize_snapshot_summary_counts() -> None:
    snap = _build_snapshot()
    summary = serialize_snapshot_summary(snap)
    assert summary["status"] == "completed"
    assert summary["messages_before_count"] == 1
    assert summary["messages_after_count"] == 1
    assert summary["events_count"] >= 1
    assert summary["turns_count"] == 0
    assert summary["metadata"] == {"k": "v"}
    # summary 不应含完整 messages / events
    assert "messages_before" not in summary
    assert "events" not in summary


def test_serialize_snapshot_full_has_messages() -> None:
    snap = _build_snapshot()
    full = serialize_snapshot_full(snap)
    assert full["status"] == "completed"
    assert isinstance(full["messages_before"], list)
    assert isinstance(full["events"], list)
    assert len(full["messages_before"]) >= 1


# ============================================================================
# serialize_session
# ============================================================================


def test_serialize_session_none_returns_not_attached() -> None:
    out = serialize_session(None)
    assert out == {"attached": False}


def test_serialize_session_real_session() -> None:
    sess = SessionMemory(session_id="demo-1", title="Demo", metadata={"project": "test"})
    out = serialize_session(sess)
    assert out["attached"] is True
    assert out["id"] == "demo-1"
    assert out["title"] == "Demo"
    assert out["turn_count"] == 0
    assert out["message_count"] == 0
    assert out["snapshot_count"] == 0
    assert out["metadata"]["project"] == "test"


# ============================================================================
# serialize_skill
# ============================================================================


def test_serialize_skill_default_excludes_prompt() -> None:
    skill = Skill(
        name="x",
        description="d",
        prompt="secret-prompt-body",
    )
    out = serialize_skill(skill)
    assert out["name"] == "x"
    assert "prompt" not in out


def test_serialize_skill_include_prompt_str() -> None:
    skill = Skill(
        name="x",
        description="d",
        prompt="secret-prompt-body",
    )
    out = serialize_skill(skill, include_prompt=True)
    assert out["prompt"] == "secret-prompt-body"


def test_serialize_skill_include_prompt_template() -> None:
    from pi_agent_core_py import PromptTemplate
    skill = Skill(
        name="x",
        description="d",
        prompt=PromptTemplate(
            name="x-tpl",
            template="hello {who}",
            variables=["who"],
        ),
    )
    out = serialize_skill(skill, include_prompt=True)
    assert isinstance(out["prompt"], dict)
    assert out["prompt"]["template"] == "hello {who}"
    assert out["prompt"]["variables"] == ["who"]


def test_serialize_skill_tags_and_metadata() -> None:
    skill = Skill(
        name="x",
        description="d",
        prompt="p",
        tags=["a", "b"],
        tool_names=["t1"],
        metadata={"source": "file", "path": "/x"},
    )
    out = serialize_skill(skill)
    assert out["tags"] == ["a", "b"]
    assert out["tool_names"] == ["t1"]
    assert out["metadata"]["source"] == "file"


# ============================================================================
# serialize_mcp_server_state
# ============================================================================


def test_serialize_mcp_server_state_basic() -> None:
    from pi_agent_core_py import MCPServerState
    state = MCPServerState(
        name="srv",
        connected=True,
        tool_count=3,
        metadata={"prompts_last_error": None},
    )
    out = serialize_mcp_server_state(state)
    assert out["name"] == "srv"
    assert out["connected"] is True
    assert out["tool_count"] == 3
    assert "prompts_last_error" in out["metadata"]


# ============================================================================
# serialize_policy_audit_record
# ============================================================================


def test_serialize_policy_audit_record() -> None:
    record = ToolPermissionAuditRecord(
        tool_call_id="call_1",
        tool_name="write_file",
        decision="deny",
        policy_name="default",
        reason="path outside workspace",
        metadata={"category": "write"},
    )
    out = serialize_policy_audit_record(record)
    assert out["tool_call_id"] == "call_1"
    assert out["decision"] == "deny"
    assert out["policy_name"] == "default"
    assert out["metadata"]["category"] == "write"
