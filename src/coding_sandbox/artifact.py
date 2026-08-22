"""Immutable, self-verifying coding Sandbox output artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import stat
import string
import tarfile
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import validate_sandbox_path
from .validation import SandboxValidationEvidence
from .workspace_models import SandboxFileEntry, validate_workspace_relative_path

ARTIFACT_SCHEMA_VERSION: Literal["pi-agent-coding-artifact/v1"] = "pi-agent-coding-artifact/v1"
ARTIFACT_SIGNATURE_CONTEXT: Literal["pi-agent-coding-artifact-signature/v1"] = (
    "pi-agent-coding-artifact-signature/v1"
)
ARTIFACT_MANIFEST_PATH = "metadata/manifest.json"
ARTIFACT_VALIDATION_PATH = "metadata/validation-evidence.json"
ARTIFACT_BINARY_DIFF_PATH = "metadata/binary-diff.json"
ARTIFACT_DELETED_FILES_PATH = "metadata/deleted-files.json"
ARTIFACT_FILES_PREFIX = "files/"
MAX_ARTIFACT_METADATA_BYTES = 32 * 1024 * 1024

ArtifactChangedStatus = Literal["added", "modified"]
ArtifactDiffStatus = Literal["added", "modified", "deleted"]


def canonical_json_bytes(value: object) -> bytes:
    """Encode security-sensitive metadata with one deterministic JSON form."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def is_binary_content(value: bytes) -> bool:
    """Use one conservative, deterministic text/binary classification."""
    if b"\x00" in value:
        return True
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


class SandboxArtifactChangedFile(BaseModel):
    """One added or modified payload embedded under ``files/``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    status: ArtifactChangedStatus
    before_size: int | None = Field(default=None, ge=0)
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    before_binary: bool | None = None
    after_size: int = Field(ge=0)
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_binary: bool
    binary: bool
    member_path: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)

    @model_validator(mode="after")
    def _validate_change(self) -> SandboxArtifactChangedFile:
        before = (self.before_size, self.before_sha256, self.before_binary)
        if self.status == "added" and before != (None, None, None):
            raise ValueError("an added artifact file cannot have baseline metadata")
        if self.status == "modified" and any(value is None for value in before):
            raise ValueError("a modified artifact file requires baseline metadata")
        if self.member_path != ARTIFACT_FILES_PREFIX + self.path:
            raise ValueError("artifact payload member path does not match file path")
        if self.binary != (bool(self.before_binary) or self.after_binary):
            raise ValueError("artifact binary classification is inconsistent")
        return self


class SandboxArtifactDeletedFile(BaseModel):
    """One baseline file absent from the frozen workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    before_size: int = Field(ge=0)
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_binary: bool

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


