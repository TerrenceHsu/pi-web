"""Local Publisher commit, rollback and crash-recovery contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import threading
from pathlib import Path

import pytest

from coding_sandbox import (
    HMACSHA256ArtifactSigner,
    LocalTransactionalPublisher,
    ProjectSnapshot,
    PublisherError,
    SandboxArtifactChangedFile,
    SandboxArtifactDeletedFile,
    SandboxArtifactManifest,
    SandboxFileEntry,
    SandboxOutputArtifact,
    SandboxValidationCheckEvidence,
    SandboxValidationEvidence,
    baseline_manifest_digest,
    build_project_snapshot,
    canonical_json_bytes,
    create_artifact_signature,
    validation_evidence_bytes,
)

CONFIG = b"""version = 1

[[required_checks]]
id = "tests"
argv = ["python3", "-c", "print('ok')"]
cwd = "."
timeout_seconds = 20
"""
OLD_TEXT = b"old\n"
NEW_TEXT = b"new\n"
GONE_TEXT = b"remove me\n"
ADDED_TEXT = b"added\n"


class SimulatedProcessCrash(BaseException):
    """Bypass Publisher's in-process Exception rollback path."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_project(root: Path) -> dict[str, bytes]:
    root.mkdir()
    files = {
        ".pi-agent/sandbox.toml": CONFIG,
        "gone.txt": GONE_TEXT,
        "text.txt": OLD_TEXT,
        "untouched.txt": b"stable\n",
    }
    for relative_path, value in files.items():
        target = root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    return files


def _workspace_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _evidence() -> SandboxValidationEvidence:
    empty_digest = _sha(b"")
    workspace_digest = "b" * 64
    check = SandboxValidationCheckEvidence(
        check_id="tests",
        argv=("python3", "-c", "print('ok')"),
        cwd=".",
        timeout_seconds=20,
        status="passed",
        exit_code=0,
        termination_reason="exited",
        started_at_ms=10,
        finished_at_ms=11,
        duration_ms=1,
        captured_stdout_sha256=empty_digest,
        captured_stderr_sha256=empty_digest,
    )
    return SandboxValidationEvidence(
        evidence_id="validation-" + "2" * 32,
        operation_id="publisher-test",
        source_path=".pi-agent/sandbox.toml",
        source_sha256=_sha(CONFIG),
        plan_sha256="a" * 64,
        workspace_revision=3,
        workspace_sha256_before=workspace_digest,
        workspace_sha256_after=workspace_digest,
        checks=(check,),
        passed=True,
        started_at_ms=10,
        finished_at_ms=11,
        duration_ms=1,
    )


def _baseline_entries(snapshot: ProjectSnapshot) -> tuple[SandboxFileEntry, ...]:
    return tuple(
        SandboxFileEntry(path=entry.path, size=entry.size, sha256=entry.sha256)
        for entry in snapshot.manifest.entries
    )


def _artifact_manifest(
    snapshot: ProjectSnapshot,
    evidence: SandboxValidationEvidence,
) -> SandboxArtifactManifest:
    changed = (
        SandboxArtifactChangedFile(
            path="nested/added.txt",
            status="added",
            after_size=len(ADDED_TEXT),
            after_sha256=_sha(ADDED_TEXT),
            after_binary=False,
            binary=False,
            member_path="files/nested/added.txt",
        ),
        SandboxArtifactChangedFile(
            path="text.txt",
            status="modified",
            before_size=len(OLD_TEXT),
            before_sha256=_sha(OLD_TEXT),
            before_binary=False,
            after_size=len(NEW_TEXT),
            after_sha256=_sha(NEW_TEXT),
            after_binary=False,
            binary=False,
            member_path="files/text.txt",
        ),
    )
    deleted = (
        SandboxArtifactDeletedFile(
            path="gone.txt",
            before_size=len(GONE_TEXT),
            before_sha256=_sha(GONE_TEXT),
            before_binary=False,
        ),
    )
    evidence_bytes = validation_evidence_bytes(evidence)
    core = {
        "schema_version": "pi-agent-coding-artifact/v1",
        "artifact_id": "artifact-" + "3" * 32,
        "operation_id": evidence.operation_id,
        "workspace_revision": evidence.workspace_revision,
        "created_at_ms": 12,
        "baseline_sha256": baseline_manifest_digest(_baseline_entries(snapshot)),
        "workspace_sha256": evidence.workspace_sha256_after,
        "validation_evidence_sha256": _sha(evidence_bytes),
        "changed_files": [entry.model_dump(mode="json") for entry in changed],
        "deleted_files": [entry.model_dump(mode="json") for entry in deleted],
        "binary_diff": [],
        "changed_file_count": len(changed),
        "deleted_file_count": len(deleted),
        "binary_diff_count": 0,
        "payload_bytes": len(ADDED_TEXT) + len(NEW_TEXT),
    }
    return SandboxArtifactManifest(
        **core,
        manifest_sha256=_sha(canonical_json_bytes(core)),
    )


