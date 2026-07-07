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
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
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
    assert data["count"] == 2
    names = [f["name"] for f in data["files"]]
    assert set(names) == {"a.txt", "b.txt"}


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
    assert r1["count"] == 1 and r1["files"][0]["name"] == "a.txt"
    assert r2["count"] == 1 and r2["files"][0]["name"] == "b.txt"


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
    assert client.get(f"/api/sessions/{sid}/files").json()["count"] == 0


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
    assert client.get(f"/api/sessions/{sid}/files").json()["count"] == 1


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

    # path 应在 uploads/sid/file_id/ 下
    from pathlib import Path
    target = Path(ref["path"]).resolve()
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
    assert client.get(f"/api/sessions/{sid}/files").json()["count"] == 2

    # 删 session
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["deleted_files"] == 2

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
