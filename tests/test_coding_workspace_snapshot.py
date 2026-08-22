"""Security and determinism tests for local project snapshot creation."""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_sandbox import (
    SnapshotError,
    SnapshotManifest,
    SnapshotManifestEntry,
    SnapshotPolicy,
    build_project_snapshot,
    validate_snapshot_archive,
)


def _add_file(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(content))


def _write_archive(path: Path, members: tuple[tuple[tarfile.TarInfo, bytes], ...]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for info, content in members:
            archive.addfile(info, io.BytesIO(content))


@pytest.mark.asyncio
async def test_snapshot_selects_regular_project_files_and_excludes_secrets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules").mkdir()
    (root / "src" / "main.py").write_text("print('你好')\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / "private.pem").write_text("secret\n", encoding="utf-8")
    (root / ".git" / "config").write_text("git secret\n", encoding="utf-8")
    (root / "node_modules" / "dependency.js").write_text("ignored\n", encoding="utf-8")

    snapshot = await build_project_snapshot(root, tmp_path / "snapshot.tar.gz")

    assert tuple(entry.path for entry in snapshot.manifest.entries) == (
        "README.md",
        "src/main.py",
    )
    assert snapshot.manifest.file_count == 2
    assert snapshot.archive_size == snapshot.archive_path.stat().st_size
    assert validate_snapshot_archive(snapshot.archive_path) == snapshot.manifest


@pytest.mark.asyncio
async def test_snapshot_archive_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "b.txt").write_bytes(b"second")
    (root / "a.txt").write_bytes(b"first")

    first = await build_project_snapshot(root, tmp_path / "first.tar.gz")
    second = await build_project_snapshot(root, tmp_path / "second.tar.gz")

    assert first.archive_sha256 == second.archive_sha256
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.manifest == second.manifest


@pytest.mark.asyncio
async def test_snapshot_rejects_archive_destination_inside_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises(SnapshotError) as exc_info:
        await build_project_snapshot(root, root / "snapshot.tar.gz")

    assert exc_info.value.code == "unsafe_path"
    assert not (root / "snapshot.tar.gz").exists()


@pytest.mark.asyncio
async def test_snapshot_rejects_missing_root_as_invalid_root(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError) as exc_info:
        await build_project_snapshot(tmp_path / "missing", tmp_path / "snapshot.tar.gz")

    assert exc_info.value.code == "invalid_root"


@pytest.mark.asyncio
async def test_snapshot_enforces_per_file_total_and_count_quotas(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_bytes(b"1234")
    (root / "b.txt").write_bytes(b"5678")

    cases = (
        (SnapshotPolicy(max_file_bytes=3), "file_too_large"),
        (SnapshotPolicy(max_total_bytes=7), "snapshot_too_large"),
        (SnapshotPolicy(max_file_count=1), "too_many_files"),
    )
    for index, (policy, expected_code) in enumerate(cases):
        with pytest.raises(SnapshotError) as exc_info:
            await build_project_snapshot(
                root,
                tmp_path / f"snapshot-{index}.tar.gz",
                policy=policy,
            )
        assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_snapshot_rejects_symlinks_in_selected_tree(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be read\n", encoding="utf-8")
    link = root / "linked.txt"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SnapshotError) as exc_info:
        await build_project_snapshot(root, tmp_path / "snapshot.tar.gz")

    assert exc_info.value.code == "unsafe_path"
    assert exc_info.value.relative_path == "linked.txt"


def test_manifest_rejects_case_insensitive_path_collisions() -> None:
    entries = (
        SnapshotManifestEntry(path="A.py", size=0, sha256="0" * 64, mode=0o644),
        SnapshotManifestEntry(path="a.py", size=0, sha256="0" * 64, mode=0o644),
    )

    with pytest.raises(ValidationError, match="case-insensitively unique"):
        SnapshotManifest(
            entries=entries,
            file_count=2,
            total_bytes=0,
            manifest_sha256="0" * 64,
        )


def test_archive_validation_rejects_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "traversal.tar.gz"
    info = tarfile.TarInfo("../escape.txt")
    info.size = 6
    _write_archive(archive_path, ((info, b"escape"),))

    with pytest.raises(SnapshotError) as exc_info:
        validate_snapshot_archive(archive_path)

    assert exc_info.value.code == "archive_invalid"


def test_archive_validation_rejects_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "link.tar.gz"
    info = tarfile.TarInfo("workspace/link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    _write_archive(archive_path, ((info, b""),))

    with pytest.raises(SnapshotError) as exc_info:
        validate_snapshot_archive(archive_path)

    assert exc_info.value.code == "archive_invalid"


@pytest.mark.asyncio
async def test_archive_validation_hashes_content_against_manifest(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "value.txt").write_bytes(b"good")
    snapshot = await build_project_snapshot(root, tmp_path / "valid.tar.gz")
    with tarfile.open(snapshot.archive_path, mode="r:gz") as source:
        manifest_file = source.extractfile("metadata/base-manifest.json")
        assert manifest_file is not None
        manifest_bytes = manifest_file.read()

    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, mode="w:gz") as archive:
        _add_file(archive, "workspace/value.txt", b"evil")
        _add_file(archive, "metadata/base-manifest.json", manifest_bytes)

    with pytest.raises(SnapshotError) as exc_info:
        validate_snapshot_archive(tampered)

    assert exc_info.value.code == "archive_invalid"
    assert json.loads(manifest_bytes)["entries"][0]["sha256"] != "0" * 64


def test_archive_validation_requires_exactly_one_manifest(tmp_path: Path) -> None:
    archive_path = tmp_path / "missing-manifest.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        _add_file(archive, "workspace/main.py", b"pass\n")

    with pytest.raises(SnapshotError) as exc_info:
        validate_snapshot_archive(archive_path)

    assert exc_info.value.code == "archive_invalid"