def _add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(value)
    member.mode = 0o600
    member.mtime = 0
    archive.addfile(member, io.BytesIO(value))


def _build_artifact(
    tmp_path: Path,
    snapshot: ProjectSnapshot,
) -> tuple[SandboxOutputArtifact, HMACSHA256ArtifactSigner]:
    evidence = _evidence()
    manifest = _artifact_manifest(snapshot, evidence)
    archive_path = tmp_path / "artifact.tar"
    with tarfile.open(archive_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        _add_bytes(archive, "metadata/manifest.json", manifest.canonical_bytes())
        _add_bytes(
            archive,
            "metadata/validation-evidence.json",
            validation_evidence_bytes(evidence),
        )
        _add_bytes(archive, "metadata/binary-diff.json", b"[]")
        _add_bytes(
            archive,
            "metadata/deleted-files.json",
            canonical_json_bytes(
                [entry.model_dump(mode="json") for entry in manifest.deleted_files]
            ),
        )
        _add_bytes(archive, "files/nested/added.txt", ADDED_TEXT)
        _add_bytes(archive, "files/text.txt", NEW_TEXT)
    archive_bytes = archive_path.read_bytes()
    archive_digest = _sha(archive_bytes)
    signer = HMACSHA256ArtifactSigner(key_id="publisher-v1", secret=b"k" * 32)
    artifact = SandboxOutputArtifact(
        archive_path=archive_path,
        archive_size=len(archive_bytes),
        archive_sha256=archive_digest,
        manifest=manifest,
        validation_evidence=evidence,
        signature=create_artifact_signature(
            signer,
            archive_sha256=archive_digest,
            manifest_sha256=manifest.manifest_sha256,
            signed_at_ms=13,
        ),
    )
    return artifact, signer


async def _setup(
    tmp_path: Path,
    *,
    fault_hook: object | None = None,
    lock_timeout_seconds: float = 1.0,
) -> tuple[
    Path,
    Path,
    dict[str, bytes],
    ProjectSnapshot,
    SandboxOutputArtifact,
    HMACSHA256ArtifactSigner,
    LocalTransactionalPublisher,
]:
    project = tmp_path / "project"
    state = tmp_path / "state"
    original = _write_project(project)
    snapshot = await build_project_snapshot(project, tmp_path / "snapshot.tar.gz")
    artifact, signer = _build_artifact(tmp_path, snapshot)
    publisher = LocalTransactionalPublisher(
        project_root=project,
        state_root=state,
        lock_timeout_seconds=lock_timeout_seconds,
        fault_hook=fault_hook,  # type: ignore[arg-type]
    )
    return project, state, original, snapshot, artifact, signer, publisher


def _journal_paths(state: Path) -> list[Path]:
    return sorted(state.glob("projects/*/transactions/publish-*/journal.jsonl"))


@pytest.mark.asyncio
async def test_publish_commits_exact_bytes_and_retry_is_idempotent(tmp_path: Path) -> None:
    project, state, _, snapshot, artifact, signer, publisher = await _setup(tmp_path)

    result = await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert result.status == "published"
    assert _workspace_bytes(project) == {
        ".pi-agent/sandbox.toml": CONFIG,
        "nested/added.txt": ADDED_TEXT,
        "text.txt": NEW_TEXT,
        "untouched.txt": b"stable\n",
    }
    journal_path = _journal_paths(state)[0]
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    assert [record["sequence"] for record in records] == list(range(len(records)))
    assert "commit" in {record["event"] for record in records}
    assert "cleanup_complete" in {record["event"] for record in records}
    transaction_root = journal_path.parent
    assert not (transaction_root / "backups").exists()
    assert not list(project.rglob(".pi-agent-publish-*"))

    retry = await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert retry.status == "already_published"
    assert retry.transaction_id == result.transaction_id
    assert len(_journal_paths(state)) == 1


@pytest.mark.asyncio
async def test_publish_rejects_baseline_conflict_before_project_mutation(
    tmp_path: Path,
) -> None:
    project, state, _, snapshot, artifact, signer, publisher = await _setup(tmp_path)
    (project / "untouched.txt").write_bytes(b"user edit\n")
    before = _workspace_bytes(project)

    with pytest.raises(PublisherError) as raised:
        await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert raised.value.code == "publish_conflict"
    assert _workspace_bytes(project) == before
    assert not _journal_paths(state)


@pytest.mark.asyncio
async def test_publish_rejects_artifact_tampering_before_project_mutation(
    tmp_path: Path,
) -> None:
    project, state, original, snapshot, artifact, signer, publisher = await _setup(tmp_path)
    with artifact.archive_path.open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(PublisherError) as raised:
        await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert raised.value.code == "artifact_invalid"
    assert _workspace_bytes(project) == original
    assert not _journal_paths(state)


@pytest.mark.asyncio
async def test_publish_rejects_baseline_archive_tampering_before_mutation(
    tmp_path: Path,
) -> None:
    project, state, original, snapshot, artifact, signer, publisher = await _setup(tmp_path)
    with snapshot.archive_path.open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(PublisherError) as raised:
        await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert raised.value.code == "baseline_invalid"
    assert _workspace_bytes(project) == original
    assert not _journal_paths(state)


def test_publisher_state_must_remain_outside_the_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_project(project)

    with pytest.raises(PublisherError) as raised:
        LocalTransactionalPublisher(
            project_root=project,
            state_root=project / ".pi-agent-data",
        )

    assert raised.value.code == "invalid_state_root"


@pytest.mark.asyncio
async def test_publish_rejects_reparse_escape_without_touching_outside(
    tmp_path: Path,
) -> None:
    project, _, original, snapshot, artifact, signer, publisher = await _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / "nested").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this Windows account cannot create symlinks")

    with pytest.raises(PublisherError) as raised:
        await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert raised.value.code == "unsafe_path"
    assert not list(outside.iterdir())
    observed = _workspace_bytes(project)
    assert all(observed[path] == value for path, value in original.items())


