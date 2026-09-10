"""Session cleanup removes the whole owned tree without crossing its boundary."""

from __future__ import annotations

import asyncio
import io
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from agent_workspace.store import FileStoreError, UnsafeFilenameError, WorkspaceStore


@pytest.fixture
async def store(tmp_path: Path) -> WorkspaceStore:
    instance = WorkspaceStore(tmp_path / "uploads")
    await instance.init()
    return instance


async def test_cleanup_removes_nested_and_hidden_contents_only_for_target(store: WorkspaceStore):
    target, _ = await store.ensure_session_workspace("target")
    other, _ = await store.ensure_session_workspace("other")
    shared = store.root_dir / "shared.txt"
    shared.write_text("keep", encoding="utf-8")
    for relative in (
        ".workspace-transactions/tx/payload/deep/result.txt",
        ".workspace-published/receipt.json",
        ".deleted-old/nested/stale.txt",
        "orphan.txt",
    ):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("delete", encoding="utf-8")

    assert await store.delete_session_files("target") == 2
    assert not target.exists()
    assert other.is_dir()
    assert len(await store.list_session("other")) == 2
    assert shared.read_text(encoding="utf-8") == "keep"
    assert await store.delete_session_files("target") == 0


@pytest.mark.parametrize("session_id", [".", "..", "", "../other", "a/b", "a\\b"])
async def test_cleanup_rejects_unsafe_session_ids(store: WorkspaceStore, session_id: str):
    target = await store.ensure_session_folder("keep")
    with pytest.raises(UnsafeFilenameError):
        await store.delete_session_files(session_id)
    assert target.is_dir()


async def test_cleanup_without_store_root_is_idempotent(tmp_path: Path):
    store = WorkspaceStore(tmp_path / "missing")
    assert await store.delete_session_files("missing-session") == 0
    assert not store.root_dir.exists()


async def test_cleanup_io_failure_propagates_and_allows_retry(store: WorkspaceStore, monkeypatch):
    target, _ = await store.ensure_session_workspace("target")
    import agent_workspace.store as store_module

    original = store_module.shutil.rmtree

    def fail_delete(path):
        assert path == target
        raise PermissionError("private path must not be in the public message")

    monkeypatch.setattr(store_module.shutil, "rmtree", fail_delete)
    with pytest.raises(FileStoreError, match="cleanup failed.*retried") as caught:
        await store.delete_session_files("target")
    assert "private path" not in str(caught.value)
    assert target.is_dir()
    await store.write_text("target", "still-usable.txt", "keep")
    monkeypatch.setattr(store_module.shutil, "rmtree", original)
    assert await store.delete_session_files("target") == 3
    assert not target.exists()


@pytest.mark.parametrize("link_location", ["session", "nested", "broken"])
async def test_cleanup_rejects_symlinks_without_touching_target(
    store: WorkspaceStore, tmp_path: Path, link_location: str,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence = outside / "keep.txt"
    evidence.write_text("keep", encoding="utf-8")
    if link_location == "session":
        link = store.root_dir / "target"
    else:
        directory = await store.ensure_session_folder("target")
        (directory / "original.txt").write_text("keep", encoding="utf-8")
        link = directory / "link"
    try:
        link.symlink_to(outside if link_location != "broken" else outside / "missing",
                        target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Creating symlinks is not permitted on this host: {type(exc).__name__}")

    with pytest.raises(UnsafeFilenameError):
        await store.delete_session_files("target")
    assert evidence.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
    if link_location != "session":
        assert (link.parent / "original.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("unsafe_location", ["root", "session", "nested"])
async def test_cleanup_rejects_reparse_points_before_deleting_anything(
    store: WorkspaceStore, monkeypatch, unsafe_location: str,
):
    target = await store.ensure_session_folder("target")
    nested = target / "nested"
    nested.mkdir()
    keep = target / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    unsafe = {"root": store.root_dir, "session": target, "nested": nested}[unsafe_location]
    original = Path.lstat

    def reparse_stat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == unsafe:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return info

    monkeypatch.setattr(Path, "lstat", reparse_stat)
    with pytest.raises(UnsafeFilenameError):
        await store.delete_session_files("target")
    assert keep.read_text(encoding="utf-8") == "keep"
    assert nested.is_dir()


async def test_cleanup_rejects_non_directory_session(store: WorkspaceStore):
    target = store.root_dir / "target"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(UnsafeFilenameError):
        await store.delete_session_files("target")
    assert target.read_text(encoding="utf-8") == "keep"


async def test_cleanup_serializes_with_workspace_mutation(store: WorkspaceStore):
    target = await store.ensure_session_folder("target")
    async with store._session_lock("target"):
        cleanup = asyncio.create_task(store.delete_session_files("target"))
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert target.is_dir()
    assert await cleanup == 0
    assert not target.exists()


@pytest.mark.parametrize("operation", ["save", "write_text", "ensure_session_workspace"])
async def test_cleanup_prevents_queued_writes_from_recreating_session(
    store: WorkspaceStore, operation: str,
):
    target = await store.ensure_session_folder("target")

    async def queued_write():
        if operation == "save":
            upload = UploadFile(filename="late.txt", file=io.BytesIO(b"late"))
            try:
                await store.save("target", upload)
            finally:
                await upload.close()
        elif operation == "write_text":
            await store.write_text("target", "late.txt", "late")
        else:
            await store.ensure_session_workspace("target")

    async with store._session_lock("target"):
        cleanup = asyncio.create_task(store.delete_session_files("target"))
        await asyncio.sleep(0)
        writer = asyncio.create_task(queued_write())
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert not writer.done()
    assert await cleanup == 0
    with pytest.raises(FileStoreError, match="has been deleted"):
        await writer
    assert not target.exists()
    assert await store.list_session("target") == []
    assert await store.delete_session_files("target") == 0


async def test_cleanup_missing_session_also_prevents_late_creation(store: WorkspaceStore):
    assert await store.delete_session_files("missing") == 0
    with pytest.raises(FileStoreError, match="has been deleted"):
        await store.ensure_session_folder("missing")
    assert not (store.root_dir / "missing").exists()
