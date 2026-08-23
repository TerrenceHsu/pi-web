"""Filesystem boundary and atomic mirror tests for the new Wiki layout."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import WikiFileStore, WikiPathError, WikiSpace, WikiStoreError

_SPACE_ID = "space_111111111111111111111111"


def _space(*, name: str = "Docs", updated_at_ms: int = 1) -> WikiSpace:
    return WikiSpace(
        id=_SPACE_ID,
        name=name,
        created_at_ms=1,
        updated_at_ms=updated_at_ms,
    )


@pytest.fixture
def file_store(tmp_path: Path) -> WikiFileStore:
    store = WikiFileStore(tmp_path)
    store.ensure_root()
    store.create_or_repair_space_layout(_space())
    return store


def test_fixed_root_and_space_layout_are_idempotent(tmp_path: Path) -> None:
    store = WikiFileStore(tmp_path)
    store.ensure_root()
    assert (tmp_path / "legacy").is_dir()
    assert (tmp_path / "spaces").is_dir()
    assert store.create_or_repair_space_layout(_space()) is True
    assert store.create_or_repair_space_layout(_space()) is False
    assert (tmp_path / "spaces" / _SPACE_ID / "raw").is_dir()
    assert (tmp_path / "spaces" / _SPACE_ID / "pages").is_dir()


def test_space_manifest_is_canonical_and_replaced_atomically(tmp_path: Path) -> None:
    store = WikiFileStore(tmp_path)
    store.ensure_root()
    store.create_or_repair_space_layout(_space())
    manifest = tmp_path / "spaces" / _SPACE_ID / "space.json"
    first = manifest.read_bytes()

    assert store.create_or_repair_space_layout(
        _space(name="Updated", updated_at_ms=2)
    )
    second = manifest.read_bytes()
    payload = json.loads(second)
    assert first != second
    assert payload["space"]["name"] == "Updated"
    assert payload["space"]["updated_at_ms"] == 2
    assert not list(manifest.parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "relative_path",
    (
        "",
        "/raw/source.pdf",
        "raw",
        "other/file.md",
        "../raw/source.pdf",
        "raw/../../escape",
        r"raw\source.pdf",
        "raw//source.pdf",
        "raw/./source.pdf",
        "raw/C:stream",
        "raw/NUL.txt",
        "raw/trailing. ",
        "raw/control\x00.txt",
    ),
)
def test_owned_paths_reject_escape_and_windows_aliases(relative_path: str) -> None:
    with pytest.raises(WikiPathError) as exc_info:
        WikiFileStore.validate_owned_relative_path(relative_path)
    assert exc_info.value.code == "path_unsafe"
    if relative_path:
        assert relative_path not in str(exc_info.value)


def test_raw_write_once_and_page_atomic_replace(file_store: WikiFileStore) -> None:
    raw_hash = file_store.write_owned_file_atomic(
        _SPACE_ID,
        "raw/report--source_1/source.pdf",
        b"%PDF-1.7\n",
        overwrite=False,
        max_bytes=1024,
    )
    assert len(raw_hash) == 64
    with pytest.raises(WikiStoreError) as exists:
        file_store.write_owned_file_atomic(
            _SPACE_ID,
            "raw/report--source_1/source.pdf",
            b"changed",
            overwrite=False,
            max_bytes=1024,
        )
    assert exists.value.code == "file_exists"
    assert (
        file_store.read_owned_file(
            _SPACE_ID,
            "raw/report--source_1/source.pdf",
            max_bytes=1024,
        )
        == b"%PDF-1.7\n"
    )

    file_store.write_owned_file_atomic(
        _SPACE_ID,
        "pages/index.md",
        b"# First\n",
        overwrite=True,
        max_bytes=1024,
    )
    file_store.write_owned_file_atomic(
        _SPACE_ID,
        "pages/index.md",
        b"# Second\n",
        overwrite=True,
        max_bytes=1024,
    )
    assert file_store.read_owned_file(
        _SPACE_ID,
        "pages/index.md",
        max_bytes=1024,
    ) == b"# Second\n"


def test_size_limits_are_checked_before_and_during_read(file_store: WikiFileStore) -> None:
    with pytest.raises(WikiStoreError) as write_error:
        file_store.write_owned_file_atomic(
            _SPACE_ID,
            "pages/large.md",
            b"12345",
            overwrite=True,
            max_bytes=4,
        )
    assert write_error.value.code == "file_too_large"

    file_store.write_owned_file_atomic(
        _SPACE_ID,
        "pages/large.md",
        b"12345",
        overwrite=True,
        max_bytes=5,
    )
    with pytest.raises(WikiStoreError) as read_error:
        file_store.read_owned_file(
            _SPACE_ID,
            "pages/large.md",
            max_bytes=4,
        )
    assert read_error.value.code == "file_too_large"


def test_casefold_collision_is_rejected(file_store: WikiFileStore) -> None:
    file_store.write_owned_file_atomic(
        _SPACE_ID,
        "pages/Topic.md",
        b"one",
        overwrite=True,
        max_bytes=32,
    )
    with pytest.raises(WikiPathError):
        file_store.write_owned_file_atomic(
            _SPACE_ID,
            "pages/topic.md",
            b"two",
            overwrite=True,
            max_bytes=32,
        )


def test_casefold_directory_collision_is_rejected(file_store: WikiFileStore) -> None:
    file_store.write_owned_file_atomic(
        _SPACE_ID,
        "raw/Report/source.pdf",
        b"one",
        overwrite=False,
        max_bytes=32,
    )
    with pytest.raises(WikiPathError):
        file_store.write_owned_file_atomic(
            _SPACE_ID,
            "raw/report/parsed.md",
            b"two",
            overwrite=False,
            max_bytes=32,
        )


def test_failed_replace_keeps_previous_bytes_and_removes_temp(
    file_store: WikiFileStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_store.write_owned_file_atomic(
        _SPACE_ID,
        "pages/note.md",
        b"before",
        overwrite=True,
        max_bytes=32,
    )
    original_replace = os.replace

    def fail_target(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "note.md":
            raise OSError("injected")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_target)
    with pytest.raises(WikiStoreError) as exc_info:
        file_store.write_owned_file_atomic(
            _SPACE_ID,
            "pages/note.md",
            b"after",
            overwrite=True,
            max_bytes=32,
        )
    assert exc_info.value.code == "file_io_failed"

    note = tmp_path / "spaces" / _SPACE_ID / "pages" / "note.md"
    assert note.read_bytes() == b"before"
    assert not list(note.parent.glob(".note.md.*.tmp"))


def test_symlink_escape_is_rejected_when_supported(
    file_store: WikiFileStore,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "spaces" / _SPACE_ID / "raw" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this Windows account")

    with pytest.raises(WikiPathError):
        file_store.write_owned_file_atomic(
            _SPACE_ID,
            "raw/link/escape.txt",
            b"blocked",
            overwrite=False,
            max_bytes=32,
        )
    assert not (outside / "escape.txt").exists()


def test_root_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-root"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this Windows account")

    with pytest.raises(WikiPathError):
        WikiFileStore(link)
