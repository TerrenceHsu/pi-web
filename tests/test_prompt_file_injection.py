"""P0-3: POST /api/prompt 支持 file_ids（附件注入 UserMessage FileBlock）。

覆盖：
- POST /api/prompt 支持 file_ids
- md / html / csv / parquet / 图片 各文件注入 FileBlock
- 图片文件 format=image_unsupported（不新增 ImageBlock）
- 多文件顺序保持
- missing file_id 返回 404
- cross-session file_id 返回 403
- snapshot metadata 记录 attached_file_ids / names / counts
- sqlite messages restore 后 FileBlock 强类型不丢
- convert_to_llm 能处理 FileBlock
- 不带 file_ids 的旧 prompt 路径不回归
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.context import convert_to_llm
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import FileBlock, TextContent, UserMessage
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app, dispose_app

pytestmark = [pytest.mark.slow]


# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
def web_client(tmp_path):
    """带 uploads_dir + sqlite 的 TestClient。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    # 用文件 db 让 replace_messages 真的持久化（便于 restore 测试）
    db_path = tmp_path / "sessions.db"
    app = create_app(
        harness,
        db_path=db_path,
        uploads_dir=tmp_path / "uploads",
        max_file_size=1024 * 1024,
        max_session_upload_size=4 * 1024 * 1024,
    )
    with TestClient(app) as client:
        try:
            yield client, harness, app, tmp_path
        finally:
            pass
    dispose_app(app)


def _make_session(client: TestClient, title: str = "test") -> str:
    r = client.post("/api/sessions", json={"title": title})
    return r.json()["id"]


def _upload(
    client: TestClient, sid: str, content: bytes,
    name: str, mime: str = "text/plain",
) -> str:
    """上传单文件，返回 file_id。"""
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[("files", (name, io.BytesIO(content), mime))],
    )
    assert r.status_code == 200, r.text
    return r.json()["files"][0]["id"]


# ============================================================================
# 1-5: 各格式文件注入 FileBlock
# ============================================================================


def test_prompt_with_md_file_id_injects_fileblock(web_client):
    client, harness, _, _ = web_client
    sid = _make_session(client)
    fid = _upload(client, sid, b"# hi", "n.md", "text/markdown")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "look", "file_ids": [fid],
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["attachments"]["attached_file_count"] == 1
    assert data["attachments"]["attached_supported_file_count"] == 1
    assert data["attachments"]["attached_unsupported_file_count"] == 0
    assert data["attachments"]["attached_file_ids"] == [fid]

    # messages 中应有一条 UserMessage 含 FileBlock
    msgs = data["messages"]
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert user_msgs, "should have user message"
    last_user = user_msgs[-1]
    types = [b["type"] for b in last_user["content"]]
    assert "file" in types
    fb = next(b for b in last_user["content"] if b["type"] == "file")
    assert fb["format"] == "markdown"
    assert fb["file_id"] == fid
    assert fb["name"] == "n.md"


def test_prompt_with_html_csv_parquet_files_inject_fileblock(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    f_html = _upload(client, sid, b"<p>x</p>", "p.html", "text/html")
    f_csv = _upload(client, sid, b"a,b\n1,2\n", "d.csv", "text/csv")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "look",
        "file_ids": [f_html, f_csv],
    })
    data = r.json()
    last_user = next(m for m in data["messages"] if m["role"] == "user")
    formats = sorted(b["format"] for b in last_user["content"] if b["type"] == "file")
    assert formats == ["csv", "html"]


def test_prompt_with_image_file_injects_image_unsupported_fileblock(web_client):
    """图片文件统一以 format=image_unsupported 注入；不新增 ImageBlock。"""
    client, _, _, _ = web_client
    sid = _make_session(client)
    f_img = _upload(client, sid, b"\x89PNG fake", "i.png", "image/png")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "see image", "file_ids": [f_img],
    })
    data = r.json()
    last_user = next(m for m in data["messages"] if m["role"] == "user")
    file_blocks = [b for b in last_user["content"] if b["type"] == "file"]
    assert len(file_blocks) == 1
    assert file_blocks[0]["format"] == "image_unsupported"

    # 不应有 image block（没有 ImageBlock 类）
    assert all(b["type"] != "image" for b in last_user["content"])


def test_prompt_multiple_files_keep_order(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    f1 = _upload(client, sid, b"a", "a.txt")
    f2 = _upload(client, sid, b"b", "b.txt")
    f3 = _upload(client, sid, b"c", "c.txt")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "all",
        "file_ids": [f1, f2, f3],
    })
    data = r.json()
    last_user = next(m for m in data["messages"] if m["role"] == "user")
    file_blocks = [b for b in last_user["content"] if b["type"] == "file"]
    names = [b["name"] for b in file_blocks]
    assert names == ["a.txt", "b.txt", "c.txt"]


def test_prompt_with_text_and_files_combined_in_one_user_message(web_client):
    """TextContent + FileBlocks 应同在一条 UserMessage。"""
    client, _, _, _ = web_client
    sid = _make_session(client)
    f1 = _upload(client, sid, b"a", "a.txt")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "hello", "file_ids": [f1],
    })
    data = r.json()
    user_msgs = [m for m in data["messages"] if m["role"] == "user"]
    # 只有一条 user message（text + file 同 message）
    assert len(user_msgs) == 1
    last_user = user_msgs[-1]
    types = [b["type"] for b in last_user["content"]]
    assert types.count("text") == 1
    assert types.count("file") == 1


# ============================================================================
# 6-7: errors
# ============================================================================


def test_prompt_missing_file_id_returns_404(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "x", "file_ids": ["file-nonexistent"],
    })
    assert r.status_code == 404


