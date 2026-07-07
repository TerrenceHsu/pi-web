"""P0-2: VirtualFileStore 单元测试。

覆盖纯 store 行为（不走 FastAPI）：
- init / save / get / list / delete
- sanitize_filename
- 路径穿越防护
- 大小 / 总量限制
- metadata 持久化
- 删除 session 级联
"""
from __future__ import annotations

import io

import pytest
from fastapi import UploadFile

from pi_agent_core_py.web.files import (
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_MAX_SESSION_SIZE,
    FileAccessDeniedError,
    FileRef,
    FileTooLargeError,
    SessionStorageLimitError,
    UnsafeFilenameError,
    VirtualFileNotFoundError,
    VirtualFileStore,
    sanitize_filename,
)

# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    s = VirtualFileStore(tmp_path / "uploads")
    await s.init()
    try:
        yield s
    finally:
        pass  # 纯文件 IO，无 close


def _upload(content: bytes, filename: str = "test.txt", content_type: str | None = None):
    """构造 UploadFile。

    新版 starlette UploadFile 不接 content_type kwarg；用 Headers 传。
    """
    from starlette.datastructures import Headers

    headers = Headers({"content-type": content_type} if content_type else {})
    return UploadFile(
        filename=filename,
        file=io.BytesIO(content),
        headers=headers,
    )


# ============================================================================
# sanitize_filename
# ============================================================================


def test_sanitize_filename_basic():
    assert sanitize_filename("hello.txt") == "hello.txt"


def test_sanitize_filename_strips_path():
    assert sanitize_filename("../evil.txt") == "evil.txt"
    assert sanitize_filename("/etc/passwd") == "passwd"
    assert sanitize_filename("..\\windows\\file.txt") == "file.txt"


def test_sanitize_filename_empty_uses_default():
    assert sanitize_filename("") == "upload.bin"
    assert sanitize_filename(None) == "upload.bin"  # type: ignore[arg-type]


def test_sanitize_filename_strips_control_chars():
    cleaned = sanitize_filename("file\x00.txt")
    assert "\x00" not in cleaned
    assert "file" in cleaned


def test_sanitize_filename_truncates_long():
    long_name = "a" * 500 + ".txt"
    cleaned = sanitize_filename(long_name)
    assert len(cleaned) <= 255
    assert cleaned.endswith(".txt")


def test_sanitize_filename_keeps_chinese():
    cleaned = sanitize_filename("测试文件.md")
    assert "测试文件" in cleaned


# ============================================================================
# init
# ============================================================================


@pytest.mark.asyncio
async def test_init_creates_root_dir(tmp_path):
    target = tmp_path / "uploads"
    assert not target.exists()
    s = VirtualFileStore(target)
    await s.init()
    assert target.exists()
    assert target.is_dir()


@pytest.mark.asyncio
async def test_init_idempotent(tmp_path):
    s = VirtualFileStore(tmp_path / "uploads")
    await s.init()
    await s.init()  # 不抛错


# ============================================================================
# save / get / list
# ============================================================================


@pytest.mark.asyncio
async def test_save_returns_fileref(store):
    ref = await store.save("sess-1", _upload(b"hello", "test.txt", "text/plain"))
    assert isinstance(ref, FileRef)
    assert ref.session_id == "sess-1"
    assert ref.name == "test.txt"
    assert ref.size == 5
    assert ref.mime == "text/plain"
    assert ref.sha256
    assert ref.id.startswith("file-")
    # path 存在
    from pathlib import Path

    assert Path(ref.path).is_file()  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_save_computes_sha256(store):
    content = b"hello world"
    expected_sha = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    ref = await store.save("sess-1", _upload(content, "hello.txt"))
    assert ref.sha256 == expected_sha


@pytest.mark.asyncio
async def test_save_persists_metadata_json(store, tmp_path):
    ref = await store.save("sess-1", _upload(b"hi", "a.txt"))
    metadata_path = tmp_path / "uploads" / "sess-1" / ref.id / "metadata.json"
    assert metadata_path.is_file()
    import json
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["id"] == ref.id
    assert payload["session_id"] == "sess-1"
    assert payload["name"] == "a.txt"