class SandboxArtifactBinaryDiffEntry(BaseModel):
    """Hash-level binary replacement/deletion evidence; payloads remain exact bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    status: ArtifactDiffStatus
    before_size: int | None = Field(default=None, ge=0)
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_size: int | None = Field(default=None, ge=0)
    after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)

    @model_validator(mode="after")
    def _validate_sides(self) -> SandboxArtifactBinaryDiffEntry:
        before = (self.before_size, self.before_sha256)
        after = (self.after_size, self.after_sha256)
        if self.status == "added" and (before != (None, None) or None in after):
            raise ValueError("binary added entry has inconsistent sides")
        if self.status == "modified" and (None in before or None in after):
            raise ValueError("binary modified entry requires both sides")
        if self.status == "deleted" and (None in before or after != (None, None)):
            raise ValueError("binary deleted entry has inconsistent sides")
        return self


class SandboxArtifactManifest(BaseModel):
    """Canonical manifest embedded in and cryptographically bound to the archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-coding-artifact/v1"] = ARTIFACT_SCHEMA_VERSION
    artifact_id: str = Field(pattern=r"^artifact-[0-9a-f]{32}$")
    operation_id: str = Field(min_length=1, max_length=128)
    workspace_revision: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_files: tuple[SandboxArtifactChangedFile, ...]
    deleted_files: tuple[SandboxArtifactDeletedFile, ...]
    binary_diff: tuple[SandboxArtifactBinaryDiffEntry, ...]
    changed_file_count: int = Field(ge=0)
    deleted_file_count: int = Field(ge=0)
    binary_diff_count: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_manifest(self) -> SandboxArtifactManifest:
        changed_paths = tuple(entry.path for entry in self.changed_files)
        deleted_paths = tuple(entry.path for entry in self.deleted_files)
        if changed_paths != tuple(sorted(changed_paths)):
            raise ValueError("artifact changed files must be sorted")
        if deleted_paths != tuple(sorted(deleted_paths)):
            raise ValueError("artifact deleted files must be sorted")
        all_paths = changed_paths + deleted_paths
        if len({path.casefold() for path in all_paths}) != len(all_paths):
            raise ValueError("artifact paths must be case-insensitively unique")
        if (
            self.changed_file_count != len(self.changed_files)
            or self.deleted_file_count != len(self.deleted_files)
            or self.binary_diff_count != len(self.binary_diff)
            or self.payload_bytes != sum(entry.after_size for entry in self.changed_files)
        ):
            raise ValueError("artifact manifest counts are inconsistent")
        expected_binary = _binary_diff_from_entries(
            self.changed_files,
            self.deleted_files,
        )
        if self.binary_diff != expected_binary:
            raise ValueError("artifact binary diff does not match manifest entries")
        if self.manifest_sha256 != artifact_manifest_digest(self):
            raise ValueError("artifact manifest digest is inconsistent")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class SandboxArtifactContents(BaseModel):
    """Fully revalidated contents of a downloaded archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: SandboxArtifactManifest
    validation_evidence: SandboxValidationEvidence


class SandboxArtifactSignature(BaseModel):
    """Server signature over the verified archive and embedded manifest digests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    key_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    signed_at_ms: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class SandboxOutputArtifact(BaseModel):
    """Content-addressed local artifact plus its server-generated signature."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_path: Path
    archive_size: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: SandboxArtifactManifest
    validation_evidence: SandboxValidationEvidence
    signature: SandboxArtifactSignature

    @model_validator(mode="after")
    def _validate_links(self) -> SandboxOutputArtifact:
        if (
            self.signature.archive_sha256 != self.archive_sha256
            or self.signature.manifest_sha256 != self.manifest.manifest_sha256
            or self.validation_evidence.operation_id != self.manifest.operation_id
            or self.validation_evidence.workspace_revision != self.manifest.workspace_revision
            or self.validation_evidence.workspace_sha256_after != self.manifest.workspace_sha256
        ):
            raise ValueError("artifact receipt fields are inconsistent")
        return self


class SandboxRemoteArtifactReceipt(BaseModel):
    """Small fixed response produced before the server downloads the archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    remote_path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("remote_path")
    @classmethod
    def _validate_remote_path(cls, value: str) -> str:
        return validate_sandbox_path(value)


class ArtifactSigner(Protocol):
    """Server-owned signing boundary; implementations must never expose key bytes."""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


class HMACSHA256ArtifactSigner:
    """Small stdlib signer suitable for a Keyring-resolved server secret."""

    def __init__(self, *, key_id: str, secret: bytes) -> None:
        if (
            not key_id
            or len(key_id) > 128
            or any(char not in string.ascii_letters + string.digits + "_.-" for char in key_id)
        ):
            raise ValueError("artifact signing key id is invalid")
        if len(secret) < 32 or len(secret) > 4096:
            raise ValueError("artifact signing secret must contain 32-4096 bytes")
        self._key_id = key_id
        self._secret = bytes(secret)

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, payload: bytes) -> str:
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)

    def __repr__(self) -> str:
        return f"HMACSHA256ArtifactSigner(key_id={self._key_id!r})"


def artifact_manifest_digest(manifest: SandboxArtifactManifest) -> str:
    core = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    return hashlib.sha256(canonical_json_bytes(core)).hexdigest()


def baseline_manifest_digest(entries: tuple[SandboxFileEntry, ...]) -> str:
    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    payload = [entry.model_dump(mode="json") for entry in ordered]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def validation_evidence_bytes(evidence: SandboxValidationEvidence) -> bytes:
    return canonical_json_bytes(evidence.model_dump(mode="json"))


def create_artifact_signature(
    signer: ArtifactSigner,
    *,
    archive_sha256: str,
    manifest_sha256: str,
    signed_at_ms: int,
) -> SandboxArtifactSignature:
    payload = artifact_signature_payload(
        key_id=signer.key_id,
        signed_at_ms=signed_at_ms,
        archive_sha256=archive_sha256,
        manifest_sha256=manifest_sha256,
    )
    return SandboxArtifactSignature(
        key_id=signer.key_id,
        signed_at_ms=signed_at_ms,
        archive_sha256=archive_sha256,
        manifest_sha256=manifest_sha256,
        signature=signer.sign(payload),
    )


def artifact_signature_payload(
    *,
    key_id: str,
    signed_at_ms: int,
    archive_sha256: str,
    manifest_sha256: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "context": ARTIFACT_SIGNATURE_CONTEXT,
            "algorithm": "hmac-sha256",
            "key_id": key_id,
            "signed_at_ms": signed_at_ms,
            "archive_sha256": archive_sha256,
            "manifest_sha256": manifest_sha256,
        }
    )


