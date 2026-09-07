"""Explicit offline Wiki upgrade into a NEW directory, with the old tree retained."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from ...maintenance import (
    MaintenanceError,
    _new_destination,
    check_database,
    inventory,
    safe_path,
    snapshot_tree,
)
from .files import WikiFileStore
from .models import WIKI_SCHEMA_VERSION
from .store import _REQUIRED_COLUMNS, WIKI_APPLICATION_ID, WikiStore


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schema_version(path: Path) -> int:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        check_database(db)
        if db.execute("PRAGMA application_id").fetchone()[0] != WIKI_APPLICATION_ID:
            raise MaintenanceError("not_a_wiki_database")
        row = db.execute(
            "SELECT value FROM wiki_schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or db.execute("PRAGMA user_version").fetchone()[0] != row[0]:
            raise MaintenanceError("wiki_version_mismatch")
        return int(row[0])


async def upgrade_wiki(
    source: Path, destination: Path, *, mode: Literal["compatible", "rebuild-sources"],
    offline: bool = False,
) -> dict[str, Any]:
    if not offline:
        raise MaintenanceError("stop_web_and_worker_then_confirm_offline")
    source = safe_path(source)
    destination = _new_destination(source, destination)
    version = _schema_version(source / "wiki.db")
    if not 1 <= version < WIKI_SCHEMA_VERSION:
        raise MaintenanceError("only_retired_wiki_versions_can_be_upgraded")
    if mode == "compatible" and version != 7:
        raise MaintenanceError("legacy_schema_requires_explicit_source_rebuild")
    destination.mkdir()
    archive, target = destination / "legacy", destination / "wiki"
    original = snapshot_tree(source, archive)
    (destination / "legacy-inventory.json").write_text(
        json.dumps(original, indent=2), encoding="utf-8",
    )
    store = await WikiStore.open(target)
    await store.close()
    if mode == "compatible":
        counts = _compatible_rows(archive / "wiki.db", target / "wiki.db")
        for name in original:
            if name == "wiki.db":
                continue
            file = target / name
            file.parent.mkdir(parents=True, exist_ok=True)
            if file.exists():
                raise MaintenanceError("upgrade_file_collision")
            shutil.copyfile(archive / name, file)
        # Validate current constraints and regenerate only rebuildable mirrors/FTS.
        current = await WikiStore.open(target)
        await current.close()
        result: dict[str, Any] = {"row_counts": counts, "preserves_ids": True}
    elif mode == "rebuild-sources":
        result = await _rebuild_sources(archive, target)
    else:
        raise MaintenanceError("unsupported_upgrade_mode")
    if inventory(archive) != original:
        raise MaintenanceError("legacy_archive_changed")
    report = {
        "schema": "pi-wiki-upgrade/v1", "from_version": version,
        "to_version": WIKI_SCHEMA_VERSION, "mode": mode, **result,
        "legacy": "legacy", "target": "wiki", "activation_required": True,
        "warning": "Never overwrite the old directory; retain the installation backup and keys",
    }
    (destination / "upgrade-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report


def _compatible_rows(source: Path, destination: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
        with closing(sqlite3.connect(destination)) as new:
            known = {
                row[0] for row in new.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            previous = {
                row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if previous - known:
                raise MaintenanceError("unknown_legacy_tables_require_explicit_source_rebuild")
            # Cyclic deferred references are checked in full before commit.
            new.execute("PRAGMA foreign_keys=OFF")
            new.execute("BEGIN IMMEDIATE")
            for table in _REQUIRED_COLUMNS:
                if table == "wiki_pages_fts":
                    continue
                columns = [row[1] for row in new.execute(f"PRAGMA table_info({_quote(table)})")]
                old_columns = [row[1] for row in old.execute(f"PRAGMA table_info({_quote(table)})")]
                if set(columns) != set(old_columns):
                    raise MaintenanceError("legacy_columns_require_explicit_source_rebuild")
                names = ",".join(_quote(column) for column in columns)
                placeholders = ",".join("?" for _ in columns)
                rows = old.execute(f"SELECT {names} FROM {_quote(table)}")
                count = 0
                try:
                    while batch := rows.fetchmany(1000):
                        new.executemany(
                            f"INSERT INTO {_quote(table)} ({names}) VALUES ({placeholders})", batch,
                        )
                        count += len(batch)
                except sqlite3.IntegrityError:
                    raise MaintenanceError(
                        "legacy_evidence_requires_explicit_source_rebuild",
                    ) from None
                counts[table] = count
            check_database(new)
            new.commit()
    return counts


async def _rebuild_sources(archive: Path, target: Path) -> dict[str, Any]:
    files = WikiFileStore(archive)
    mapping: list[dict[str, str]] = []
    store = await WikiStore.open(target)
    try:
        with closing(sqlite3.connect((archive / "wiki.db").as_uri() + "?mode=ro", uri=True)) as old:
            old.row_factory = sqlite3.Row
            for space in old.execute("SELECT * FROM wiki_spaces ORDER BY id"):
                created = await store.create_space(
                    name=space["name"], description=space["description"],
                )
                mapping.append({"old_space_id": space["id"], "new_space_id": created.id})
                for source in old.execute(
                    "SELECT * FROM wiki_sources WHERE space_id=? ORDER BY id", (space["id"],),
                ):
                    if source["status"] == "deleting":
                        continue  # Deleted source metadata remains in the complete legacy archive.
                    path = files.resolve_owned_regular_file(space["id"], source["source_relpath"])
                    if (
                        path.stat().st_size != source["size_bytes"]
                        or path.stat().st_size > 250 * 1024**2
                    ):
                        raise MaintenanceError("legacy_source_size_mismatch")
                    payload = path.read_bytes()
                    if hashlib.sha256(payload).hexdigest() != source["source_sha256"]:
                        raise MaintenanceError("legacy_source_hash_mismatch")
                    uploaded = await store.upload_source(
                        created.id, display_name=source["display_name"],
                        mime_type=source["mime_type"], content=payload,
                    )
                    mapping.append({"old_source_id": source["id"], "new_source_id": uploaded.id})
    finally:
        await store.close()
    return {
        "preserves_ids": False, "mapping": mapping,
        "requires_reparse_and_approval": True,
        "archive_only": ["pages", "parse_evidence", "conversations", "approvals", "graph"],
        "warning": "Select new Spaces explicitly; old citations refer to the legacy archive",
    }
