"""P0-2: Web endpoint 测试。

覆盖：
- POST /api/sessions/{sid}/files 上传（单 / 多）
- GET /api/sessions/{sid}/files 列出
- GET /api/sessions/{sid}/files/{fid} 下载
- GET /api/files/{fid}?session_id=... 兼容下载
- DELETE /api/files/{fid}?session_id=... 兼容删除
- DELETE /api/sessions/{sid}/files/{fid} 推荐
- 跨 session 访问 403
- 不存在 session / file 404
- 缺 session_id 400
- 单文件超限 413
- session 总量超限 413
- 路径穿越 sanitize
- 删 session 级联删 uploads
- FileRef sha256 / mime 正确
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import ToolCall
from pi_agent_core_py.model_client import (
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    ToolCallEvent,
)
from pi_agent_core_py.web.app import create_app, dispose_app

# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
def web_client(tmp_path):
    """带 uploads_dir 的 TestClient。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    app = create_app(
        harness,
        db_path=None,
        uploads_dir=tmp_path / "uploads",
        max_file_size=4096,           # 4 KB（允许测 session 总量用 1500 字节文件）
        max_session_upload_size=2048,  # 2 KB session 总量
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


def _upload_payload(content: bytes, filename: str = "test.txt", content_type: str = "text/plain"):
    """构造 multipart 上传 tuple。"""
    return ("files", (filename, io.BytesIO(content), content_type))


def _ordinary_files(payload):
    return [
        f for f in payload["files"]
        if f.get("purpose") not in {"agent_instructions", "memory"}
    ]


def _agent_instructions_file(payload):
    matches = [f for f in payload["files"] if f.get("purpose") == "agent_instructions"]
    assert len(matches) == 1
    return matches[0]


def _memory_file(payload):
    matches = [f for f in payload["files"] if f.get("purpose") == "memory"]
    assert len(matches) == 1
    return matches[0]


# ============================================================================
# 1-2: upload single / multiple
# ============================================================================


def test_upload_single_file(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"hello", "test.txt")],
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert len(data["files"]) == 1
    ref = data["files"][0]
    assert ref["name"] == "test.txt"
    assert ref["size"] == 5
    assert ref["mime"] == "text/plain"
    assert ref["sha256"]
    assert ref["id"].startswith("file-")


def test_default_and_new_sessions_have_eager_folders(web_client):
    client, _, app, tmp_path = web_client
    default_sid = app.state.web.current_session_id
    assert default_sid
    assert (tmp_path / "uploads" / default_sid).is_dir()

    sid = _make_session(client, "folder-backed")
    assert (tmp_path / "uploads" / sid).is_dir()
    listed = client.get(f"/api/sessions/{sid}/files").json()
    assert listed["count"] == 2
    agent_md = _agent_instructions_file(listed)
    memory_md = _memory_file(listed)
    assert agent_md["logical_path"] == "AGENT.md"
    assert agent_md["origin"] == "system"
    assert memory_md["logical_path"] == "Memory.md"
    assert memory_md["origin"] == "system"
    assert "path" not in agent_md
    assert "path" not in memory_md


def test_agent_md_is_editable_versioned_and_protected(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client, "agent-instructions")
    listed = client.get(f"/api/sessions/{sid}/files").json()
    agent_md = _agent_instructions_file(listed)

    original = client.get(
        f"/api/sessions/{sid}/files/{agent_md['id']}"
    )
    assert original.status_code == 200
    assert b"# AGENT.md" in original.content

    custom = "# AGENT.md\n\nAlways answer with a concise checklist.\n"
    updated = client.put(
        f"/api/sessions/{sid}/files/{agent_md['id']}/content",
        json={"content": custom, "expected_sha256": agent_md["sha256"]},
    )
    assert updated.status_code == 200
    updated_file = updated.json()["file"]
    assert updated_file["sha256"] != agent_md["sha256"]
    assert updated_file["origin"] == "user"

    stale = client.put(
        f"/api/sessions/{sid}/files/{agent_md['id']}/content",
        json={"content": "stale", "expected_sha256": agent_md["sha256"]},
    )
    assert stale.status_code == 409
    assert client.delete(
        f"/api/sessions/{sid}/files/{agent_md['id']}"
    ).status_code == 409
    memory_md = _memory_file(listed)
    assert client.delete(
        f"/api/sessions/{sid}/files/{memory_md['id']}"
    ).status_code == 409

    ordinary = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"read only", "ordinary.txt")],
    ).json()["files"][0]
    forbidden = client.put(
        f"/api/sessions/{sid}/files/{ordinary['id']}/content",
        json={"content": "changed", "expected_sha256": ordinary["sha256"]},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == (
        "only AGENT.md and Memory.md are editable here"
    )


def test_agent_md_is_loaded_into_the_current_session_prompt(tmp_path):
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    harness = AgentHarness(Agent(system_prompt="", client=fake))
    app = create_app(
        harness,
        db_path=tmp_path / "workspace.sqlite",
        uploads_dir=tmp_path / "uploads",
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "prompt-agent-md"}).json()["id"]
        agent_md = _agent_instructions_file(
            client.get(f"/api/sessions/{sid}/files").json()
        )
        instruction = "Use the marker SESSION_AGENT_MD_MARKER in your reasoning."
        edited = client.put(
            f"/api/sessions/{sid}/files/{agent_md['id']}/content",
            json={
                "content": f"# AGENT.md\n\n{instruction}\n",
                "expected_sha256": agent_md["sha256"],
            },
        )
        assert edited.status_code == 200
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "hello"},
        ).status_code == 200
        assert fake.last_system_prompt is not None
        assert instruction in fake.last_system_prompt
        assert "<session_agent_md>" in fake.last_system_prompt
        assert "对话助手" in fake.last_system_prompt
    dispose_app(app)