@pytest.mark.asyncio
async def test_get_returns_fileref(store):
    saved = await store.save("sess-1", _upload(b"hi", "a.txt"))
    fetched = await store.get(saved.id)
    assert fetched is not None
    assert fetched.id == saved.id
    assert fetched.name == "a.txt"


@pytest.mark.asyncio
async def test_get_missing_returns_none(store):
    assert await store.get("ghost") is None


@pytest.mark.asyncio
async def test_get_for_session_strict(store):
    """get_for_session 强校验 session_id。"""
    ref = await store.save("sess-1", _upload(b"hi", "a.txt"))
    # 正确 session → OK
    fetched = await store.get_for_session("sess-1", ref.id)
    assert fetched.id == ref.id
    # 错误 session → FileAccessDeniedError
    with pytest.raises(FileAccessDeniedError):
        await store.get_for_session("sess-other", ref.id)
    # 不存在 file → VirtualFileNotFoundError
    with pytest.raises(VirtualFileNotFoundError):
        await store.get_for_session("sess-1", "ghost")


@pytest.mark.asyncio
async def test_list_session_orders_by_created_at(store):
    """list_session 按 created_at 升序。"""
    r1 = await store.save("sess-1", _upload(b"a", "a.txt"))
    await asyncio_sleep_ms(10)
    r2 = await store.save("sess-1", _upload(b"b", "b.txt"))
    files = await store.list_session("sess-1")
    assert len(files) == 2
    assert files[0].id == r1.id
    assert files[1].id == r2.id


@pytest.mark.asyncio
async def test_list_session_isolated(store):
    """list_session 只返回该 session 文件。"""
    await store.save("sess-1", _upload(b"a", "a.txt"))
    await store.save("sess-2", _upload(b"b", "b.txt"))
    s1 = await store.list_session("sess-1")
    s2 = await store.list_session("sess-2")
    assert len(s1) == 1
    assert len(s2) == 1
    assert s1[0].name == "a.txt"
    assert s2[0].name == "b.txt"


@pytest.mark.asyncio
async def test_list_session_missing_returns_empty(store):
    assert await store.list_session("ghost") == []


@pytest.mark.asyncio
async def test_session_total_size(store):
    await store.save("sess-1", _upload(b"12345", "a.txt"))
    await store.save("sess-1", _upload(b"abc", "b.txt"))
    assert await store.session_total_size("sess-1") == 8


# ============================================================================
# delete
# ============================================================================


@pytest.mark.asyncio
async def test_delete_for_session(store):
    ref = await store.save("sess-1", _upload(b"hi", "a.txt"))
    deleted = await store.delete_for_session("sess-1", ref.id)
    assert deleted.id == ref.id
    # 再 list 应空
    assert await store.list_session("sess-1") == []


@pytest.mark.asyncio
async def test_delete_for_session_missing(store):
    with pytest.raises(VirtualFileNotFoundError):
        await store.delete_for_session("sess-1", "ghost")


@pytest.mark.asyncio
async def test_delete_for_session_cross_session_denied(store):
    ref = await store.save("sess-1", _upload(b"hi", "a.txt"))
    with pytest.raises(FileAccessDeniedError):
        await store.delete_for_session("sess-other", ref.id)


@pytest.mark.asyncio
async def test_delete_session_files_removes_all(store, tmp_path):
    await store.save("sess-1", _upload(b"a", "a.txt"))
    await store.save("sess-1", _upload(b"b", "b.txt"))
    await store.save("sess-2", _upload(b"c", "c.txt"))

    deleted = await store.delete_session_files("sess-1")
    assert deleted == 2
    # sess-1 目录被删
    assert not (tmp_path / "uploads" / "sess-1").exists()
    # sess-2 仍在
    assert (tmp_path / "uploads" / "sess-2").is_dir()
    assert len(await store.list_session("sess-2")) == 1


