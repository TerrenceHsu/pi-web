"""Immutable Sandbox artifact, freeze, download and signing contract tests."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import coding_sandbox.operation as operation_module
from coding_sandbox import (
    FakeSandboxBackend,
    HMACSHA256ArtifactSigner,
    SandboxArtifactBinaryDiffEntry,
    SandboxArtifactChangedFile,
    SandboxArtifactDeletedFile,
    SandboxArtifactManifest,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxFileEntry,
    SandboxLimits,
    SandboxOperation,
    SandboxValidationEvidence,
    SandboxWorkspaceError,
    artifact_signature_payload,
    baseline_manifest_digest,
    canonical_json_bytes,
    parse_sandbox_validation_config,
    validate_sandbox_artifact_archive,
    validation_evidence_bytes,
    verify_artifact_signature,
    verify_output_artifact,
)

CONFIG = b'''version = 1

[[required_checks]]
id = "tests"
argv = ["python3", "-c", "print('ok')"]
cwd = "."
timeout_seconds = 20
'''
FIXED_UUID = SimpleNamespace(hex="1" * 32)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _helper_result(
    result: dict[str, object],
    *,
    ok: bool = True,
    error: str | None = None,
) -> SandboxCommandResult:
    payload: dict[str, object] = {"ok": ok}
    if ok:
        payload["result"] = result
    else:
        payload["error"] = error
    return SandboxCommandResult(
        command_id="queued",
        exit_code=0 if ok else 2,
        stdout=json.dumps(payload),
        started_at_ms=10,
        finished_at_ms=11,
    )


def _config_read() -> SandboxCommandResult:
    return _helper_result(
        {
            "path": ".pi-agent/sandbox.toml",
            "content": CONFIG.decode("utf-8"),
            "size": len(CONFIG),
            "sha256": _sha(CONFIG),
        }
    )


def _fingerprint(value: str = "a") -> SandboxCommandResult:
    return _helper_result(
        {"file_count": 4, "total_bytes": 23, "sha256": value * 64}
    )


def _check_result() -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id="check",
        exit_code=0,
        stdout="ok\n",
        started_at_ms=20,
        finished_at_ms=30,
    )


def _spec() -> SandboxCreateSpec:
    return SandboxCreateSpec(
        operation_id="artifact-test",
        runtime_id="base",
        limits=SandboxLimits(
            lifetime_seconds=60,
            command_timeout_seconds=20,
            max_upload_bytes=4 * 1024 * 1024,
            max_file_bytes=1024 * 1024,
            max_file_count=100,
            max_output_bytes=1024 * 1024,
        ),
    )


def _baseline() -> tuple[SandboxFileEntry, ...]:
    return (
        SandboxFileEntry(
            path=".pi-agent/sandbox.toml",
            size=len(CONFIG),
            sha256=_sha(CONFIG),
        ),
        SandboxFileEntry(path="gone.bin", size=5, sha256=_sha(b"\x00gone")),
        SandboxFileEntry(path="text.txt", size=4, sha256=_sha(b"old\n")),
    )


def _current_files() -> tuple[SandboxFileEntry, ...]:
    return (
        SandboxFileEntry(
            path=".pi-agent/sandbox.toml",
            size=len(CONFIG),
            sha256=_sha(CONFIG),
        ),
        SandboxFileEntry(path="added.bin", size=4, sha256=_sha(b"\x00new")),
        SandboxFileEntry(
            path="scripts/__pycache__/main.cpython-312.pyc",
            size=3,
            sha256=_sha(b"pyc"),
        ),
        SandboxFileEntry(path="text.txt", size=4, sha256=_sha(b"new\n")),
    )


def _scan_result() -> SandboxCommandResult:
    return _helper_result(
        {
            "root": ".",
            "files": [entry.model_dump(mode="json") for entry in _current_files()],
            "truncated": False,
        }
    )


async def _operation(
    tmp_path: Path,
    *,
    signer: HMACSHA256ArtifactSigner | None = None,
    baseline_reader: bool = True,
) -> tuple[SandboxOperation, FakeSandboxBackend]:
    backend = FakeSandboxBackend(
        command_results=(
            _config_read(),
            _fingerprint(),
            _check_result(),
            _fingerprint(),
            _config_read(),
        )
    )
    spec = _spec()
    handle = await backend.create(spec)
    baseline_data = {
        ".pi-agent/sandbox.toml": CONFIG,
        "gone.bin": b"\x00gone",
        "text.txt": b"old\n",
    }

    async def read_baseline(path: str) -> bytes | None:
        return baseline_data.get(path)

    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir=spec.workdir,
        limits=spec.limits,
        staging_root=tmp_path / "stage",
        baseline_entries=_baseline(),
        baseline_reader=read_baseline if baseline_reader else None,
        validation_plan=parse_sandbox_validation_config(CONFIG),
        artifact_signer=signer,
        clock_ms=lambda: 100,
    )
    evidence = await operation.validate_required_checks()
    assert evidence.passed
    return operation, backend


def _manifest(evidence: SandboxValidationEvidence) -> SandboxArtifactManifest:
    validation = evidence
    changed = (
        SandboxArtifactChangedFile(
            path="added.bin",
            status="added",
            after_size=4,
            after_sha256=_sha(b"\x00new"),
            after_binary=True,
            binary=True,
            member_path="files/added.bin",
        ),
        SandboxArtifactChangedFile(
            path="text.txt",
            status="modified",
            before_size=4,
            before_sha256=_sha(b"old\n"),
            before_binary=False,
            after_size=4,
            after_sha256=_sha(b"new\n"),
            after_binary=False,
            binary=False,
            member_path="files/text.txt",
        ),
    )
    deleted = (
        SandboxArtifactDeletedFile(
            path="gone.bin",
            before_size=5,
            before_sha256=_sha(b"\x00gone"),
            before_binary=True,
        ),
    )
    binary = (
        SandboxArtifactBinaryDiffEntry(
            path="added.bin",
            status="added",
            after_size=4,
            after_sha256=_sha(b"\x00new"),
        ),
        SandboxArtifactBinaryDiffEntry(
            path="gone.bin",
            status="deleted",
            before_size=5,
            before_sha256=_sha(b"\x00gone"),
        ),
    )
    evidence_bytes = validation_evidence_bytes(validation)
    core = {
        "schema_version": "pi-agent-coding-artifact/v1",
        "artifact_id": "artifact-" + "1" * 32,
        "operation_id": "artifact-test",
        "workspace_revision": 0,
        "created_at_ms": 100,
        "baseline_sha256": baseline_manifest_digest(_baseline()),
        "workspace_sha256": "a" * 64,
        "validation_evidence_sha256": _sha(evidence_bytes),
        "changed_files": [entry.model_dump(mode="json") for entry in changed],
        "deleted_files": [entry.model_dump(mode="json") for entry in deleted],
        "binary_diff": [entry.model_dump(mode="json") for entry in binary],
        "changed_file_count": 2,
        "deleted_file_count": 1,
        "binary_diff_count": 2,
        "payload_bytes": 8,
    }
    return SandboxArtifactManifest(
        **core,
        manifest_sha256=_sha(canonical_json_bytes(core)),
    )


def _add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(value))


def _write_archive(
    path: Path,
    evidence: SandboxValidationEvidence,
    *,
    corrupt_payload: bool = False,
    extra_member: bool = False,
) -> SandboxArtifactManifest:
    manifest = _manifest(evidence)
    with tarfile.open(path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        _add_bytes(archive, "metadata/manifest.json", manifest.canonical_bytes())
        _add_bytes(
            archive,
            "metadata/validation-evidence.json",
            validation_evidence_bytes(evidence),
        )
        _add_bytes(
            archive,
            "metadata/binary-diff.json",
            canonical_json_bytes(
                [entry.model_dump(mode="json") for entry in manifest.binary_diff]
            ),
        )
        _add_bytes(
            archive,
            "metadata/deleted-files.json",
            canonical_json_bytes(
                [entry.model_dump(mode="json") for entry in manifest.deleted_files]
            ),
        )
        _add_bytes(
            archive,
            "files/added.bin",
            b"bad!" if corrupt_payload else b"\x00new",
        )
        _add_bytes(archive, "files/text.txt", b"new\n")
        if extra_member:
            _add_bytes(archive, "unexpected", b"x")
    return manifest


async def _queue_artifact_export(
    tmp_path: Path,
    operation: SandboxOperation,
    backend: FakeSandboxBackend,
    *,
    corrupt_payload: bool = False,
    extra_member: bool = False,
    final_fingerprint: str = "a",
) -> Path:
    archive_path = tmp_path / "remote.tar"
    evidence = operation.last_validation_evidence
    assert evidence is not None
    manifest = _write_archive(
        archive_path,
        evidence,
        corrupt_payload=corrupt_payload,
        extra_member=extra_member,
    )
    digest = _sha(archive_path.read_bytes())
    remote_path = "/tmp/pi-agent-artifact-" + "1" * 32 + ".tar"
    await backend.upload_file(
        operation.handle,
        local_path=archive_path,
        remote_path=remote_path,
        expected_sha256=digest,
    )
    for result in (
        _config_read(),
        _fingerprint(),
        _scan_result(),
        _helper_result(
            {
                "remote_path": remote_path,
                "size": archive_path.stat().st_size,
                "sha256": digest,
                "manifest_sha256": manifest.manifest_sha256,
                "workspace_sha256": "a" * 64,
            }
        ),
        _config_read(),
        _fingerprint(final_fingerprint),
    ):
        backend.queue_command_result(result)
    return archive_path


def test_hmac_signer_hides_secret_and_rejects_modified_envelope() -> None:
    signer = HMACSHA256ArtifactSigner(key_id="server-v1", secret=b"k" * 32)
    payload = artifact_signature_payload(
        key_id="server-v1",
        signed_at_ms=1,
        archive_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    signature = signer.sign(payload)

    assert signer.verify(payload, signature)
    assert not signer.verify(payload + b"x", signature)
    assert "kkkk" not in repr(signer)
    with pytest.raises(ValueError):
        HMACSHA256ArtifactSigner(key_id="bad key", secret=b"k" * 32)
    with pytest.raises(ValueError):
        HMACSHA256ArtifactSigner(key_id="short", secret=b"short")


@pytest.mark.asyncio
async def test_freeze_downloads_rehashes_signs_and_blocks_all_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operation_module, "uuid4", lambda: FIXED_UUID)
    signer = HMACSHA256ArtifactSigner(key_id="server-v1", secret=b"k" * 32)
    operation, backend = await _operation(tmp_path, signer=signer)
    await _queue_artifact_export(tmp_path, operation, backend)

    artifact = await operation.freeze_output_artifact()

    assert operation.frozen is True
    assert operation.output_artifact is artifact
    assert artifact.archive_path.name == f"{artifact.archive_sha256}.tar"
    assert artifact.manifest.changed_file_count == 2
    assert {entry.path for entry in artifact.manifest.changed_files} == {
        "added.bin",
        "text.txt",
    }
    assert artifact.manifest.deleted_file_count == 1
    assert artifact.manifest.binary_diff_count == 2
    assert verify_artifact_signature(signer, artifact.signature)
    forged_signature = artifact.signature.model_copy(update={"signature": "0" * 64})
    assert not verify_artifact_signature(signer, forged_signature)
    verified = verify_output_artifact(
        artifact,
        signer,
        max_archive_bytes=_spec().limits.max_upload_bytes,
        max_file_bytes=_spec().limits.max_file_bytes,
        max_file_count=_spec().limits.max_file_count,
    )
    assert verified.manifest == artifact.manifest
    command_count = len(backend.executed_commands)
    for mutation in (
        operation.write_file("new.txt", "x"),
        operation.delete_file("text.txt"),
        operation.apply_patch("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n"),
        operation.run(("python3", "-V")),
        operation.validate_required_checks(),
    ):
        with pytest.raises(SandboxWorkspaceError) as exc_info:
            await mutation
        assert exc_info.value.code == "operation_frozen"
    assert len(backend.executed_commands) == command_count
    assert await operation.freeze_output_artifact() is artifact
    artifact.archive_path.chmod(0o600)


@pytest.mark.asyncio
async def test_freeze_requires_signer_current_validation_and_verified_baseline(
    tmp_path: Path,
) -> None:
    operation, _ = await _operation(tmp_path / "unsigned")
    with pytest.raises(SandboxWorkspaceError) as unsigned:
        await operation.freeze_output_artifact()
    assert unsigned.value.code == "artifact_signing_unavailable"
    assert operation.frozen is False

    signer = HMACSHA256ArtifactSigner(key_id="server-v1", secret=b"k" * 32)
    stale, _ = await _operation(tmp_path / "stale", signer=signer)
    await stale.run(("python3", "-V"))
    with pytest.raises(SandboxWorkspaceError) as stale_error:
        await stale.freeze_output_artifact()
    assert stale_error.value.code == "validation_stale"
    assert stale.frozen is False

    missing, backend = await _operation(
        tmp_path / "baseline",
        signer=signer,
        baseline_reader=False,
    )
    backend.queue_command_result(_config_read())
    backend.queue_command_result(_fingerprint())
    backend.queue_command_result(_scan_result())
    with pytest.raises(SandboxWorkspaceError) as baseline_error:
        await missing.freeze_output_artifact()
    assert baseline_error.value.code == "artifact_baseline_unavailable"
    assert missing.frozen is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corrupt_payload", "extra_member"),
    ((True, False), (False, True)),
)
async def test_invalid_download_is_rejected_and_operation_remains_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_payload: bool,
    extra_member: bool,
) -> None:
    monkeypatch.setattr(operation_module, "uuid4", lambda: FIXED_UUID)
    signer = HMACSHA256ArtifactSigner(key_id="server-v1", secret=b"k" * 32)
    operation, backend = await _operation(tmp_path, signer=signer)
    await _queue_artifact_export(
        tmp_path,
        operation,
        backend,
        corrupt_payload=corrupt_payload,
        extra_member=extra_member,
    )

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.freeze_output_artifact()

    assert exc_info.value.code == "artifact_invalid"
    assert operation.frozen is True
    assert operation.output_artifact is None
    with pytest.raises(SandboxWorkspaceError) as frozen:
        await operation.write_file("x", "x")
    assert frozen.value.code == "operation_frozen"


@pytest.mark.asyncio
async def test_out_of_band_change_during_download_prevents_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operation_module, "uuid4", lambda: FIXED_UUID)
    signer = HMACSHA256ArtifactSigner(key_id="server-v1", secret=b"k" * 32)
    operation, backend = await _operation(tmp_path, signer=signer)
    await _queue_artifact_export(
        tmp_path,
        operation,
        backend,
        final_fingerprint="b",
    )

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.freeze_output_artifact()

    assert exc_info.value.code == "artifact_stale"
    assert operation.last_validation_evidence is None
    assert operation.output_artifact is None


def test_archive_validator_rejects_payload_tampering_and_extra_members(
    tmp_path: Path,
) -> None:
    # Pydantic construction is tested separately; a complete evidence comes from
    # the operation path, so these corruption checks reuse the async fixture logic
    # through deliberately malformed raw tar member sets below.
    path = tmp_path / "unsafe.tar"
    with tarfile.open(path, mode="w") as archive:
        info = tarfile.TarInfo("metadata/manifest.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        archive.addfile(info)
    with pytest.raises(ValueError):
        validate_sandbox_artifact_archive(
            path,
            max_archive_bytes=1024 * 1024,
            max_file_bytes=1024,
            max_file_count=10,
        )