def verify_artifact_signature(
    signer: ArtifactSigner,
    signature: SandboxArtifactSignature,
) -> bool:
    if signer.key_id != signature.key_id:
        return False
    payload = artifact_signature_payload(
        key_id=signature.key_id,
        signed_at_ms=signature.signed_at_ms,
        archive_sha256=signature.archive_sha256,
        manifest_sha256=signature.manifest_sha256,
    )
    return signer.verify(payload, signature.signature)


def validate_sandbox_artifact_archive(
    archive_path: Path,
    *,
    max_archive_bytes: int,
    max_file_bytes: int,
    max_file_count: int,
) -> SandboxArtifactContents:
    """Recompute every downloaded member digest without extracting the tar."""
    _validate_archive_file(archive_path, max_archive_bytes=max_archive_bytes)
    members: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            seen_names: set[str] = set()
            for member in archive:
                _validate_member(member)
                folded = member.name.casefold()
                if folded in seen_names:
                    raise ValueError("artifact archive has duplicate members")
                seen_names.add(folded)
                members[member.name] = member
            manifest_bytes = _read_metadata_member(
                archive,
                members,
                ARTIFACT_MANIFEST_PATH,
            )
            manifest = SandboxArtifactManifest.model_validate_json(manifest_bytes)
            if manifest.canonical_bytes() != manifest_bytes:
                raise ValueError("artifact manifest is not canonical")
            evidence_bytes = _read_metadata_member(
                archive,
                members,
                ARTIFACT_VALIDATION_PATH,
            )
            evidence = SandboxValidationEvidence.model_validate_json(evidence_bytes)
            if validation_evidence_bytes(evidence) != evidence_bytes:
                raise ValueError("artifact validation evidence is not canonical")
            binary_bytes = _read_metadata_member(
                archive,
                members,
                ARTIFACT_BINARY_DIFF_PATH,
            )
            deleted_bytes = _read_metadata_member(
                archive,
                members,
                ARTIFACT_DELETED_FILES_PATH,
            )
            if binary_bytes != canonical_json_bytes(
                [entry.model_dump(mode="json") for entry in manifest.binary_diff]
            ) or deleted_bytes != canonical_json_bytes(
                [entry.model_dump(mode="json") for entry in manifest.deleted_files]
            ):
                raise ValueError("artifact metadata files disagree with the manifest")
            expected_names = {
                ARTIFACT_MANIFEST_PATH,
                ARTIFACT_VALIDATION_PATH,
                ARTIFACT_BINARY_DIFF_PATH,
                ARTIFACT_DELETED_FILES_PATH,
                *(entry.member_path for entry in manifest.changed_files),
            }
            if set(members) != expected_names:
                raise ValueError("artifact archive member set is invalid")
            if len(manifest.changed_files) > max_file_count:
                raise ValueError("artifact file count exceeds the limit")
            payload_total = 0
            for entry in manifest.changed_files:
                if entry.after_size > max_file_bytes:
                    raise ValueError("artifact file exceeds the limit")
                member = members[entry.member_path]
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("artifact payload cannot be read")
                size, digest, binary = _digest_member(
                    extracted,
                    declared_size=member.size,
                    max_bytes=max_file_bytes,
                )
                if (
                    size != entry.after_size
                    or digest != entry.after_sha256
                    or binary != entry.after_binary
                ):
                    raise ValueError("artifact payload does not match its manifest")
                payload_total += size
            if payload_total != manifest.payload_bytes:
                raise ValueError("artifact payload total is inconsistent")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("artifact archive is invalid") from exc
    evidence_digest = hashlib.sha256(evidence_bytes).hexdigest()
    if (
        evidence_digest != manifest.validation_evidence_sha256
        or not evidence.passed
        or evidence.operation_id != manifest.operation_id
        or evidence.workspace_revision != manifest.workspace_revision
        or evidence.workspace_sha256_after != manifest.workspace_sha256
    ):
        raise ValueError("artifact validation evidence is inconsistent")
    return SandboxArtifactContents(
        manifest=manifest,
        validation_evidence=evidence,
    )


def hash_regular_file(path: Path, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    info = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError("artifact path is not a regular file")
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("artifact file exceeds the limit")
            digest.update(chunk)
    final = path.stat(follow_symlinks=False)
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) or size != final.st_size:
        raise ValueError("artifact file changed while hashing")
    return size, digest.hexdigest()