def test_conversation_and_agent_md_persist_across_workspace_reopen(tmp_path):
    db_path = tmp_path / "workspace.sqlite"
    uploads_dir = tmp_path / "uploads"
    first_fake = FakeClient([
        [TextDeltaEvent(delta="persisted answer"), DoneEvent(stop_reason="stop")]
    ])
    first_app = create_app(
        AgentHarness(Agent(system_prompt="", client=first_fake)),
        db_path=db_path,
        uploads_dir=uploads_dir,
    )
    with TestClient(first_app) as client:
        sid = client.post("/api/sessions", json={"title": "persistent"}).json()["id"]
        agent_md = _agent_instructions_file(
            client.get(f"/api/sessions/{sid}/files").json()
        )
        custom = "# AGENT.md\n\nPersist this instruction across login and restart.\n"
        assert client.put(
            f"/api/sessions/{sid}/files/{agent_md['id']}/content",
            json={"content": custom, "expected_sha256": agent_md["sha256"]},
        ).status_code == 200
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "remember this conversation"},
        ).status_code == 200
    dispose_app(first_app)

    second_app = create_app(
        AgentHarness(Agent(system_prompt="", client=FakeClient([]))),
        db_path=db_path,
        uploads_dir=uploads_dir,
    )
    with TestClient(second_app) as client:
        sessions = client.get("/api/sessions").json()["sessions"]
        assert any(session["id"] == sid for session in sessions)
        messages = client.get(f"/api/messages?session_id={sid}").json()
        assert messages["count"] == 2
        assert "persisted answer" in str(messages["messages"])
        listed = client.get(f"/api/sessions/{sid}/files").json()
        agent_md = _agent_instructions_file(listed)
        assert listed["count"] == 2
        downloaded = client.get(f"/api/sessions/{sid}/files/{agent_md['id']}")
        assert downloaded.text == custom
        assert _memory_file(listed)["logical_path"] == "Memory.md"
    dispose_app(second_app)


def test_agent_can_create_file_in_active_session_and_user_can_download(tmp_path):
    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="tc-write",
                name="write_file",
                arguments={
                    "filename": "agent-result.md",
                    "content": "# Agent result\n\nCreated inside this Session.",
                },
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        [TextDeltaEvent(delta="Created the file."), DoneEvent(stop_reason="stop")],
    ])
    harness = AgentHarness(Agent(system_prompt="", client=fake))
    app = create_app(
        harness,
        db_path=None,
        uploads_dir=tmp_path / "agent-uploads",
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "agent-files"}).json()["id"]
        response = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "Create a markdown result file."},
        )
        assert response.status_code == 200

        listed = client.get(f"/api/sessions/{sid}/files").json()
        assert listed["count"] == 3
        generated = _ordinary_files(listed)[0]
        assert generated["name"] == "agent-result.md"
        downloaded = client.get(
            f"/api/sessions/{sid}/files/{generated['id']}"
        )
        assert downloaded.status_code == 200
        assert b"Created inside this Session" in downloaded.content
    dispose_app(app)


def test_upload_multiple_files(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[
            _upload_payload(b"aaa", "a.txt"),
            _upload_payload(b"bbb", "b.txt"),
            _upload_payload(b"ccc", "c.txt"),
        ],
    )
    assert r.status_code == 200
    assert r.json()["count"] == 3


