"""Legacy Knowledge retirement gate and verified read-only backup tests."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import (
    WIKI_LEGACY_MANIFEST_SCHEMA,
    LegacyRetirementError,
    WikiLegacyBackupReceipt,
    WikiPathError,
    WikiStore,
    retire_legacy_knowledge,
)
from pi_agent_core_py.web.wiki import legacy as legacy_module

_BACKUP_ID = "backup_1234567890abcdef12345678"


def _seed_legacy(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "knowledge.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE knowledge_libraries (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO knowledge_libraries (id, name) VALUES ('lib_old', '旧资料')"
        )
        connection.commit()
    finally:
        connection.close()
    document = root / "libraries" / "lib_old" / "documents" / "doc_old"
    document.mkdir(parents=True)
    (document / "source.pdf").write_bytes(b"%PDF-1.7\nlegacy\n")
    (document / "document.md").write_text("# 旧资料\n", encoding="utf-8")


def test_no_legacy_data_is_a_noop_but_initializes_fixed_roots(tmp_path: Path) -> None:
    receipt = retire_legacy_knowledge(tmp_path)
    assert receipt is None
    assert (tmp_path / "legacy").is_dir()
    assert (tmp_path / "spaces").is_dir()


async def test_preserve_policy_keeps_legacy_and_opens_independent_wiki(
    tmp_path: Path,
) -> None:
    _seed_legacy(tmp_path)
    old_database = (tmp_path / "knowledge.db").read_bytes()

    store = await WikiStore.open(tmp_path)
    try:
        assert store.legacy_backup_receipt is None
        assert await store.list_spaces() == ()
        assert (tmp_path / "wiki.db").is_file()
        assert (tmp_path / "knowledge.db").read_bytes() == old_database
        assert (tmp_path / "libraries" / "lib_old").is_dir()
    finally:
        await store.close()


async def test_explicit_retire_backs_up_then_initializes_empty_wiki(
    tmp_path: Path,
) -> None:
    _seed_legacy(tmp_path)
    store = await WikiStore.open(
        tmp_path,
        legacy_policy="retire",
        clock_ms=lambda: 1234,
        backup_id_factory=lambda: _BACKUP_ID,
    )
    try:
        receipt = store.legacy_backup_receipt
        assert receipt is not None
        assert receipt.cleanup_complete is True
        assert receipt.retired_names == ("knowledge.db", "libraries")
        assert receipt.backup_relpath == f"legacy/knowledge-1234-{_BACKUP_ID}"
        assert await store.list_spaces() == ()
    finally:
        await store.close()

    assert not (tmp_path / "knowledge.db").exists()
    assert not (tmp_path / "libraries").exists()
    backup = tmp_path / receipt.backup_relpath
    assert (backup / "knowledge.db").is_file()
    assert (backup / "libraries/lib_old/documents/doc_old/source.pdf").is_file()
    manifest_path = backup / "legacy-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == WIKI_LEGACY_MANIFEST_SCHEMA
    assert manifest["backup_id"] == _BACKUP_ID
    assert {entry["path"] for entry in manifest["files"]} == {
        "knowledge.db",
        "libraries/lib_old/documents/doc_old/document.md",
        "libraries/lib_old/documents/doc_old/source.pdf",
    }
    assert stat.S_IMODE(manifest_path.stat().st_mode) & stat.S_IWUSR == 0

    with sqlite3.connect(backup / "knowledge.db") as connection:
        row = connection.execute("SELECT name FROM knowledge_libraries").fetchone()
    assert row == ("旧资料",)
    with sqlite3.connect(tmp_path / "wiki.db") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "wiki_spaces" in tables
    assert "knowledge_libraries" not in tables


def test_invalid_legacy_database_fails_without_deleting_originals(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "knowledge.db"
    database.write_bytes(b"not a sqlite database")
    library = tmp_path / "libraries" / "lib_old"
    library.mkdir(parents=True)
    (library / "evidence.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(LegacyRetirementError) as exc_info:
        retire_legacy_knowledge(
            tmp_path,
            clock_ms=lambda: 1,
            id_factory=lambda: _BACKUP_ID,
        )
    assert exc_info.value.code == "legacy_invalid"
    assert database.read_bytes() == b"not a sqlite database"
    assert (library / "evidence.txt").read_text(encoding="utf-8") == "keep"
    assert not list((tmp_path / "legacy").glob("knowledge-*"))
    assert not list((tmp_path / "legacy").glob(".*.tmp"))


def test_invalid_backup_id_fails_before_mutating_legacy(tmp_path: Path) -> None:
    _seed_legacy(tmp_path)
    with pytest.raises(LegacyRetirementError) as exc_info:
        retire_legacy_knowledge(tmp_path, id_factory=lambda: "../escape")
    assert exc_info.value.code == "invalid_configuration"
    assert (tmp_path / "knowledge.db").exists()
    assert (tmp_path / "libraries").exists()


def test_existing_retirement_lock_fails_busy_without_mutation(tmp_path: Path) -> None:
    _seed_legacy(tmp_path)
    lock = tmp_path / ".wiki-legacy-retirement.lock"
    lock.write_text("other initializer", encoding="utf-8")

    with pytest.raises(LegacyRetirementError) as exc_info:
        retire_legacy_knowledge(tmp_path)
    assert exc_info.value.code == "legacy_busy"
    assert lock.read_text(encoding="utf-8") == "other initializer"
    assert (tmp_path / "knowledge.db").exists()
    assert (tmp_path / "libraries").exists()
    assert not list((tmp_path / "legacy").glob("knowledge-*"))


def test_backup_is_published_before_cleanup_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_legacy(tmp_path)

    def fail_cleanup(**_: Path) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(legacy_module, "_cleanup_originals", fail_cleanup)
    with pytest.raises(LegacyRetirementError) as exc_info:
        retire_legacy_knowledge(
            tmp_path,
            clock_ms=lambda: 55,
            id_factory=lambda: _BACKUP_ID,
        )
    assert exc_info.value.code == "legacy_cleanup_failed"
    receipt = exc_info.value.receipt
    assert isinstance(receipt, WikiLegacyBackupReceipt)
    assert receipt.cleanup_complete is False
    assert (tmp_path / receipt.backup_relpath / "legacy-manifest.json").is_file()
    assert (tmp_path / "knowledge.db").exists()
    assert (tmp_path / "libraries").exists()


def test_legacy_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence = outside / "evidence.txt"
    evidence.write_text("outside", encoding="utf-8")
    libraries = tmp_path / "libraries"
    try:
        libraries.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this Windows account")

    with pytest.raises(WikiPathError):
        retire_legacy_knowledge(tmp_path)
    assert evidence.read_text(encoding="utf-8") == "outside"
    assert libraries.is_symlink()


def test_legacy_errors_do_not_echo_paths_or_content(tmp_path: Path) -> None:
    secret_name = "private-customer-name"
    (tmp_path / "knowledge.db").write_text(secret_name, encoding="utf-8")
    with pytest.raises(LegacyRetirementError) as exc_info:
        retire_legacy_knowledge(tmp_path)
    assert secret_name not in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)
    assert secret_name not in repr(exc_info.value)
    assert os.fspath(tmp_path) not in repr(exc_info.value)
