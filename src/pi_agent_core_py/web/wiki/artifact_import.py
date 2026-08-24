"""Fail-closed import of untrusted parser tar artifacts into one raw bundle."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import ValidationError

from wiki_parser import (
    ParserArtifactManifest,
    ParserArtifactManifestV2,
    ParserArtifactReceipt,
    ParserArtifactReceiptV2,
    ParserJobStatusV2,
    ParserLimits,
    ParserParsedPage,
    ParserRequestedMode,
    ParserRoutingConfigIdentity,
    canonical_markdown_v2,
    validate_artifact_path,
)

from .errors import WikiStoreError
from .models import WikiArtifact, WikiArtifactKind, WikiSource
from .store import WikiStore

_MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImportedParserBundle:
    """Validated provider evidence and rows ready for one DB commit."""

    manifest: ParserArtifactManifest
    artifacts: tuple[WikiArtifact, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class ImportedParserBundleV2:
    """Fully validated Contract v2 evidence ready for one atomic DB commit."""

    manifest: ParserArtifactManifestV2
    artifacts: tuple[WikiArtifact, ...]
    page_count: int


def _read_archive(path: Path, max_bytes: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise WikiStoreError("invalid_artifact")
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise WikiStoreError("invalid_artifact")
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except WikiStoreError:
        raise
    except OSError as exc:
        raise WikiStoreError("invalid_artifact") from exc
    before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_id != after_id or len(payload) != before.st_size:
        raise WikiStoreError("invalid_artifact")
    return payload


def _validate_image_magic(mime_type: str, payload: bytes) -> None:
    valid = False
    if mime_type == "image/png":
        valid = payload.startswith(b"\x89PNG\r\n\x1a\n")
    elif mime_type == "image/jpeg":
        valid = payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")
    elif mime_type == "image/webp":
        valid = len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    if not valid:
        raise WikiStoreError("invalid_artifact")


def _read_tar_files(archive: bytes, limits: ParserLimits) -> dict[str, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            members = bundle.getmembers()
            if len(members) > limits.max_artifact_files:
                raise WikiStoreError("invalid_artifact")
            names: set[str] = set()
            files: dict[str, bytes] = {}
            total = 0
            for member in members:
                if not member.isfile() or member.name == "":
                    raise WikiStoreError("invalid_artifact")
                if member.name != "manifest.json":
                    validate_artifact_path(member.name)
                folded = member.name.casefold()
                if folded in names:
                    raise WikiStoreError("invalid_artifact")
                names.add(folded)
                # Contract v2 explicitly permits an empty per-page Markdown file.
                # Kind-specific non-empty rules are enforced after parsing the manifest.
                if member.size < 0 or member.size > limits.max_artifact_bytes:
                    raise WikiStoreError("invalid_artifact")
                total += member.size
                if total > limits.max_artifact_bytes:
                    raise WikiStoreError("invalid_artifact")
                stream = bundle.extractfile(member)
                if stream is None:
                    raise WikiStoreError("invalid_artifact")
                payload = stream.read(member.size + 1)
                if len(payload) != member.size:
                    raise WikiStoreError("invalid_artifact")
                files[member.name] = payload
    except WikiStoreError:
        raise
    except (tarfile.TarError, ValueError) as exc:
        raise WikiStoreError("invalid_artifact") from exc
    return files


def _validate_manifest(
    files: dict[str, bytes],
    *,
    source: WikiSource,
    job_id: str,
    receipt: ParserArtifactReceipt,
    limits: ParserLimits,
    expected_provider: str,
    expected_provider_version: str,
) -> ParserArtifactManifest:
    manifest_bytes = files.get("manifest.json")
    if manifest_bytes is None or len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise WikiStoreError("invalid_artifact")
    if hashlib.sha256(manifest_bytes).hexdigest() != receipt.manifest_sha256:
        raise WikiStoreError("invalid_artifact")
    try:
        manifest = ParserArtifactManifest.model_validate_json(manifest_bytes)
    except ValidationError as exc:
        raise WikiStoreError("invalid_artifact") from exc
    if (
        manifest.job_id != job_id
        or manifest.source_id != source.id
        or manifest.source_sha256 != source.source_sha256
        or receipt.job_id != job_id
        or receipt.source_id != source.id
        or manifest.provider != expected_provider
        or manifest.provider_version != expected_provider_version
    ):
        raise WikiStoreError("invalid_artifact")
    declared = {manifest.markdown.path, *(image.path for image in manifest.images)}
    if set(files) != {"manifest.json", *declared}:
        raise WikiStoreError("invalid_artifact")
    if len(files) != receipt.file_count or len(manifest.images) > limits.max_image_count:
        raise WikiStoreError("invalid_artifact")
    for entry in (manifest.markdown, *manifest.images):
        payload = files[entry.path]
        if len(payload) != entry.size_bytes:
            raise WikiStoreError("invalid_artifact")
        if hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise WikiStoreError("invalid_artifact")
        if entry.kind == "markdown":
            try:
                payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WikiStoreError("invalid_artifact") from exc
        else:
            if len(payload) > limits.max_image_bytes:
                raise WikiStoreError("invalid_artifact")
            _validate_image_magic(entry.mime_type, payload)
    return manifest


def import_parser_artifact(
    store: WikiStore,
    source: WikiSource,
    *,
    job_id: str,
    archive_path: Path,
    receipt: ParserArtifactReceipt,
    limits: ParserLimits,
    expected_provider: str,
    expected_provider_version: str,
    parse_revision_id: str,
) -> ImportedParserBundle:
    """Validate all bytes first, then idempotently materialize owned files."""
    archive = _read_archive(archive_path, limits.max_artifact_bytes)
    if len(archive) != receipt.size_bytes or hashlib.sha256(archive).hexdigest() != receipt.sha256:
        raise WikiStoreError("invalid_artifact")
    files = _read_tar_files(archive, limits)
    manifest = _validate_manifest(
        files,
        source=source,
        job_id=job_id,
        receipt=receipt,
        limits=limits,
        expected_provider=expected_provider,
        expected_provider_version=expected_provider_version,
    )
    bundle_dir = PurePosixPath(
        store.file_store.parse_revision_relative_path(source, parse_revision_id)
    )
    artifact_rows: list[WikiArtifact] = []
    entries = (manifest.markdown, *manifest.images)
    for entry in entries:
        relpath = str(bundle_dir / entry.path)
        payload = files[entry.path]
        store.file_store.write_owned_file_once_or_verify(
            source.space_id,
            relpath,
            payload,
            max_bytes=limits.max_image_bytes if entry.kind == "embedded_image" else len(payload),
        )
        locator = "{}"
        if entry.page_number is not None:
            locator = json.dumps(
                {"page_number": entry.page_number},
                sort_keys=True,
                separators=(",", ":"),
            )
        artifact_rows.append(
            store.new_artifact(
                source.id,
                parse_revision_id=parse_revision_id,
                kind=("parsed_markdown" if entry.kind == "markdown" else "embedded_image"),
                relpath=relpath,
                mime_type=entry.mime_type,
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
                source_locator_json=locator,
            )
        )
    page_count = max(manifest.page_count, 1)
    markdown_payload = files[manifest.markdown.path]
    for page_number in range(1, page_count + 1):
        page_payload = markdown_payload if page_number == 1 else b""
        page_relpath = str(bundle_dir / f"pages/{page_number:06d}.md")
        store.file_store.write_owned_file_once_or_verify(
            source.space_id,
            page_relpath,
            page_payload,
            max_bytes=max(len(page_payload), 1),
            allow_empty=True,
        )
        artifact_rows.append(
            store.new_artifact(
                source.id,
                parse_revision_id=parse_revision_id,
                kind="page_markdown",
                relpath=page_relpath,
                mime_type="text/markdown",
                size_bytes=len(page_payload),
                sha256=hashlib.sha256(page_payload).hexdigest(),
                source_locator_json=json.dumps(
                    {"page_number": page_number},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    manifest_relpath = str(bundle_dir / "manifest.json")
    manifest_bytes = files["manifest.json"]
    store.file_store.write_owned_file_once_or_verify(
        source.space_id,
        manifest_relpath,
        manifest_bytes,
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    artifact_rows.append(
        store.new_artifact(
            source.id,
            parse_revision_id=parse_revision_id,
            kind="manifest",
            relpath=manifest_relpath,
            mime_type="application/json",
            size_bytes=len(manifest_bytes),
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
    )
    return ImportedParserBundle(
        manifest=manifest,
        artifacts=tuple(artifact_rows),
        page_count=page_count,
    )


def _validate_manifest_v2(
    files: dict[str, bytes],
    *,
    source: WikiSource,
    job_id: str,
    requested_mode: ParserRequestedMode,
    receipt: ParserArtifactReceiptV2,
    status: ParserJobStatusV2,
    limits: ParserLimits,
    expected_provider: str,
    expected_routing_config: ParserRoutingConfigIdentity,
) -> ParserArtifactManifestV2:
    manifest_bytes = files.get("manifest.json")
    if (
        manifest_bytes is None
        or not manifest_bytes
        or len(manifest_bytes) > _MAX_MANIFEST_BYTES
        or hashlib.sha256(manifest_bytes).hexdigest() != receipt.manifest_sha256
    ):
        raise WikiStoreError("invalid_artifact")
    try:
        manifest = ParserArtifactManifestV2.model_validate_json(manifest_bytes)
    except ValidationError as exc:
        raise WikiStoreError("invalid_artifact") from exc
    if (
        receipt.job_id != job_id
        or receipt.source_id != source.id
        or status.handle.provider != expected_provider
        or status.handle.job_id != job_id
        or status.handle.source_id != source.id
        or status.state != "succeeded"
        or status.artifact_size_bytes != receipt.size_bytes
        or status.artifact_sha256 != receipt.sha256
        or manifest.job_id != job_id
        or manifest.source_id != source.id
        or manifest.source_sha256 != source.source_sha256
        or manifest.requested_mode != requested_mode
        or manifest.routing_config != expected_routing_config
        or status.route_decision != manifest.route_decision
        or status.attempts != manifest.attempts
    ):
        raise WikiStoreError("invalid_artifact")
    declared = {
        manifest.markdown.path,
        *(entry.path for entry in manifest.pages),
        *(entry.path for entry in manifest.assets),
    }
    if (
        set(files) != {"manifest.json", *declared}
        or len(files) != receipt.file_count
        or len(manifest.assets) > limits.max_image_count
    ):
        raise WikiStoreError("invalid_artifact")
    for entry in (manifest.markdown, *manifest.pages, *manifest.assets):
        payload = files[entry.path]
        if (
            len(payload) != entry.size_bytes
            or hashlib.sha256(payload).hexdigest() != entry.sha256
        ):
            raise WikiStoreError("invalid_artifact")
        if entry.kind in {"document_markdown", "page_markdown"}:
            try:
                markdown = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise WikiStoreError("invalid_artifact") from exc
            if "\ufffd" in markdown or "\x00" in markdown:
                raise WikiStoreError("invalid_artifact")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in markdown):
                raise WikiStoreError("invalid_artifact")
        else:
            if len(payload) > limits.max_image_bytes:
                raise WikiStoreError("invalid_artifact")
            _validate_image_magic(entry.mime_type, payload)

    parsed_pages: list[ParserParsedPage] = []
    try:
        for entry in manifest.pages:
            markdown = files[entry.path].decode("utf-8", errors="strict")
            plain_text = markdown
            parsed_pages.append(
                ParserParsedPage(
                    page_number=entry.page_number,
                    markdown=markdown,
                    markdown_sha256=entry.sha256,
                    plain_text=plain_text,
                    plain_text_sha256=hashlib.sha256(plain_text.encode("utf-8")).hexdigest(),
                )
            )
        canonical = canonical_markdown_v2(tuple(parsed_pages))
    except (UnicodeDecodeError, ValueError, ValidationError) as exc:
        raise WikiStoreError("invalid_artifact") from exc
    if files[manifest.markdown.path] != canonical:
        raise WikiStoreError("invalid_artifact")
    return manifest


def import_parser_artifact_v2(
    store: WikiStore,
    source: WikiSource,
    *,
    job_id: str,
    requested_mode: ParserRequestedMode,
    archive_path: Path,
    receipt: ParserArtifactReceiptV2,
    status: ParserJobStatusV2,
    limits: ParserLimits,
    expected_provider: str,
    expected_routing_config: ParserRoutingConfigIdentity,
    parse_revision_id: str,
) -> ImportedParserBundleV2:
    """Validate a v2 tar completely before materializing its immutable revision."""

    archive = _read_archive(archive_path, limits.max_artifact_bytes)
    if len(archive) != receipt.size_bytes or hashlib.sha256(archive).hexdigest() != receipt.sha256:
        raise WikiStoreError("invalid_artifact")
    files = _read_tar_files(archive, limits)
    manifest = _validate_manifest_v2(
        files,
        source=source,
        job_id=job_id,
        requested_mode=requested_mode,
        receipt=receipt,
        status=status,
        limits=limits,
        expected_provider=expected_provider,
        expected_routing_config=expected_routing_config,
    )
    bundle_dir = PurePosixPath(
        store.file_store.parse_revision_relative_path(source, parse_revision_id)
    )
    artifact_rows: list[WikiArtifact] = []
    for entry in (manifest.markdown, *manifest.pages, *manifest.assets):
        payload = files[entry.path]
        relpath = str(bundle_dir / entry.path)
        is_page = entry.kind == "page_markdown"
        is_image = entry.kind in {"embedded_image", "table_image"}
        store.file_store.write_owned_file_once_or_verify(
            source.space_id,
            relpath,
            payload,
            max_bytes=(limits.max_image_bytes if is_image else max(len(payload), 1)),
            allow_empty=is_page,
        )
        kind = cast(
            WikiArtifactKind,
            {
                "document_markdown": "parsed_markdown",
                "page_markdown": "page_markdown",
                "embedded_image": "embedded_image",
                "table_image": "table_image",
            }[entry.kind],
        )
        locator = "{}"
        if entry.page_number is not None:
            locator = json.dumps(
                {"page_number": entry.page_number},
                sort_keys=True,
                separators=(",", ":"),
            )
        artifact_rows.append(
            store.new_artifact(
                source.id,
                parse_revision_id=parse_revision_id,
                kind=kind,
                relpath=relpath,
                mime_type=entry.mime_type,
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
                source_locator_json=locator,
            )
        )
    manifest_bytes = files["manifest.json"]
    manifest_relpath = str(bundle_dir / "manifest.json")
    store.file_store.write_owned_file_once_or_verify(
        source.space_id,
        manifest_relpath,
        manifest_bytes,
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    artifact_rows.append(
        store.new_artifact(
            source.id,
            parse_revision_id=parse_revision_id,
            kind="manifest",
            relpath=manifest_relpath,
            mime_type="application/json",
            size_bytes=len(manifest_bytes),
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
    )
    return ImportedParserBundleV2(
        manifest=manifest,
        artifacts=tuple(artifact_rows),
        page_count=manifest.page_count,
    )


__all__ = [
    "ImportedParserBundle",
    "ImportedParserBundleV2",
    "import_parser_artifact",
    "import_parser_artifact_v2",
]
