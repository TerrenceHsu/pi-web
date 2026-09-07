"""Offline backup/restore and non-destructive Wiki upgrade journeys."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pi_agent_core_py.maintenance import (
    MaintenanceError,
    backup_data,
    installation_lock,
    restore_data,
    verify_backup,
)
from pi_agent_core_py.web.wiki.store import WikiStore
from pi_agent_core_py.web.wiki.upgrade import upgrade_wiki


def installation(root: Path) -> Path:
    root.mkdir()
    with sqlite3.connect(root / "auth.sqlite") as db:
        db.execute("CREATE TABLE accounts(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO accounts VALUES ('mine')")
    (root / "users").mkdir()
    (root / "users" / "Memory.md").write_text("重要记忆", encoding="utf-8")
    return root


def test_backup_restore_exact_files_and_wal_commits(tmp_path: Path) -> None:
    source = installation(tmp_path / "data")
    with sqlite3.connect(source / "workspace.sqlite") as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE messages(body TEXT)")
        live.execute("INSERT INTO messages VALUES ('committed in WAL')")
        live.commit()
        result = backup_data(source, tmp_path / "backup", offline=True)
        assert result["files"] == 3
        assert verify_backup(tmp_path / "backup")["files"]
        restore_data(tmp_path / "backup", tmp_path / "restored", offline=True)
    assert (tmp_path / "restored/users/Memory.md").read_text(encoding="utf-8") == "重要记忆"
    with sqlite3.connect(tmp_path / "restored/workspace.sqlite") as db:
        assert db.execute("SELECT body FROM messages").fetchall() == [("committed in WAL",)]
    assert not (tmp_path / "restored/workspace.sqlite-wal").exists()

    # An offline WAL-mode DB with no sidecar must also succeed on its first backup.
    offline = installation(tmp_path / "offline")
    with closing(sqlite3.connect(offline / "workspace.sqlite")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE ready(id INTEGER)")
        db.commit()
    assert not (offline / "workspace.sqlite-wal").exists()
    backup_data(offline, tmp_path / "offline-backup", offline=True)
    assert not (offline / "workspace.sqlite-wal").exists()


def test_backup_busy_offline_and_no_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = installation(tmp_path / "data")
    with pytest.raises(MaintenanceError, match="confirm_offline"):
        backup_data(source, tmp_path / "backup")
    with installation_lock(source):
        with pytest.raises(MaintenanceError, match="installation_busy"):
            backup_data(source, tmp_path / "backup", offline=True)
    with pytest.raises(MaintenanceError, match="new_and_outside"):
        backup_data(source, source / "nested", offline=True)
    assert not (tmp_path / "backup").exists()
    original_scandir = os.scandir

    def unreadable_users(path: str) -> object:
        if Path(path) == source / "users":
            raise PermissionError("synthetic unreadable subtree")
        return original_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", unreadable_users)
        with pytest.raises(MaintenanceError, match="source_tree_unreadable"):
            backup_data(source, tmp_path / "unreadable-backup", offline=True)
    assert not (tmp_path / "unreadable-backup/backup-manifest.json").exists()


def test_tampered_backup_refuses_restore_and_never_overwrites(tmp_path: Path) -> None:
    source = installation(tmp_path / "data")
    backup = tmp_path / "backup"
    backup_data(source, backup, offline=True)
    with pytest.raises(MaintenanceError, match="new_and_outside"):
        restore_data(backup, source, offline=True)
    (backup / "data/users/Memory.md").write_text("changed", encoding="utf-8")
    with pytest.raises(MaintenanceError, match="hash_mismatch"):
        restore_data(backup, tmp_path / "restored", offline=True)
    assert not (tmp_path / "restored").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["compatible", "rebuild-sources"])
async def test_wiki_upgrade_preserves_original_and_validates_new_store(
    tmp_path: Path, mode: str,
) -> None:
    source = tmp_path / "old"
    old = await WikiStore.open(source)
    space = await old.create_space(name="Knowledge")
    raw = await old.upload_source(
        space.id, display_name="source.html", mime_type="text/html",
        content=b"<html><body>preserved source</body></html>",
    )
    await old.close()
    with sqlite3.connect(source / "wiki.db") as db:
        db.execute("UPDATE wiki_schema_meta SET value=7 WHERE key='schema_version'")
        db.execute("PRAGMA user_version=7")
    before = hashlib.sha256((source / "wiki.db").read_bytes()).hexdigest()
    output = tmp_path / "upgrade"
    report = await upgrade_wiki(source, output, mode=mode, offline=True)
    assert report["from_version"] == 7 and report["to_version"] == 8
    assert hashlib.sha256((source / "wiki.db").read_bytes()).hexdigest() == before
    archive = json.loads((output / "legacy-inventory.json").read_text())
    assert "wiki.db" in archive
    upgraded = await WikiStore.open(output / "wiki")
    try:
        spaces = await upgraded.list_spaces()
        assert len(spaces) == 1
        sources = await upgraded.list_sources(spaces[0].id)
        assert len(sources) == 1 and sources[0].source_sha256 == raw.source_sha256
        if mode == "compatible":
            assert spaces[0].id == space.id and sources[0].id == raw.id
        else:
            assert report["requires_reparse_and_approval"] is True
            assert "pages" in report["archive_only"]
            assert sources[0].status == "uploaded"
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_wiki_future_version_and_missing_offline_refuse(tmp_path: Path) -> None:
    source = tmp_path / "old"
    store = await WikiStore.open(source)
    await store.close()
    with pytest.raises(MaintenanceError, match="confirm_offline"):
        await upgrade_wiki(source, tmp_path / "new", mode="compatible")
    with pytest.raises(MaintenanceError, match="retired_wiki"):
        await upgrade_wiki(source, tmp_path / "new", mode="compatible", offline=True)
    assert not (tmp_path / "new").exists()
