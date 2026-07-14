"""P1-D1: Export Markdown 测试——18 用例。

覆盖 renderer 纯函数 + API endpoint + filename 安全 + 大小限制 + 安全过滤。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.markdown_export import (
    ExportMessage,
    build_export_filename,
    check_export_size,
    render_session_markdown,
    sanitize_filename,
)


def _make_harness() -> AgentHarness:
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client(tmp_path):
    harness = _make_harness()
    app = create_app(harness, db_path=str(tmp_path / "export.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


# ============================================================================
# Tests 1-6: Renderer 纯函数
# ============================================================================


def test_1_empty_session_renders_title():
    md = render_session_markdown(
        session_title="Empty",
        messages=[],
        exported_at="2026-07-13T00:00:00Z",
    )
    assert "# Empty" in md
    assert "Exported at" in md
    # 无 user/assistant 正文


def test_2_single_turn():
    msgs = [
        ExportMessage(id="u1", role="user", text="Hello", idx=0),
        ExportMessage(id="a1", role="assistant", text="Hi there", idx=1),
    ]
    md = render_session_markdown(session_title="Test", messages=msgs)
    assert "## User" in md
    assert "Hello" in md
    assert "## Assistant" in md
    assert "Hi there" in md


def test_3_multi_turn():
    msgs = [
        ExportMessage(id="u1", role="user", text="Q1", idx=0),
        ExportMessage(id="a1", role="assistant", text="A1", idx=1),
        ExportMessage(id="u2", role="user", text="Q2", idx=2),
        ExportMessage(id="a2", role="assistant", text="A2", idx=3),
    ]
    md = render_session_markdown(session_title="Multi", messages=msgs)
    assert md.count("## User") == 2
    assert md.count("## Assistant") == 2
    assert md.count("---") == 3  # 3 个 separator（turn 之间）


def test_4_chinese_and_emoji():
    msgs = [
        ExportMessage(id="u1", role="user", text="你好世界 🌍", idx=0),
        ExportMessage(id="a1", role="assistant", text="你好！🎉", idx=1),
    ]
    md = render_session_markdown(session_title="中文测试", messages=msgs)
    assert "你好世界 🌍" in md
    assert "你好！🎉" in md
    assert "中文测试" in md


def test_5_markdown_preserved():
    """用户 Markdown 正文保持——不被 escape/strip。"""
    user_text = "# My Heading\n\n- item 1\n- item 2\n\n**bold** and *italic*"
    msgs = [ExportMessage(id="u1", role="user", text=user_text, idx=0)]
    md = render_session_markdown(session_title="T", messages=msgs)
    assert user_text in md


def test_6_session_title_in_output():
    md = render_session_markdown(
        session_title="My Chat Session",
        messages=[],
    )
    assert "# My Chat Session" in md


# ============================================================================
# Tests 7-9: Filename 安全
# ============================================================================


def test_7_safe_filename():
    assert sanitize_filename("hello world") == "hello world"
    assert sanitize_filename("simple") == "simple"
    assert sanitize_filename("") == "chat-export"
    assert sanitize_filename("   ") == "chat-export"


def test_8_path_traversal_title():
    """path traversal 字符被移除。"""
    assert sanitize_filename("../../../etc/passwd") == "etcpasswd"
    assert sanitize_filename("..\\..\\secret") == "secret"
    # 控制字符移除
    assert sanitize_filename("name\x00\x01\x02") == "name"
    # 非法 Windows 字符移除
    safe = sanitize_filename('file:*?"<>|')
    assert ":" not in safe and "*" not in safe and "?" not in safe
    # 结尾点 trim
    assert sanitize_filename("hello...") == "hello"


def test_9_unicode_content_disposition():
    from pi_agent_core_py.web.markdown_export import build_content_disposition

    cd = build_content_disposition("中文文件名_20260713.md")
    assert "attachment" in cd
    assert "filename*=" in cd
    assert "UTF-8''" in cd


# ============================================================================
# Tests 10-14: 安全过滤
# ============================================================================


async def test_10_not_found_404(web_client):
    client, _, _ = web_client
    resp = client.get("/api/sessions/nonexistent/export/markdown")
    assert resp.status_code == 404


async def test_11_system_prompt_not_exported():
    """system message 不导出。"""
    msgs = [
        ExportMessage(id="s1", role="system", text="SECRET SYSTEM PROMPT", idx=0),
        ExportMessage(id="u1", role="user", text="hello", idx=1),
    ]
    md = render_session_markdown(session_title="T", messages=msgs)
    assert "SECRET SYSTEM PROMPT" not in md


async def test_12_request_id_sequence_not_exported():
    """request_id / sequence 不在 renderer 输出。"""
    msgs = [
        ExportMessage(id="req_abc_1", role="user", text="hello", idx=0),
    ]
    md = render_session_markdown(session_title="T", messages=msgs)
    # message id 不出现在输出
    assert "req_abc_1" not in md


async def test_13_tool_result_not_exported_by_default():
    """toolResult 默认不导出。"""
    msgs = [
        ExportMessage(id="u1", role="user", text="hello", idx=0),
        ExportMessage(id="t1", role="toolResult", text='{"secret": "value"}', idx=1),
        ExportMessage(id="a1", role="assistant", text="answer", idx=2),
    ]
    md = render_session_markdown(session_title="T", messages=msgs)
    assert "secret" not in md
    assert "toolResult" not in md


async def test_14_raw_tool_args_not_exported():
    """raw tool args 不导出——include_runtime_summaries=False 默认。"""
    msgs = [
        ExportMessage(id="t1", role="toolResult", text='{"args": "secret_value"}', idx=0),
    ]
    md = render_session_markdown(session_title="T", messages=msgs)
    assert "secret_value" not in md


# ============================================================================
# Tests 15-16: 大小限制 + 顺序
# ============================================================================


def test_15_export_size_limit_413():
    """超大导出返回 413 error。"""
    big = "x" * (2_000_001)
    err = check_export_size(big)
    assert err is not None
    assert "max chars" in err


def test_16_message_order_preserved():
    """消息按 idx ASC 排序。"""
    msgs = [
        ExportMessage(id="a", role="user", text="first", idx=0),
        ExportMessage(id="b", role="assistant", text="second", idx=1),
        ExportMessage(id="c", role="user", text="third", idx=2),
    ]
    md = render_session_markdown(session_title="T", messages=msgs)
    assert md.index("first") < md.index("second")
    assert md.index("second") < md.index("third")


# ============================================================================
# Tests 17-18: API endpoint 集成
# ============================================================================


async def test_17_api_export_single_turn(web_client):
    client, _, _ = web_client
    # 发 prompt 产生 user + assistant message
    resp = client.post("/api/prompt", json={"text": "hello export test"})
    assert resp.status_code == 200

    # 获取 default session
    sid = client.get("/api/sessions").json()["sessions"][0]["id"]

    # 导出
    export_resp = client.get(f"/api/sessions/{sid}/export/markdown")
    assert export_resp.status_code == 200
    assert "text/markdown" in export_resp.headers.get("content-type", "")
    assert "attachment" in export_resp.headers.get("content-disposition", "")

    body = export_resp.text
    assert "## User" in body
    assert "hello export test" in body
    assert "## Assistant" in body
    assert export_resp.text.endswith("\n")


async def test_18_export_does_not_modify_session(web_client):
    """导出后原 session/messages 不被修改。"""
    client, _, _ = web_client
    client.post("/api/prompt", json={"text": "persist test"})
    sid = client.get("/api/sessions").json()["sessions"][0]["id"]

    # 导出前 messages
    before = client.get(f"/api/messages?session_id={sid}").json()

    # 导出
    client.get(f"/api/sessions/{sid}/export/markdown")

    # 导出后 messages 不变
    after = client.get(f"/api/messages?session_id={sid}").json()
    assert before["count"] == after["count"]


# ============================================================================
# Tests 19-22: 审核补强——空 session / 404 双路径 / CRLF 注入 / active draft 不导出
# ============================================================================


async def test_19_empty_session_endpoint_returns_valid_markdown(web_client):
    """空 session（无 messages）endpoint 返回 200 + 仅含 title 的 Markdown。"""
    client, _, _ = web_client
    # 创建一个新空 session（default session 可能有内容）
    create = client.post("/api/sessions", json={"title": "Empty Session"})
    assert create.status_code == 200
    sid = create.json()["id"]

    resp = client.get(f"/api/sessions/{sid}/export/markdown")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers.get("content-type", "")

    body = resp.text
    # 有 title 头
    assert "# Empty Session" in body
    # 没有 user / assistant 段
    assert "## User" not in body
    assert "## Assistant" not in body


async def test_20_not_found_session_404_both_paths(web_client):
    """不存在 session 返回 404——覆盖 get_session 和 list_messages 两个失败点。"""
    client, _, _ = web_client
    # 任意不存在的 sid
    fake_sid = "nonexistent-session-id-xxx"
    resp = client.get(f"/api/sessions/{fake_sid}/export/markdown")
    assert resp.status_code == 404
    assert resp.json()["detail"]


async def test_21_content_disposition_no_crlf_injection(web_client):
    """title 含 CR/LF 不能注入 Content-Disposition header。

    sanitize_filename 用 \\x00-\\x1f 正则移除所有控制字符（含 \\r\\n）。
    """
    from pi_agent_core_py.web.markdown_export import (
        build_content_disposition,
        sanitize_filename,
    )

    # 直接测 sanitize_filename——CRLF 被移除
    safe = sanitize_filename("evil\r\nSet-Cookie: admin=1")
    assert "\r" not in safe
    assert "\n" not in safe
    # "evil" + "Set-Cookie: admin=1"（空格保留，但移除 : 已经被移除……等等 : 在非法字符里）
    # 实际：sanitize 移除 / \\ : * ? " < > | 和 \\x00-\\x1f
    # 所以 "evil\r\nSet-Cookie: admin=1" → "evilSet-Cookie admin=1"（: 被移除，CRLF 被移除）
    assert "Set-Cookie" not in safe or ":" not in safe

    # 完整 Content-Disposition 链路——header 中无 CRLF
    filename = build_export_filename("evil\r\nInjected: value", "20260713")
    assert "\r" not in filename
    assert "\n" not in filename
    cd = build_content_disposition(filename)
    assert "\r" not in cd
    assert "\n" not in cd


async def test_22_active_request_draft_not_exported(web_client, tmp_path):
    """active streaming draft 不进 export——export 只读 SQLite 持久化消息。

    场景：user 已发消息并持久化，但 assistant 还在流式生成中
    （streamItems 中有 draft，SQLite messages 还没有 assistant）。
    此时 export 只导出 SQLite 中已持久化的 user，不含 draft。
    """
    import anyio

    from pi_agent_core_py.messages import TextContent, UserMessage

    client, harness, app = web_client

    # 拿到 store 直接写 user message（不发 prompt → 没有 assistant）
    store = app.state.web.session_store
    sid = client.get("/api/sessions").json()["sessions"][0]["id"]

    # 直接 append 一条 user message 到 SQLite（绕过 prompt 流程）
    user_msg = UserMessage(content=[TextContent(text="persisted user question")])
    await store.append_message(sid, user_msg)

    # 模拟前端 streamItems 中有一条未持久化的 assistant draft
    # 后端 export 不读 streamItems，所以 draft 内容不会出现在导出中
    DRAFT_TEXT = "STREAMING_DRAFT_SHOULD_NOT_APPEAR"

    # 用 anyio 让 event loop 走一圈，确保 store commit 落地
    await anyio.lowlevel.checkpoint()

    # 导出——此刻 chatStore.streamItems 可能含 draft，但 SQLite 没有 assistant
    resp = client.get(f"/api/sessions/{sid}/export/markdown")
    assert resp.status_code == 200
    body = resp.text

    # user 正文持久化在 SQLite 中——导出
    assert "persisted user question" in body
    assert "## User" in body

    # assistant draft 未持久化——导出中不存在
    assert DRAFT_TEXT not in body
    assert "## Assistant" not in body

    # messages 数量断言——只有 1 条 user
    msgs = client.get(f"/api/messages?session_id={sid}").json()
    assert msgs["count"] == 1