def verify_output_artifact(
    artifact: SandboxOutputArtifact,
    signer: ArtifactSigner,
    *,
    max_archive_bytes: int,
    max_file_bytes: int,
    max_file_count: int,
) -> SandboxArtifactContents:
    size, digest = hash_regular_file(
        artifact.archive_path,
        max_bytes=max_archive_bytes,
    )
    if size != artifact.archive_size or digest != artifact.archive_sha256:
        raise ValueError("artifact archive receipt does not match local bytes")
    contents = validate_sandbox_artifact_archive(
        artifact.archive_path,
        max_archive_bytes=max_archive_bytes,
        max_file_bytes=max_file_bytes,
        max_file_count=max_file_count,
    )
    if (
        contents.manifest != artifact.manifest
        or contents.validation_evidence != artifact.validation_evidence
        or not verify_artifact_signature(signer, artifact.signature)
    ):
        raise ValueError("artifact receipt or signature is invalid")
    final_size, final_digest = hash_regular_file(
        artifact.archive_path,
        max_bytes=max_archive_bytes,
    )
    if final_size != size or final_digest != digest:
        raise ValueError("artifact archive changed during verification")
    return contents


def _binary_diff_from_entries(
    changed: tuple[SandboxArtifactChangedFile, ...],
    deleted: tuple[SandboxArtifactDeletedFile, ...],
) -> tuple[SandboxArtifactBinaryDiffEntry, ...]:
    entries = [
        SandboxArtifactBinaryDiffEntry(
            path=entry.path,
            status=entry.status,
            before_size=entry.before_size,
            before_sha256=entry.before_sha256,
            after_size=entry.after_size,
            after_sha256=entry.after_sha256,
        )
        for entry in changed
        if entry.binary
    ]
    entries.extend(
        SandboxArtifactBinaryDiffEntry(
            path=entry.path,
            status="deleted",
            before_size=entry.before_size,
            before_sha256=entry.before_sha256,
        )
        for entry in deleted
        if entry.before_binary
    )
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _validate_archive_file(path: Path, *, max_archive_bytes: int) -> None:
    if max_archive_bytes < 1:
        raise ValueError("artifact limit is invalid")
    info = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > max_archive_bytes:
        raise ValueError("artifact archive file is invalid")


def _validate_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        not member.isfile()
        or not member.name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in member.name
        or "\x00" in member.name
        or str(path) != member.name
        or any(ord(char) < 32 or ord(char) == 127 for char in member.name)
    ):
        raise ValueError("artifact archive member is unsafe")


def _read_metadata_member(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> bytes:
    member = members.get(name)
    if member is None or member.size > MAX_ARTIFACT_METADATA_BYTES:
        raise ValueError("artifact metadata member is missing or too large")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ValueError("artifact metadata member cannot be read")
    value = extracted.read(MAX_ARTIFACT_METADATA_BYTES + 1)
    if len(value) != member.size:
        raise ValueError("artifact metadata member size is invalid")
    return value


def _digest_member(
    stream: object,
    *,
    declared_size: int,
    max_bytes: int,
) -> tuple[int, str, bool]:
    digest = hashlib.sha256()
    collected = bytearray()
    size = 0
    reader = getattr(stream, "read", None)
    if reader is None:
        raise ValueError("artifact payload stream is invalid")
    while chunk := reader(1024 * 1024):
        if not isinstance(chunk, bytes):
            raise ValueError("artifact payload stream returned invalid data")
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("artifact payload exceeds the limit")
        digest.update(chunk)
        collected.extend(chunk)
    if size != declared_size:
        raise ValueError("artifact payload declared size is invalid")
    return size, digest.hexdigest(), is_binary_content(bytes(collected))


__all__ = [
    "ARTIFACT_BINARY_DIFF_PATH",
    "ARTIFACT_DELETED_FILES_PATH",
    "ARTIFACT_FILES_PREFIX",
    "ARTIFACT_MANIFEST_PATH",
    "ARTIFACT_SCHEMA_VERSION",
    "ARTIFACT_SIGNATURE_CONTEXT",
    "ARTIFACT_VALIDATION_PATH",
    "ArtifactSigner",
    "HMACSHA256ArtifactSigner",
    "SandboxArtifactBinaryDiffEntry",
    "SandboxArtifactChangedFile",
    "SandboxArtifactContents",
    "SandboxArtifactDeletedFile",
    "SandboxArtifactManifest",
    "SandboxArtifactSignature",
    "SandboxOutputArtifact",
    "SandboxRemoteArtifactReceipt",
    "artifact_manifest_digest",
    "artifact_signature_payload",
    "baseline_manifest_digest",
    "canonical_json_bytes",
    "create_artifact_signature",
    "hash_regular_file",
    "is_binary_content",
    "validate_sandbox_artifact_archive",
    "validation_evidence_bytes",
    "verify_artifact_signature",
    "verify_output_artifact",
]
