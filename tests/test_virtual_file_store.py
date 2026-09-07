"""P0-2: VirtualFileStore 单元测试。

覆盖纯 store 行为（不走 FastAPI）：
- init / ensure session folder / save / write_text / get / list / delete
- sanitize_filename
- 路径穿越防护
- 大小 / 总量限制
- metadata 持久化
- 删除 session 级联
"""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import UploadFile

from pi_agent_core_py.web.files import (
    AGENT_INSTRUCTIONS_PATH,
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_MAX_SESSION_SIZE,
    DEFAULT_MEMORY,
    MEMORY_PATH,
    FileAccessDeniedError,
    FileRef,
    FileTooLargeError,
    FileVersionConflictError,
    SessionStorageLimitError,
    UnsafeFilenameError,
    VirtualFileNotFoundError,
    VirtualFileStore,
    WorkspacePathConflictError,
    WorkspaceStore,
    WorkspaceVersionConflictError,
    _extended_length_path,
    normalize_logical_path,
    normalize_workspace_logical_path,
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
    assert sanitize_filename("C:\\fakepath\\file.txt") == "file.txt"
    assert sanitize_filename("C:file.txt") == "file.txt"
    assert sanitize_filename("\\\\server\\share\\file.txt") == "file.txt"
    assert sanitize_filename("../windows\\nested/file.txt") == "file.txt"


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


def test_normalize_logical_path_accepts_relative_folders_only():
    assert normalize_logical_path("report.md", "outputs/2026") == "outputs/2026/report.md"
    with pytest.raises(UnsafeFilenameError):
        normalize_logical_path("report.md", "../escape")
    with pytest.raises(UnsafeFilenameError):
        normalize_logical_path("report.md", "C:\\escape")


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


@pytest.mark.asyncio
async def test_ensure_session_folder_is_eager_and_idempotent(store, tmp_path):
    session_dir = await store.ensure_session_folder("sess-1")
    # Windows 下 store 根路径统一为 \\?\ 扩展长度形式；比较前同样归一
    assert session_dir == _extended_length_path(
        (tmp_path / "uploads" / "sess-1").resolve()
    )
    assert session_dir.is_dir()
    assert await store.ensure_session_folder("sess-1") == session_dir


@pytest.mark.asyncio
async def test_ensure_session_folder_rejects_path_traversal(store):
    with pytest.raises(UnsafeFilenameError):
        await store.ensure_session_folder("../escape")


@pytest.mark.asyncio
async def test_ensure_session_workspace_seeds_persistent_root_files(store):
    _, first = await store.ensure_session_workspace("sess-1")
    _, second = await store.ensure_session_workspace("sess-1")
    assert WorkspaceStore is VirtualFileStore
    assert first.id == second.id
    assert first.logical_path == AGENT_INSTRUCTIONS_PATH
    assert first.purpose == "agent_instructions"
    assert first.origin == "system"
    files = await store.list_session("sess-1")
    assert [ref.logical_path for ref in files] == [
        AGENT_INSTRUCTIONS_PATH,
        MEMORY_PATH,
    ]
    memory = await store.get_by_logical_path("sess-1", MEMORY_PATH)
    assert memory is not None
    assert memory.purpose == "memory"
    assert memory.origin == "system"
    assert __import__("pathlib").Path(memory.path).read_text() == DEFAULT_MEMORY

    custom = "# AGENT.md\n\nKeep this.\n"
    await store.update_text(
        "sess-1",
        first.id,
        custom,
        expected_sha256=first.sha256,
    )
    _, after_reopen = await store.ensure_session_workspace("sess-1")
    assert after_reopen.id == first.id
    assert __import__("pathlib").Path(after_reopen.path).read_text() == custom
    with pytest.raises(FileVersionConflictError):
        await store.update_text(
            "sess-1",
            first.id,
            "stale",
            expected_sha256=first.sha256,
        )


@pytest.mark.asyncio
async def test_workspace_migrates_legacy_root_paths_without_replacing_files(store):
    await store.ensure_session_folder("sess-legacy-roots")
    legacy_agent = await store._write_text_unlocked(
        "sess-legacy-roots",
        "agent.md",
        "# Existing instructions\n",
        logical_path="agent.md",
        content_type="text/markdown",
        origin="user",
        purpose="file",
    )
    legacy_memory = await store._write_text_unlocked(
        "sess-legacy-roots",
        "memory.md",
        "# Existing memory\n\nKeep this.\n",
        logical_path="memory.md",
        content_type="text/markdown",
        origin="user",
        purpose="file",
    )

    _, migrated_agent = await store.ensure_session_workspace(
        "sess-legacy-roots"
    )
    migrated_memory = await store.get_by_logical_path(
        "sess-legacy-roots", MEMORY_PATH
    )

    assert migrated_agent.id == legacy_agent.id
    assert migrated_agent.logical_path == AGENT_INSTRUCTIONS_PATH
    assert migrated_agent.purpose == "agent_instructions"
    assert migrated_memory is not None
    assert migrated_memory.id == legacy_memory.id
    assert migrated_memory.logical_path == MEMORY_PATH
    assert migrated_memory.purpose == "memory"
    assert __import__("pathlib").Path(migrated_memory.path).read_text() == (
        "# Existing memory\n\nKeep this.\n"
    )
    assert len(await store.list_session("sess-legacy-roots")) == 2


@pytest.mark.asyncio
async def test_workspace_root_initialization_is_concurrency_safe(store):
    results = await asyncio.gather(*(
        store.ensure_session_workspace("sess-concurrent")
        for _ in range(8)
    ))

    assert len({agent.id for _, agent in results}) == 1
    files = await store.list_session("sess-concurrent")
    assert [ref.logical_path for ref in files].count(AGENT_INSTRUCTIONS_PATH) == 1
    assert [ref.logical_path for ref in files].count(MEMORY_PATH) == 1
    assert len(files) == 2


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
async def test_write_text_creates_managed_file_readable_by_existing_apis(store):
    ref = await store.write_text("sess-1", "report.md", "# 报告\n\n完成")
    assert ref.name == "report.md"
    assert ref.mime == "text/markdown"
    assert ref.size == len("# 报告\n\n完成".encode())
    assert "完成" in __import__("pathlib").Path(ref.path).read_text(encoding="utf-8")
    assert (await store.get_for_session("sess-1", ref.id)).id == ref.id
    assert [item.id for item in await store.list_session("sess-1")] == [ref.id]


@pytest.mark.asyncio
async def test_write_text_creates_unique_logical_tree_paths(store):
    first = await store.write_text(
        "sess-1",
        "report.md",
        "one",
        folder="outputs/2026",
    )
    second = await store.write_text(
        "sess-1",
        "report.md",
        "two",
        folder="outputs/2026",
    )
    assert first.logical_path == "outputs/2026/report.md"
    assert second.logical_path == "outputs/2026/report (2).md"


@pytest.mark.asyncio
async def test_write_text_never_overwrites_same_named_file(store):
    first = await store.write_text("sess-1", "result.txt", "one")
    second = await store.write_text("sess-1", "result.txt", "two")
    assert first.id != second.id
    assert len(await store.list_session("sess-1")) == 2


@pytest.mark.asyncio
async def test_update_text_commits_by_atomic_metadata_pointer(store):
    from pathlib import Path

    first = await store.write_text("sess-1", "notes.md", "old")
    first_path = Path(first.path)
    updated = await store.update_text(
        "sess-1", first.id, "new", expected_sha256=first.sha256
    )

    assert Path(updated.path) != first_path
    assert Path(updated.path).read_text(encoding="utf-8") == "new"  # noqa: ASYNC240
    assert not first_path.exists()  # noqa: ASYNC240
    assert (await store.get_for_session("sess-1", first.id)).path == updated.path


@pytest.mark.asyncio
async def test_update_text_metadata_failure_keeps_old_generation(
    store, monkeypatch
):
    from pathlib import Path

    first = await store.write_text("sess-1", "notes.md", "old")
    original = store._write_metadata

    def fail_metadata(*_args, **_kwargs):
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(store, "_write_metadata", fail_metadata)
    with pytest.raises(Exception, match="simulated metadata failure"):
        await store.update_text(
            "sess-1", first.id, "new", expected_sha256=first.sha256
        )
    monkeypatch.setattr(store, "_write_metadata", original)

    restored = await store.get_for_session("sess-1", first.id)
    assert restored.sha256 == first.sha256
    assert Path(restored.path).read_text(encoding="utf-8") == "old"  # noqa: ASYNC240
    file_dir = Path(first.path).parent
    assert not list(file_dir.glob(".content-*.blob"))


@pytest.mark.asyncio
async def test_init_repairs_legacy_content_metadata_mismatch(tmp_path):
    from pathlib import Path

    root = tmp_path / "uploads"
    first_store = VirtualFileStore(root)
    ref = await first_store.write_text("sess-1", "notes.md", "old")
    Path(ref.path).write_text("published", encoding="utf-8")  # noqa: ASYNC240

    reopened = VirtualFileStore(root)
    await reopened.init()
    recovered = await reopened.get_for_session("sess-1", ref.id)

    assert recovered.size == len(b"published")
    assert recovered.sha256 != ref.sha256
    assert Path(recovered.path).read_text(encoding="utf-8") == "published"  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_write_text_honours_file_and_session_limits(tmp_path):
    file_limited = VirtualFileStore(tmp_path / "file-limit", max_file_size=3)
    with pytest.raises(FileTooLargeError):
        await file_limited.write_text("sess-1", "large.txt", "four")

    session_limited = VirtualFileStore(
        tmp_path / "session-limit",
        max_file_size=10,
        max_session_size=5,
    )
    await session_limited.write_text("sess-1", "a.txt", "123")
    with pytest.raises(SessionStorageLimitError):
        await session_limited.write_text("sess-1", "b.txt", "456")


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
    assert target.is_relative_to(
        _extended_length_path((tmp_path / "uploads").resolve())
    )  # noqa: ASYNC240


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


# ============================================================================
# Windows 长路径（MAX_PATH）回归
# ============================================================================


@pytest.mark.asyncio
async def test_write_text_survives_windows_max_path(tmp_path):
    """深层嵌套 root 下叶子路径超过 260 字符时写入仍成功。

    Windows 未启用 LongPathsEnabled 时，普通路径 ≥260 的文件 IO 会以
    FileNotFoundError 失败；store 根路径统一扩展长度前缀后必须正常。
    非 Windows 平台长路径原生可用，本测试同样应通过。
    """
    deep_root = tmp_path
    # 每层 60 字符 × 5 层，确保叶子路径在任何平台都足够深
    for _ in range(5):
        deep_root = deep_root / ("d" * 60)
    store = VirtualFileStore(deep_root)
    await store.init()
    ref = await store.write_text(
        "sess-1", ("f" * 80) + ".md", "# deep",
        origin="agent", purpose="file",
    )
    from pathlib import Path

    assert Path(ref.path).read_text(encoding="utf-8") == "# deep"  # noqa: ASYNC240
    fetched = await store.get_for_session("sess-1", ref.id)
    assert fetched.sha256 == ref.sha256


@pytest.mark.asyncio
async def test_update_text_with_legacy_plain_metadata_path(store, tmp_path):
    """旧版 metadata.json 存普通（无扩展前缀）路径时 update_text 不误判越界。"""
    import json
    import os as _os
    from pathlib import Path

    ref = await store.write_text(
        "sess-legacy", "Memory.md", "v1",
        origin="agent", purpose="memory",
    )
    # 把 metadata.json 改写为历史版本的普通路径形式
    file_dir = Path(ref.path).parent
    meta_path = file_dir / "metadata.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    legacy = _os.path.abspath(payload["path"])  # noqa: ASYNC240
    if legacy.startswith("\\\\?\\"):
        legacy = legacy[4:]
    payload["path"] = legacy
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    updated = await store.update_text(
        "sess-legacy", ref.id, "v2",
        expected_sha256=ref.sha256,
        origin="agent", purpose="memory",
    )
    assert Path(updated.path).read_text(encoding="utf-8") == "v2"  # noqa: ASYNC240


# ============================================================================
# Phase 2 Workspace tree + revision semantics
# ============================================================================


@pytest.mark.asyncio
async def test_agent_code_uses_scripts_and_uploaded_code_uses_upload(store):
    agent_code = await store.write_text("sess-code", "main.py", "print('ok')")
    nested_code = await store.write_text(
        "sess-code",
        "util.ts",
        "export {}",
        folder="packages/core",
    )
    uploaded_code = await store.save(
        "sess-code",
        _upload(b"fn main() {}", "main.rs"),
        relative_folder="src",
    )
    markdown = await store.write_text(
        "sess-code",
        "README.md",
        "# Read me",
        folder="docs",
    )

    assert agent_code.logical_path == "scripts/main.py"
    assert nested_code.logical_path == "scripts/packages/core/util.ts"
    assert uploaded_code.logical_path == "upload/src/main.rs"
    assert uploaded_code.purpose == "input"
    assert markdown.logical_path == "docs/README.md"


@pytest.mark.asyncio
async def test_upload_directory_is_lazy_isolated_collision_safe_and_materialized(tmp_path):
    from pathlib import Path

    from agent_workspace import is_sandbox_publishable_workspace_path, workspace_path_policy

    store = WorkspaceStore(tmp_path / "storage")
    await store.ensure_session_workspace("one")
    await store.ensure_session_workspace("two")
    initial_files = await store.list_session("one")
    assert not any(ref.logical_path.startswith("upload/") for ref in initial_files)
    originals = [
        ("report.pdf", b"pdf"), ("image.png", b"png"), ("main.py", b"code"),
        ("notes.docx", b"docx"), ("data.xlsx", b"xlsx"),
    ]
    for name, content in originals:
        ref = await store.save("one", _upload(content, name))
        assert ref.logical_path == f"upload/{name}"
        assert await asyncio.to_thread(Path(ref.path).read_bytes) == content
        policy = workspace_path_policy(ref.logical_path, purpose=ref.purpose)
        assert policy.immutable_content and not policy.sandbox_publishable
        assert not is_sandbox_publishable_workspace_path(ref.logical_path)

    duplicate = await store.save("one", _upload(b"different", "report.pdf"))
    assert duplicate.logical_path == "upload/report (2).pdf"
    other_files = await store.list_session("two")
    assert not any(ref.logical_path.startswith("upload/") for ref in other_files)
    await store.materialize_workspace_revision("one", tmp_path / "snapshot")
    for name, content in originals:
        assert (tmp_path / "snapshot" / "upload" / name).read_bytes() == content
    assert (tmp_path / "snapshot" / "upload" / "report (2).pdf").read_bytes() == b"different"


@pytest.mark.asyncio
async def test_upload_subfolder_stays_below_upload_and_rejects_traversal(store):
    uploaded = await store.save("one", _upload(b"hello", "AGENT.md"), relative_folder="UPLOAD/docs")
    assert uploaded.logical_path == "upload/docs/AGENT.md"
    with pytest.raises(UnsafeFilenameError):
        await store.save("one", _upload(b"bad", "file.txt"), relative_folder="../outside")


@pytest.mark.parametrize(
    "logical_path",
    ["../escape.md", "docs/../escape.md", "/root.md", "C:/root.md", "a\\b.md"],
)
def test_workspace_logical_paths_are_strict(logical_path):
    with pytest.raises(UnsafeFilenameError):
        normalize_workspace_logical_path(logical_path)


@pytest.mark.asyncio
async def test_workspace_revision_is_persistent_and_rejects_stale_mutations(tmp_path):
    root = tmp_path / "revision-uploads"
    first_store = WorkspaceStore(root)
    await first_store.ensure_session_workspace("sess-revision")
    initial = await first_store.get_workspace_state("sess-revision")
    assert initial.revision == 0

    created = await first_store.write_text(
        "sess-revision",
        "notes.md",
        "one",
        expected_workspace_revision=0,
        unique_logical_path=False,
    )
    assert (await first_store.get_workspace_state("sess-revision")).revision == 1
    with pytest.raises(WorkspaceVersionConflictError) as conflict:
        await first_store.update_text(
            "sess-revision",
            created.id,
            "stale",
            expected_sha256=created.sha256,
            expected_workspace_revision=0,
        )
    assert conflict.value.current == 1

    reopened = WorkspaceStore(root)
    await reopened.init()
    assert (await reopened.get_workspace_state("sess-revision")).revision == 1


@pytest.mark.asyncio
async def test_exact_markdown_create_move_and_delete_use_revision(store):
    await store.ensure_session_workspace("sess-crud")
    created = await store.write_text(
        "sess-crud",
        "plan.md",
        "v1",
        folder="docs",
        expected_workspace_revision=0,
        unique_logical_path=False,
    )
    with pytest.raises(WorkspacePathConflictError):
        await store.write_text(
            "sess-crud",
            "plan.md",
            "duplicate",
            folder="docs",
            expected_workspace_revision=1,
            unique_logical_path=False,
        )

    moved = await store.move_file(
        "sess-crud",
        created.id,
        "notes/renamed.md",
        expected_sha256=created.sha256,
        expected_workspace_revision=1,
    )
    assert moved.logical_path == "notes/renamed.md"
    assert (await store.get_workspace_state("sess-crud")).revision == 2

    with pytest.raises(WorkspaceVersionConflictError):
        await store.delete_for_session(
            "sess-crud",
            moved.id,
            expected_sha256=moved.sha256,
            expected_workspace_revision=1,
        )
    deleted = await store.delete_for_session(
        "sess-crud",
        moved.id,
        expected_sha256=moved.sha256,
        expected_workspace_revision=2,
    )
    assert deleted.id == moved.id
    assert (await store.get_workspace_state("sess-crud")).revision == 3