# ============================================================================
# 3: list session files
# ============================================================================


def test_list_session_files(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"a", "a.txt")],
    )
    client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"b", "b.txt")],
    )
    r = client.get(f"/api/sessions/{sid}/files")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 4
    names = [f["name"] for f in data["files"]]
    assert set(names) == {"AGENT.md", "Memory.md", "a.txt", "b.txt"}


def test_list_files_isolated_by_session(web_client):
    client, _, _, _ = web_client
    sid1 = _make_session(client)
    sid2 = _make_session(client)
    client.post(
        f"/api/sessions/{sid1}/files",
        files=[_upload_payload(b"a", "a.txt")],
    )
    client.post(
        f"/api/sessions/{sid2}/files",
        files=[_upload_payload(b"b", "b.txt")],
    )
    r1 = client.get(f"/api/sessions/{sid1}/files").json()
    r2 = client.get(f"/api/sessions/{sid2}/files").json()
    assert [f["name"] for f in _ordinary_files(r1)] == ["a.txt"]
    assert [f["name"] for f in _ordinary_files(r2)] == ["b.txt"]
    assert _agent_instructions_file(r1)["session_id"] == sid1
    assert _agent_instructions_file(r2)["session_id"] == sid2


# ============================================================================
# 4-5: download endpoints
# ============================================================================


def test_download_via_session_endpoint(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    upload_resp = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"hello world", "test.txt", "text/plain")],
    ).json()
    fid = upload_resp["files"][0]["id"]

    r = client.get(f"/api/sessions/{sid}/files/{fid}")
    assert r.status_code == 200
    assert r.content == b"hello world"
    assert r.headers["content-type"].startswith("text/plain")


def test_download_via_compat_endpoint(web_client):
    """GET /api/files/{fid}?session_id=... 兼容入口。"""
    client, _, _, _ = web_client
    sid = _make_session(client)
    upload_resp = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x")],
    ).json()
    fid = upload_resp["files"][0]["id"]

    r = client.get(f"/api/files/{fid}?session_id={sid}")
    assert r.status_code == 200
    assert r.content == b"x"


def test_download_without_session_id_returns_400(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    upload_resp = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x")],
    ).json()
    fid = upload_resp["files"][0]["id"]

    r = client.get(f"/api/files/{fid}")
    assert r.status_code == 400


# ============================================================================
# 6-7: delete endpoints
# ============================================================================


def test_delete_via_compat_endpoint(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    fid = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x")],
    ).json()["files"][0]["id"]

    r = client.delete(f"/api/files/{fid}?session_id={sid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # 再列出应为空
    remaining = client.get(f"/api/sessions/{sid}/files").json()
    assert remaining["count"] == 2
    _agent_instructions_file(remaining)
    _memory_file(remaining)


def test_delete_via_session_endpoint(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    fid = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x")],
    ).json()["files"][0]["id"]

    r = client.delete(f"/api/sessions/{sid}/files/{fid}")
    assert r.status_code == 200


def test_delete_without_session_id_returns_400(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    fid = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x")],
    ).json()["files"][0]["id"]

    r = client.delete(f"/api/files/{fid}")
    assert r.status_code == 400


# ============================================================================
# 8-13: errors
# ============================================================================


def test_upload_to_missing_session_returns_404(web_client):
    client, _, _, _ = web_client
    r = client.post(
        "/api/sessions/ghost/files",
        files=[_upload_payload(b"x")],
    )
    assert r.status_code == 404


def test_download_missing_file_returns_404(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    r = client.get(f"/api/sessions/{sid}/files/ghost")
    assert r.status_code == 404


def test_cross_session_download_returns_403(web_client):
    client, _, _, _ = web_client
    sid1 = _make_session(client, "s1")
    sid2 = _make_session(client, "s2")
    fid = client.post(
        f"/api/sessions/{sid1}/files",
        files=[_upload_payload(b"x")],
    ).json()["files"][0]["id"]

    # 用 sid2 调 sid1 的 file
    r = client.get(f"/api/sessions/{sid2}/files/{fid}")
    assert r.status_code == 403


def test_cross_session_delete_returns_403(web_client):
    client, _, _, _ = web_client
    sid1 = _make_session(client, "s1")
    sid2 = _make_session(client, "s2")
    fid = client.post(
        f"/api/sessions/{sid1}/files",
        files=[_upload_payload(b"x")],
    ).json()["files"][0]["id"]

    r = client.delete(f"/api/sessions/{sid2}/files/{fid}")
    assert r.status_code == 403


def test_cross_session_compat_download_returns_403(web_client):
    client, _, _, _ = web_client
    sid1 = _make_session(client, "s1")
    sid2 = _make_session(client, "s2")
    fid = client.post(
        f"/api/sessions/{sid1}/files",
        files=[_upload_payload(b"x")],
    ).json()["files"][0]["id"]

    r = client.get(f"/api/files/{fid}?session_id={sid2}")
    assert r.status_code == 403


# ============================================================================
# 14-15: size limits
# ============================================================================


def test_upload_single_file_over_max_returns_413(web_client):
    """max_file_size=4096 → 上传 8192 字节应 413。"""
    client, _, _, _ = web_client
    sid = _make_session(client)
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x" * 8192, "big.bin")],
    )
    assert r.status_code == 413
    assert r.json()["count"] == 0
    assert r.json()["errors"][0]["error_type"] == "FileTooLargeError"


def test_upload_session_total_over_limit_returns_413(web_client):
    """max_session_upload_size=2048 → 第二次上传超过总量应 413。"""
    client, _, _, _ = web_client
    sid = _make_session(client)
    # 第一次 1500 字节 OK
    r1 = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x" * 1500, "a.bin")],
    )
    assert r1.status_code == 200
    # 第二次 1500 字节 → 1500 + 1500 = 3000 > 2048
    r2 = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"y" * 1500, "b.bin")],
    )
    assert r2.status_code == 413
    assert r2.json()["errors"][0]["error_type"] == "SessionStorageLimitError"
    # 第一次的文件应仍在
    assert client.get(f"/api/sessions/{sid}/files").json()["count"] == 3