@pytest.mark.asyncio
async def test_delete_session_files_missing_session_returns_zero(store):
    assert await store.delete_session_files("ghost") == 0


# ============================================================================
# 大小 / 总量限制
# ============================================================================


@pytest.mark.asyncio
async def test_save_rejects_file_over_max_size(tmp_path):
    s = VirtualFileStore(tmp_path / "uploads", max_file_size=10)
    await s.init()
    # 11 字节文件超过 10 字节
    with pytest.raises(FileTooLargeError):
        await s.save("sess-1", _upload(b"x" * 11, "big.bin"))


@pytest.mark.asyncio
async def test_save_partial_cleanup_after_too_large(tmp_path):
    """超限时已写部分应被清理。"""
    s = VirtualFileStore(tmp_path / "uploads", max_file_size=5)
    await s.init()
    with pytest.raises(FileTooLargeError):
        await s.save("sess-1", _upload(b"x" * 100, "big.bin"))
    # 不应留 file_dir
    files = await s.list_session("sess-1")
    assert files == []


@pytest.mark.asyncio
async def test_save_rejects_session_total_over_limit(tmp_path):
    s = VirtualFileStore(
        tmp_path / "uploads",
        max_file_size=1000,
        max_session_size=15,
    )
    await s.init()
    # 第一次 10 字节 OK
    await s.save("sess-1", _upload(b"x" * 10, "a.bin"))
    # 第二次 10 字节 + 第一次 10 = 20 > 15 → 拒
    with pytest.raises(SessionStorageLimitError):
        await s.save("sess-1", _upload(b"y" * 10, "b.bin"))


# ============================================================================
# 路径穿越防护
# ============================================================================


@pytest.mark.asyncio
async def test_save_path_traversal_filename_sanitized(store, tmp_path):
    """../evil.txt 被 sanitize 为 evil.txt——文件落在 session 目录内。"""
    ref = await store.save("sess-1", _upload(b"x", "../../../evil.txt"))
    assert ref.name == "evil.txt"
    # 路径必须在 uploads/sess-1/file-xxx/ 下
    from pathlib import Path

    target = Path(ref.path).resolve()  # noqa: ASYNC240
    assert target.is_relative_to((tmp_path / "uploads").resolve())  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_unsafe_session_id_rejected(store):
    """session_id 含 ../ → UnsafeFilenameError。"""
    with pytest.raises(UnsafeFilenameError):
        await store.save("../escape", _upload(b"x", "a.txt"))


@pytest.mark.asyncio
async def test_unsafe_file_id_rejected_in_get(store):
    with pytest.raises(UnsafeFilenameError):
        await store.get_for_session("sess-1", "../escape")


# ============================================================================
# mime
# ============================================================================


@pytest.mark.asyncio
async def test_save_uses_content_type_when_provided(store):
    ref = await store.save("sess-1", _upload(b"x", "a.bin", "application/x-custom"))
    assert ref.mime == "application/x-custom"


@pytest.mark.asyncio
async def test_save_guesses_mime_from_filename(store):
    ref = await store.save("sess-1", _upload(b"x", "image.png"))
    assert ref.mime == "image/png"


@pytest.mark.asyncio
async def test_save_defaults_octet_stream_for_unknown(store):
    ref = await store.save("sess-1", _upload(b"x", "weird.unknownext"))
    assert ref.mime == "application/octet-stream"


# ============================================================================
# 默认值常量
# ============================================================================


def test_default_constants_match_spec():
    assert DEFAULT_MAX_FILE_SIZE == 25 * 1024 * 1024
    assert DEFAULT_MAX_SESSION_SIZE == 100 * 1024 * 1024


# ============================================================================
# helpers
# ============================================================================


async def asyncio_sleep_ms(ms: int) -> None:
    import asyncio
    await asyncio.sleep(ms / 1000)