@pytest.mark.asyncio
async def test_publish_exception_rolls_back_every_applied_entry(tmp_path: Path) -> None:
    def fail_after_add(point: str, index: int | None) -> None:
        if point == "after_apply" and index == 1:
            raise RuntimeError("injected publisher failure")

    project, _, original, snapshot, artifact, signer, publisher = await _setup(
        tmp_path,
        fault_hook=fail_after_add,
    )

    with pytest.raises(PublisherError) as raised:
        await publisher.publish(artifact, baseline=snapshot, signer=signer)

    assert raised.value.code == "io_failure"
    assert _workspace_bytes(project) == original
    assert not (project / "nested").exists()
    assert not list(project.rglob(".pi-agent-publish-*"))


@pytest.mark.asyncio
async def test_startup_recovery_repairs_crash_and_torn_journal_tail(
    tmp_path: Path,
) -> None:
    def crash_after_add(point: str, index: int | None) -> None:
        if point == "after_apply" and index == 1:
            raise SimulatedProcessCrash

    project, state, original, snapshot, artifact, signer, publisher = await _setup(
        tmp_path,
        fault_hook=crash_after_add,
    )
    with pytest.raises(SimulatedProcessCrash):
        await publisher.publish(artifact, baseline=snapshot, signer=signer)
    journal_path = _journal_paths(state)[0]
    with journal_path.open("ab") as stream:
        stream.write(b'{"torn":')

    recovery = LocalTransactionalPublisher(project_root=project, state_root=state)
    report = await recovery.recover_pending()

    assert len(report.recovered_rollbacks) == 1
    assert _workspace_bytes(project) == original
    assert not (project / "nested").exists()
    assert list(journal_path.parent.glob("journal.torn-*.bin"))
    events = {json.loads(line)["event"] for line in journal_path.read_text().splitlines()}
    assert {"recovery_started", "rollback_complete", "cleanup_complete"} <= events