# ============================================================================
# 16-17: filename sanitize
# ============================================================================


def test_upload_path_traversal_filename_sanitized(web_client):
    """../evil.txt 被 sanitize 为 evil.txt——文件落在 session 目录内。"""
    client, _, app, tmp_path = web_client
    sid = _make_session(client)
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x", "../../../evil.txt")],
    )
    assert r.status_code == 200
    ref = r.json()["files"][0]
    assert ref["name"] == "evil.txt"

    # API 不暴露物理 path；磁盘内容仍限制在 uploads/sid/file_id/ 下。
    from pathlib import Path
    assert "path" not in ref
    target = Path(tmp_path / "uploads" / sid / ref["id"] / "evil.txt").resolve()
    assert target.is_file()
    uploads_root = (tmp_path / "uploads").resolve()
    assert target.is_relative_to(uploads_root)
    # 不应逃出 uploads_root
    assert ".." not in target.relative_to(uploads_root).parts


def test_upload_whitespace_filename_sanitized_to_default(web_client):
    """filename 只含路径分隔符被 sanitize 后空 → fallback upload.bin。

    注：multipart 协议层会拒绝完全空 filename（422），所以这里用全部由
    路径分隔符组成的 filename 测 sanitize fallback。
    """
    client, _, _, _ = web_client
    sid = _make_session(client)
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"x", "../../", "application/octet-stream")],
    )
    assert r.status_code == 200
    ref = r.json()["files"][0]
    assert ref["name"] == "upload.bin"


# ============================================================================
# 18: 级联删除
# ============================================================================


def test_delete_session_cascades_uploads(web_client):
    client, _, _, tmp_path = web_client
    sid = _make_session(client, "to-delete")
    client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"a", "a.txt")],
    )
    client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"b", "b.txt")],
    )
    assert client.get(f"/api/sessions/{sid}/files").json()["count"] == 4

    # 删 session
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["deleted_files"] == 4

    # uploads/sid 目录应被删
    session_dir = tmp_path / "uploads" / sid
    assert not session_dir.exists()

    # 再 GET files 应 404（session 不存在）
    assert client.get(f"/api/sessions/{sid}/files").status_code == 404


# ============================================================================
# 19-20: sha256 / mime
# ============================================================================


def test_upload_returns_correct_sha256(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"hello world", "hello.txt", "text/plain")],
    )
    assert r.json()["files"][0]["sha256"] == expected


def test_upload_returns_correct_mime(web_client):
    client, _, _, _ = web_client
    sid = _make_session(client)
    r = client.post(
        f"/api/sessions/{sid}/files",
        files=[_upload_payload(b"\x89PNG fake", "image.png", "image/png")],
    )
    assert r.json()["files"][0]["mime"] == "image/png"