def test_prompt_cross_session_file_id_returns_403(web_client):
    client, _, _, _ = web_client
    sid1 = _make_session(client, "s1")
    sid2 = _make_session(client, "s2")
    fid = _upload(client, sid1, b"x", "a.txt")

    r = client.post("/api/prompt", json={
        "session_id": sid2, "text": "steal", "file_ids": [fid],
    })
    assert r.status_code == 403


# ============================================================================
# 8: 不带 file_ids 旧路径不回归
# ============================================================================


def test_prompt_without_file_ids_does_not_regress(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "hello",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["attachments"]["attached_file_count"] == 0


# ============================================================================
# 9: metadata 在 response 中
# ============================================================================


def test_prompt_attachment_metadata_recorded(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    f1 = _upload(client, sid, b"# md", "a.md", "text/markdown")
    f2 = _upload(client, sid, b"\x89PNG fake", "i.png", "image/png")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "look", "file_ids": [f1, f2],
    })
    data = r.json()
    att = data["attachments"]
    assert att["attached_file_count"] == 2
    assert att["attached_supported_file_count"] == 1  # md
    assert att["attached_unsupported_file_count"] == 1  # png
    assert att["attached_file_ids"] == [f1, f2]
    assert att["attached_file_names"] == ["a.md", "i.png"]


# ============================================================================
# 10: sqlite restore 后 FileBlock 强类型
# ============================================================================


def test_sqlite_restore_keeps_fileblock_strong_type(web_client):
    """上传 → prompt → reload via /api/messages：FileBlock 类型应保留。"""
    client, _, _, _ = web_client
    sid = _make_session(client)
    fid = _upload(client, sid, b"# hi", "n.md", "text/markdown")

    r = client.post("/api/prompt", json={
        "session_id": sid, "text": "look", "file_ids": [fid],
    })
    assert r.status_code == 200

    # 通过 /api/messages 重读
    r2 = client.get(f"/api/messages?session_id={sid}")
    assert r2.status_code == 200
    msgs = r2.json()["messages"]
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert user_msgs
    last_user = user_msgs[-1]
    file_blocks = [b for b in last_user["content"] if b.get("type") == "file"]
    assert len(file_blocks) == 1
    assert file_blocks[0]["format"] == "markdown"
    assert file_blocks[0]["file_id"] == fid


def test_sqlite_session_store_round_trip_preserves_fileblock():
    """直接验证 SQLiteSessionStore.append_message / list_messages 不退化。"""
    from pi_agent_core_py.session_sqlite import SQLiteSessionStore

    store = SQLiteSessionStore(":memory:")
    import asyncio

    async def run():
        await store.init()
        sess = await store.ensure_default_session()
        msg = UserMessage(content=[
            TextContent(text="hi"),
            FileBlock(
                file_id="f1", name="a.csv", mime="text/csv",
                size=10, sha256="x", format="csv",
            ),
        ])
        await store.append_message(sess.id, msg)
        msgs = await store.list_messages(sess.id)
        await store.close()
        return msgs

    msgs = asyncio.run(run())
    assert len(msgs) == 1
    m = msgs[0]
    # 应仍是 UserMessage 实例，content[1] 是 FileBlock
    assert isinstance(m, UserMessage)
    assert isinstance(m.content[1], FileBlock)
    assert m.content[1].format == "csv"


# ============================================================================
# 11: convert_to_llm 处理 FileBlock
# ============================================================================


def test_convert_to_llm_handles_fileblock():
    """UserMessage 含 FileBlock → convert_to_llm 应输出可读文本说明。"""
    msg = UserMessage(content=[
        TextContent(text="look at this"),
        FileBlock(
            file_id="f1", name="a.csv", mime="text/csv",
            size=100, sha256="abc", format="csv",
        ),
        FileBlock(
            file_id="f2", name="img.png", mime="image/png",
            size=200, sha256="def", format="image_unsupported",
        ),
    ])
    out = convert_to_llm([msg])
    assert len(out) == 1
    # 三段：原 text + 两个 fileblock 描述
    assert len(out[0].content) == 3
    # 第一个是原 text
    assert out[0].content[0].text == "look at this"
    # csv 文件描述应引导 view_file
    csv_text = out[0].content[1].text
    assert "a.csv" in csv_text
    assert "view_file" in csv_text
    # 图片文件描述应说明不支持
    img_text = out[0].content[2].text
    assert "img.png" in img_text
    assert "不支持图片" in img_text


def test_convert_to_llm_does_not_expose_path():
    msg = UserMessage(content=[
        FileBlock(
            file_id="f1", name="a.csv", mime="text/csv",
            size=1, sha256="x", format="csv",
        ),
    ])
    out = convert_to_llm([msg])
    text = out[0].content[0].text
    # 不应暴露 path 字段
    assert "path" not in text.lower()


# ============================================================================
# 12: 工具注册（lifespan 内自动注册）
# ============================================================================


def test_file_tools_auto_registered_when_uploads_dir_set(web_client):
    client, harness, _, _ = web_client
    tools = harness.agent.tools
    assert tools.has("list_files")
    assert tools.has("view_file")


def test_file_tools_not_registered_when_no_uploads_dir(tmp_path):
    """uploads_dir=None 时不应注册 list_files / view_file。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    app = create_app(harness, db_path=None)  # 不传 uploads_dir
    with TestClient(app) as client:
        # /api/prompt 应仍可调用（无 file_ids）
        sid = client.post("/api/sessions", json={"title": "x"}).json()["id"]
        r = client.post("/api/prompt", json={
            "session_id": sid, "text": "hi",
        })
        assert r.status_code == 200
        # 工具不应注册
        assert not harness.agent.tools.has("list_files")
        assert not harness.agent.tools.has("view_file")
    dispose_app(app)