@pytest.mark.asyncio
async def test_recovery_never_overwrites_unrelated_post_crash_edit(tmp_path: Path) -> None:
    def crash_after_add(point: str, index: int | None) -> None:
        if point == "after_apply" and index == 1:
            raise SimulatedProcessCrash

    project, state, _, snapshot, artifact, signer, publisher = await _setup(
        tmp_path,
        fault_hook=crash_after_add,
    )
    with pytest.raises(SimulatedProcessCrash):
        await publisher.publish(artifact, baseline=snapshot, signer=signer)
    user_bytes = b"post-crash user edit\n"
    (project / "nested" / "added.txt").write_bytes(user_bytes)

    recovery = LocalTransactionalPublisher(project_root=project, state_root=state)
    with pytest.raises(PublisherError) as raised:
        await recovery.recover_pending()

    assert raised.value.code == "recovery_conflict"
    assert (project / "nested" / "added.txt").read_bytes() == user_bytes


@pytest.mark.asyncio
async def test_recovery_preserves_commit_after_crash_before_cleanup(tmp_path: Path) -> None:
    def crash_after_commit(point: str, index: int | None) -> None:
        del index
        if point == "after_commit":
            raise SimulatedProcessCrash

    project, state, _, snapshot, artifact, signer, publisher = await _setup(
        tmp_path,
        fault_hook=crash_after_commit,
    )
    with pytest.raises(SimulatedProcessCrash):
        await publisher.publish(artifact, baseline=snapshot, signer=signer)

    recovery = LocalTransactionalPublisher(project_root=project, state_root=state)
    report = await recovery.recover_pending()

    assert len(report.committed_transactions) == 1
    assert (project / "nested" / "added.txt").read_bytes() == ADDED_TEXT
    assert (project / "text.txt").read_bytes() == NEW_TEXT
    assert not (project / "gone.txt").exists()
    assert not (_journal_paths(state)[0].parent / "backups").exists()


@pytest.mark.asyncio
async def test_project_lock_rejects_a_concurrent_publisher(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def pause_after_prepare(point: str, index: int | None) -> None:
        del index
        if point == "after_prepare":
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("lock test timed out")

    project, state, _, snapshot, artifact, signer, publisher = await _setup(
        tmp_path,
        fault_hook=pause_after_prepare,
    )
    first = asyncio.create_task(publisher.publish(artifact, baseline=snapshot, signer=signer))
    assert await asyncio.to_thread(entered.wait, 5)
    contender = LocalTransactionalPublisher(
        project_root=project,
        state_root=state,
        lock_timeout_seconds=0.1,
    )
    try:
        with pytest.raises(PublisherError) as raised:
            await contender.publish(artifact, baseline=snapshot, signer=signer)
        assert raised.value.code == "lock_timeout"
    finally:
        release.set()
    assert (await first).status == "published"


@pytest.mark.asyncio
async def test_recovery_rejects_hash_chain_tampering_without_further_mutation(
    tmp_path: Path,
) -> None:
    def crash_after_delete(point: str, index: int | None) -> None:
        if point == "after_apply" and index == 0:
            raise SimulatedProcessCrash

    project, state, _, snapshot, artifact, signer, publisher = await _setup(
        tmp_path,
        fault_hook=crash_after_delete,
    )
    with pytest.raises(SimulatedProcessCrash):
        await publisher.publish(artifact, baseline=snapshot, signer=signer)
    before = _workspace_bytes(project)
    journal_path = _journal_paths(state)[0]
    records = journal_path.read_text().splitlines()
    payload = json.loads(records[1])
    payload["record_sha256"] = "0" * 64
    records[1] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    journal_path.write_text("\n".join(records) + "\n")

    recovery = LocalTransactionalPublisher(project_root=project, state_root=state)
    with pytest.raises(PublisherError) as raised:
        await recovery.recover_pending()

    assert raised.value.code == "journal_invalid"
    assert _workspace_bytes(project) == before
