"""Schema, lifecycle, Space CRUD and mirror recovery for WikiStore v2."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiosqlite
import pytest

from pi_agent_core_py.web.wiki import (
    WIKI_APPLICATION_ID,
    WIKI_SCHEMA_VERSION,
    WIKI_SPACE_MANIFEST_SCHEMA,
    WikiSchemaError,
    WikiStore,
    WikiStoreError,
)


@pytest.fixture
async def wiki_store(tmp_path: Path) -> AsyncIterator[WikiStore]:
    ids: Iterator[str] = iter(
        (
            "space_000000000000000000000001",
            "space_000000000000000000000002",
            "space_000000000000000000000003",
        )
    )
    store = await WikiStore.open(
        tmp_path,
        clock_ms=lambda: 100,
        id_factory=lambda: next(ids),
    )
    try:
        yield store
    finally:
        await store.close()


async def _table_names(store: WikiStore) -> set[str]:
    db = store._require_db()
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ) as cursor:
        rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


async def test_fresh_schema_has_all_page_centric_tables_and_no_chunk_tables(
    wiki_store: WikiStore,
) -> None:
    assert await wiki_store.get_schema_version() == WIKI_SCHEMA_VERSION
    assert await wiki_store._read_pragma_int("application_id") == WIKI_APPLICATION_ID
    assert await wiki_store._read_pragma_int("user_version") == WIKI_SCHEMA_VERSION
    tables = await _table_names(wiki_store)
    assert {
        "wiki_schema_meta",
        "wiki_spaces",
        "wiki_sources",
        "wiki_artifacts",
        "wiki_parse_attempts",
        "wiki_parse_revisions",
        "wiki_pages",
        "wiki_page_revisions",
        "wiki_page_sources",
        "wiki_edges",
        "wiki_change_sets",
        "wiki_change_set_items",
        "wiki_conversations",
        "wiki_jobs",
    }.issubset(tables)
    assert not any(name.startswith("knowledge_") for name in tables)
    assert "knowledge_chunks_fts" not in tables


async def test_schema_is_idempotent_and_restart_persists_space(tmp_path: Path) -> None:
    store = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_aaaaaaaaaaaaaaaaaaaaaaaa",
    )
    space = await store.create_space(name="Persistent")
    await store.close()

    reopened = await WikiStore.open(tmp_path)
    try:
        assert await reopened.get_space(space.id) == space
        assert reopened.startup_repair_report.repaired_space_ids == ()
    finally:
        await reopened.close()


async def test_two_fresh_openers_converge_on_one_schema(tmp_path: Path) -> None:
    first, second = await asyncio.gather(
        WikiStore.open(tmp_path),
        WikiStore.open(tmp_path),
    )
    try:
        assert await first.get_schema_version() == WIKI_SCHEMA_VERSION
        assert await second.get_schema_version() == WIKI_SCHEMA_VERSION
    finally:
        await first.close()
        await second.close()


async def test_future_schema_version_fails_closed(tmp_path: Path) -> None:
    store = await WikiStore.open(tmp_path)
    await store.close()
    with sqlite3.connect(tmp_path / "wiki.db") as connection:
        connection.execute(
            "UPDATE wiki_schema_meta SET value = ? WHERE key = 'schema_version'",
            (WIKI_SCHEMA_VERSION + 1,),
        )
        connection.commit()

    with pytest.raises(WikiSchemaError) as exc_info:
        await WikiStore.open(tmp_path)
    assert exc_info.value.code == "schema_incompatible"


async def test_retired_flat_v1_schema_requires_explicit_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "wiki.db"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA application_id = {WIKI_APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "CREATE TABLE wiki_schema_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO wiki_schema_meta (key, value) VALUES ('schema_version', 1)"
        )
        connection.execute(
            "CREATE TABLE wiki_sources (id TEXT PRIMARY KEY, parsed_markdown_relpath TEXT)"
        )
        connection.commit()
    before = path.read_bytes()

    with pytest.raises(WikiSchemaError) as exc_info:
        await WikiStore.open(tmp_path)

    assert exc_info.value.code == "schema_rebuild_required"
    assert path.read_bytes() == before


async def test_wrong_application_id_fails_without_overwriting_file(tmp_path: Path) -> None:
    path = tmp_path / "wiki.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA application_id = 123")
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
    before = path.read_bytes()

    with pytest.raises(WikiSchemaError):
        await WikiStore.open(tmp_path)

    assert path.read_bytes() == before


async def test_unversioned_partial_schema_is_not_treated_as_fresh(tmp_path: Path) -> None:
    path = tmp_path / "wiki.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE wiki_spaces (id TEXT PRIMARY KEY)")
        connection.commit()

    with pytest.raises(WikiSchemaError):
        await WikiStore.open(tmp_path)


async def test_missing_required_column_is_detected_on_reopen(tmp_path: Path) -> None:
    store = await WikiStore.open(tmp_path)
    await store.close()
    with sqlite3.connect(tmp_path / "wiki.db") as connection:
        connection.execute("DROP TABLE wiki_jobs")
        connection.execute("CREATE TABLE wiki_jobs (id TEXT PRIMARY KEY) STRICT")
        connection.commit()

    with pytest.raises(WikiSchemaError):
        await WikiStore.open(tmp_path)


async def test_foreign_keys_and_fixed_relation_types_are_database_enforced(
    wiki_store: WikiStore,
) -> None:
    db = wiki_store._require_db()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            """
            INSERT INTO wiki_sources (
                id, space_id, display_name, mime_type, size_bytes,
                source_sha256, source_relpath, created_at_ms, updated_at_ms
            ) VALUES ('source_x', 'space_ffffffffffffffffffffffff', 'x.pdf',
                      'application/pdf', 1, ?, 'raw/x/source.pdf', 1, 1)
            """,
            ("a" * 64,),
        )

    space = await wiki_store.create_space(name="Relations")
    await db.execute(
        """
        INSERT INTO wiki_change_sets (
            id, space_id, status, base_graph_revision, created_at_ms
        ) VALUES ('change_1', ?, 'draft', 0, 1)
        """,
        (space.id,),
    )
    for page_id, slug in (("page_1", "one"), ("page_2", "two")):
        await db.execute(
            """
            INSERT INTO wiki_pages (
                id, space_id, slug, title, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, 1, 1)
            """,
            (page_id, space.id, slug, slug),
        )
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            """
            INSERT INTO wiki_edges (
                id, space_id, from_page_id, to_page_id, relation_type,
                change_set_id, created_at_ms
            ) VALUES ('edge_1', ?, 'page_1', 'page_2', 'derived_from',
                      'change_1', 1)
            """,
            (space.id,),
        )


async def test_create_space_builds_fixed_layout_and_canonical_manifest(
    wiki_store: WikiStore,
    tmp_path: Path,
) -> None:
    space = await wiki_store.create_space(name="产品 Wiki", description="说明")
    directory = tmp_path / "spaces" / space.id
    assert (directory / "raw").is_dir()
    assert (directory / "pages").is_dir()
    payload = json.loads((directory / "space.json").read_text(encoding="utf-8"))
    assert payload["schema"] == WIKI_SPACE_MANIFEST_SCHEMA
    assert payload["space"] == space.model_dump(mode="json")
    assert not list(directory.glob(".*.tmp"))


async def test_space_list_update_filter_and_compare_and_swap(
    wiki_store: WikiStore,
) -> None:
    first = await wiki_store.create_space(name="First")
    second = await wiki_store.create_space(name="Second")
    assert [space.id for space in await wiki_store.list_spaces()] == [first.id, second.id]

    updated = await wiki_store.update_space(
        first.id,
        name="Updated",
        description="new",
        expected_updated_at_ms=first.updated_at_ms,
    )
    assert updated.name == "Updated"
    assert updated.updated_at_ms > first.updated_at_ms
    with pytest.raises(WikiStoreError) as exc_info:
        await wiki_store.update_space(
            first.id,
            description="stale",
            expected_updated_at_ms=first.updated_at_ms,
        )
    assert exc_info.value.code == "space_conflict"
    assert await wiki_store.get_space(first.id) == updated

    archived = await wiki_store.set_space_status(second.id, "archived")
    assert await wiki_store.list_spaces(statuses=("archived",)) == (archived,)


async def test_compare_and_swap_holds_across_two_store_connections(tmp_path: Path) -> None:
    first = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_dddddddddddddddddddddddd",
    )
    space = await first.create_space(name="Shared")
    second = await WikiStore.open(tmp_path)
    try:
        updated = await first.update_space(
            space.id,
            description="first",
            expected_updated_at_ms=space.updated_at_ms,
        )
        with pytest.raises(WikiStoreError) as exc_info:
            await second.update_space(
                space.id,
                description="second",
                expected_updated_at_ms=space.updated_at_ms,
            )
        assert exc_info.value.code == "space_conflict"
        assert await second.get_space(space.id) == updated
    finally:
        await second.close()
        await first.close()


async def test_space_status_transitions_and_delete_are_soft(
    wiki_store: WikiStore,
    tmp_path: Path,
) -> None:
    space = await wiki_store.create_space(name="Lifecycle")
    archived = await wiki_store.set_space_status(space.id, "archived")
    active = await wiki_store.set_space_status(archived.id, "active")
    deleting = await wiki_store.delete_space(
        active.id,
        expected_updated_at_ms=active.updated_at_ms,
    )
    assert deleting.status == "deleting"
    assert (tmp_path / "spaces" / space.id).is_dir()
    with pytest.raises(WikiStoreError) as exc_info:
        await wiki_store.set_space_status(space.id, "active")
    assert exc_info.value.code == "invalid_status_transition"


@pytest.mark.parametrize(
    ("name", "description"),
    (("", ""), (" padded ", ""), ("bad\x00name", ""), ("x", "bad\rtext")),
)
async def test_invalid_space_content_is_rejected_without_database_or_directory(
    wiki_store: WikiStore,
    tmp_path: Path,
    name: str,
    description: str,
) -> None:
    with pytest.raises(WikiStoreError) as exc_info:
        await wiki_store.create_space(name=name, description=description)
    assert exc_info.value.code == "invalid_space"
    assert await wiki_store.list_spaces() == ()
    assert list((tmp_path / "spaces").iterdir()) == []


async def test_missing_and_invalid_space_ids_have_fixed_errors(
    wiki_store: WikiStore,
) -> None:
    with pytest.raises(WikiStoreError) as invalid:
        await wiki_store.get_space("../escape")
    assert invalid.value.code == "invalid_identifier"
    with pytest.raises(WikiStoreError) as missing:
        await wiki_store.get_space("space_ffffffffffffffffffffffff")
    assert missing.value.code == "space_not_found"


async def test_concurrent_space_creates_are_serialized_and_persisted(tmp_path: Path) -> None:
    store = await WikiStore.open(tmp_path)
    try:
        spaces = await asyncio.gather(
            *(store.create_space(name=f"Space {index}") for index in range(12))
        )
        assert len({space.id for space in spaces}) == 12
        assert len(await store.list_spaces()) == 12
        assert all((tmp_path / "spaces" / space.id / "space.json").is_file() for space in spaces)
    finally:
        await store.close()


async def test_startup_rebuilds_missing_or_corrupt_space_mirror(tmp_path: Path) -> None:
    store = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_bbbbbbbbbbbbbbbbbbbbbbbb",
    )
    space = await store.create_space(name="Repair")
    await store.close()
    shutil.rmtree(tmp_path / "spaces" / space.id)

    reopened = await WikiStore.open(tmp_path)
    try:
        assert reopened.startup_repair_report.repaired_space_ids == (space.id,)
        manifest = tmp_path / "spaces" / space.id / "space.json"
        assert manifest.is_file()
        manifest.write_text("corrupt", encoding="utf-8")
        report = await reopened.repair_space_mirrors()
        assert report.repaired_space_ids == (space.id,)
        assert json.loads(manifest.read_text(encoding="utf-8"))["space"]["id"] == space.id
    finally:
        await reopened.close()


async def test_startup_reports_orphan_and_quarantines_private_staging(
    tmp_path: Path,
) -> None:
    first = await WikiStore.open(tmp_path)
    await first.close()
    orphan_id = "space_cccccccccccccccccccccccc"
    orphan = tmp_path / "spaces" / orphan_id
    orphan.mkdir()
    staging = tmp_path / "spaces" / ".creating-crash"
    staging.mkdir()
    (staging / "partial").write_text("evidence", encoding="utf-8")

    reopened = await WikiStore.open(tmp_path)
    try:
        report = reopened.startup_repair_report
        assert report.orphan_space_ids == (orphan_id,)
        assert report.quarantined_staging_count == 1
        assert not staging.exists()
        recovery = tmp_path / "legacy" / "recovery"
        assert any(path.name.startswith(".creating-crash-") for path in recovery.iterdir())
    finally:
        await reopened.close()


async def test_closed_store_rejects_operations(tmp_path: Path) -> None:
    store = await WikiStore.open(tmp_path)
    await store.close()
    with pytest.raises(WikiStoreError):
        await store.list_spaces()


async def test_database_special_file_is_rejected_before_connect(tmp_path: Path) -> None:
    (tmp_path / "wiki.db").mkdir()
    with pytest.raises(WikiStoreError) as exc_info:
        await WikiStore.open(tmp_path)
    assert exc_info.value.code == "path_unsafe"


async def test_database_symlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside-evidence")
    link = tmp_path / "wiki.db"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this Windows account")

    with pytest.raises(WikiStoreError) as exc_info:
        await WikiStore.open(tmp_path)
    assert exc_info.value.code == "path_unsafe"
    assert outside.read_bytes() == b"outside-evidence"
