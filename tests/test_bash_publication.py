"""Stage 3: purpose-bound artifacts, exact approval, recovery and zero-write rejection."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_workspace.store import WorkspaceStore
from coding_agent_app.execution.automation import ApprovedCodingAutomation
from coding_agent_app.execution.models import ExecutionDenied
from coding_agent_app.sandbox.workspace import (
    WorkspaceSandboxArtifactPublisher,
    WorkspaceSandboxBaselineProvider,
)
from coding_sandbox.artifact import (
    HMACSHA256ArtifactSigner,
    SandboxArtifactManifest,
    SandboxOutputArtifact,
    baseline_manifest_digest,
    canonical_json_bytes,
    create_artifact_signature,
    validate_sandbox_artifact_archive,
)
from coding_sandbox.lifecycle import (
    DEFAULT_VALIDATION_CONFIG,
    ManagedSandboxLifecycle,
    ManagedSandboxOperationRecord,
    SandboxLifecycleError,
    SQLiteSandboxOperationStore,
)
from coding_sandbox.output_evidence import BashOutputIntegrityEvidence
from coding_sandbox.publication import (
    WORKSPACE_PUBLISH_POLICY_SHA256,
    PublicationApproval,
    PublicationBinding,
)
from coding_sandbox.publisher import PublisherError
from coding_sandbox.snapshot import SnapshotPolicy
from coding_sandbox.workspace_models import SandboxFileEntry


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(path):
    return validate_sandbox_artifact_archive(
        path,
        max_archive_bytes=4 * 1024 * 1024,
        max_file_bytes=1024 * 1024,
        max_file_count=100,
    )


def write_tar(path, members):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


async def fixture(tmp_path, changed_path="artifacts/result.txt"):
    store = WorkspaceStore(tmp_path / "files")
    await store.init()
    await store.ensure_session_workspace("session-one")
    provider = WorkspaceSandboxBaselineProvider(
        store, materialization_root=tmp_path / "materialized"
    )
    baseline = await provider(
        "session-one",
        tmp_path / "baseline.tar.gz",
        SnapshotPolicy(),
        DEFAULT_VALIDATION_CONFIG,
    )
    entries = [
        SandboxFileEntry(path=e.path, size=e.size, sha256=e.sha256)
        for e in baseline.snapshot.manifest.entries
    ]
    before = next((entry for entry in entries if entry.path == changed_path), None)
    data = b"reviewed output\n"
    final = {entry.path: (entry.size, entry.sha256) for entry in entries}
    final[changed_path] = (len(data), sha(data))
    fingerprint = sha(
        b"".join(
            f"{path}\0{size}\0{digest}\n".encode() for path, (size, digest) in sorted(final.items())
        )
    )
    evidence = BashOutputIntegrityEvidence(
        account_id="owner",
        session_id="session-one",
        request_id="request-one",
        task_id="task-one",
        operation_id="sandbox-" + "1" * 32,
        command_id="bash-" + "2" * 32,
        scope_sha256="b" * 64,
        script_sha256="c" * 64,
        cwd=".",
        baseline_revision=baseline.source_workspace_revision,
        baseline_sha256=baseline.source_workspace_sha256,
        publish_policy_sha256=WORKSPACE_PUBLISH_POLICY_SHA256,
        workspace_revision=1,
        workspace_sha256_after=fingerprint,
        result_sha256="d" * 64,
    )
    changed = dict(
        path=changed_path,
        status="added" if before is None else "modified",
        before_size=None if before is None else before.size,
        before_sha256=None if before is None else before.sha256,
        before_binary=None if before is None else False,
        after_size=len(data),
        after_sha256=sha(data),
        after_binary=False,
        binary=False,
        member_path="files/" + changed_path,
    )
    core = dict(
        schema_version="pi-agent-bash-artifact/v1",
        artifact_id="artifact-" + "3" * 32,
        operation_id=evidence.operation_id,
        workspace_revision=1,
        created_at_ms=1,
        baseline_sha256=baseline_manifest_digest(tuple(entries)),
        workspace_sha256=fingerprint,
        validation_evidence_sha256=sha(canonical_json_bytes(evidence.model_dump(mode="json"))),
        changed_files=[changed],
        deleted_files=[],
        binary_diff=[],
        changed_file_count=1,
        deleted_file_count=0,
        binary_diff_count=0,
        payload_bytes=len(data),
    )
    manifest = SandboxArtifactManifest(**core, manifest_sha256=sha(canonical_json_bytes(core)))
    members = [
        ("metadata/manifest.json", manifest.canonical_bytes()),
        (
            "metadata/validation-evidence.json",
            canonical_json_bytes(evidence.model_dump(mode="json")),
        ),
        ("metadata/binary-diff.json", b"[]"),
        ("metadata/deleted-files.json", b"[]"),
        ("files/" + changed_path, data),
    ]
    archive = tmp_path / "output.tar"
    write_tar(archive, members)
    signer = HMACSHA256ArtifactSigner(key_id="stage3", secret=b"s" * 32)
    artifact = SandboxOutputArtifact(
        archive_path=archive,
        archive_size=archive.stat().st_size,
        archive_sha256=sha(archive.read_bytes()),
        manifest=manifest,
        validation_evidence=evidence,
        signature=create_artifact_signature(
            signer,
            archive_sha256=sha(archive.read_bytes()),
            manifest_sha256=manifest.manifest_sha256,
            signed_at_ms=1,
        ),
    )
    binding = PublicationBinding(
        session_id="session-one",
        purpose="bash",
        backend="local_docker",
        scope_sha256=evidence.scope_sha256,
        policy_sha256=WORKSPACE_PUBLISH_POLICY_SHA256,
    )
    return SimpleNamespace(
        store=store,
        provider=provider,
        baseline=baseline,
        artifact=artifact,
        signer=signer,
        binding=binding,
        members=members,
        publisher=WorkspaceSandboxArtifactPublisher(store, staging_root=tmp_path / "pub"),
    )


async def publish(f, binding=None):
    return await f.publisher.publish(
        f.artifact,
        baseline=f.baseline.snapshot,
        signer=f.signer,
        session_id="session-one",
        expected_workspace_revision=f.baseline.source_workspace_revision,
        expected_workspace_sha256=f.baseline.source_workspace_sha256,
        publication=f.binding if binding is None else binding,
    )


@pytest.mark.asyncio
async def test_bash_artifact_success_and_explicit_evidence_discriminator(tmp_path):
    f = await fixture(tmp_path)
    content = validate(f.artifact.archive_path)
    assert isinstance(content.validation_evidence, BashOutputIntegrityEvidence)
    assert "passed" not in content.validation_evidence.model_dump()
    core = content.manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    core["schema_version"] = "pi-agent-coding-artifact/v1"
    manifest = {**core, "manifest_sha256": sha(canonical_json_bytes(core))}
    write_tar(
        f.artifact.archive_path,
        [
            (name, canonical_json_bytes(manifest) if name.endswith("manifest.json") else data)
            for name, data in f.members
        ],
    )
    with pytest.raises(ValueError):
        validate(f.artifact.archive_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "AGENT.md",
        "Memory.md",
        "HANDOFF.md",
        "upload/input.txt",
        "inputs/input.txt",
        "documents/input.txt",
        "tasks/task.md",
        ".pi-agent/untrusted",
    ],
)
async def test_protected_output_rejects_whole_batch_without_workspace_writes(tmp_path, path):
    f = await fixture(tmp_path, path)
    before = await f.store.inspect_workspace_revision("session-one")
    with pytest.raises(PublisherError):
        await publish(f)
    assert await f.store.inspect_workspace_revision("session-one") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        {"purpose": "coding"},
        {"session_id": "foreign"},
        {"scope_sha256": "e" * 64},
        {"policy_sha256": "e" * 64},
        {"backend": "e2b"},
    ],
)
async def test_wrong_purpose_owner_scope_or_policy_cannot_publish(tmp_path, update):
    f = await fixture(tmp_path)
    before = await f.store.inspect_workspace_revision("session-one")
    with pytest.raises(PublisherError):
        await publish(f, f.binding.model_copy(update=update))
    assert await f.store.inspect_workspace_revision("session-one") == before


@pytest.mark.asyncio
async def test_even_unrelated_workspace_revision_change_conflicts(tmp_path):
    f = await fixture(tmp_path)
    await f.store.write_text("session-one", "unrelated.txt", "concurrent")
    before = await f.store.inspect_workspace_revision("session-one")
    with pytest.raises(PublisherError) as exc:
        await publish(f)
    assert exc.value.code == "publish_conflict"
    assert await f.store.inspect_workspace_revision("session-one") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "/absolute",
        "../escape",
        "files/x:stream",
        "files/CON.txt",
        "files/x.",
        "files/a\\b",
        "FILES/artifacts/result.txt",
    ],
)
async def test_unsafe_tar_members_are_never_extracted(tmp_path, name):
    f = await fixture(tmp_path)
    write_tar(f.artifact.archive_path, [*f.members, (name, b"invalid")])
    with pytest.raises(ValueError):
        validate(f.artifact.archive_path)


async def lifecycle_for(tmp_path, f):
    async def forbidden(*args):
        raise AssertionError("artifact recovery must not resolve/create compute")

    async def exists(sid):
        return sid == "session-one"

    store = await SQLiteSandboxOperationStore.open(tmp_path / "operations.db")
    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=forbidden,
        backend_resolver=forbidden,
        artifact_signer=f.signer,
        session_exists=exists,
        projects_root=None,
        state_root=tmp_path / "state",
        staging_root=tmp_path / "stage",
        baseline_provider=f.provider,
        artifact_publisher=f.publisher,
        require_execution_approval=True,
    )
    return lifecycle, store


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_before_crash", [False, True])
async def test_restart_review_exact_publication_and_idempotence_without_compute(
    tmp_path, commit_before_crash
):
    f = await fixture(tmp_path)
    lifecycle, store = await lifecycle_for(tmp_path, f)
    record = ManagedSandboxOperationRecord(
        operation_id=f.artifact.manifest.operation_id,
        session_id="session-one",
        status="freezing",
        config_revision=1,
        created_at_ms=1,
        updated_at_ms=1,
        workspace_revision=1,
        baseline_workspace_revision=f.baseline.source_workspace_revision,
        baseline_workspace_sha256=f.baseline.source_workspace_sha256,
        publication=f.binding,
    )
    review = await lifecycle._save_retained(
        record,
        SimpleNamespace(
            snapshot=f.baseline.snapshot,
            publish_root=None,
        ),
        f.artifact,
    )
    record = record.model_copy(
        update=dict(
            status="awaiting_approval",
            artifact_id=f.artifact.manifest.artifact_id,
            artifact_sha256=f.artifact.archive_sha256,
            review_sha256=review,
        )
    )
    await store.create(record)
    if commit_before_crash:
        # Workspace commit succeeds but lifecycle response/state update is lost.
        await publish(f)
        await store.compare_and_swap(record, record.model_copy(update={"status": "publishing"}))
    await lifecycle.shutdown()
    await store.close()
    lifecycle, store = await lifecycle_for(tmp_path, f)
    try:
        assert await lifecycle.recover_startup() == ()
        assert lifecycle._live == {}
        assert (
            await lifecycle.read_frozen_file(record.operation_id, "artifacts/result.txt")
        ).content == b"reviewed output\n"
        approval = PublicationApproval(
            artifact_id=record.artifact_id,
            artifact_sha256=record.artifact_sha256,
            review_sha256=review,
        )
        for bad in (
            None,
            approval.model_copy(update={"review_sha256": "e" * 64}),
            approval.model_copy(update={"artifact_sha256": "e" * 64}),
        ):
            with pytest.raises(SandboxLifecycleError, match="approval"):
                await lifecycle.publish(record.operation_id, approval=bad)
        action = lifecycle.retry_publish if commit_before_crash else lifecycle.publish
        await action(record.operation_id, approval=approval)
        done = await lifecycle.join_action(record.operation_id)
        assert done.status == "published", done
        revision = await f.store.inspect_workspace_revision("session-one")
        assert revision[0] == f.baseline.source_workspace_revision + 1
        assert await lifecycle.publish(record.operation_id, approval=approval) == done
        assert await f.store.inspect_workspace_revision("session-one") == revision
    finally:
        await lifecycle.shutdown()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["receipt", "archive", "unreleased"])
async def test_recovery_rejects_tampered_private_receipt_or_payload(tmp_path, tamper):
    f = await fixture(tmp_path)
    lifecycle, store = await lifecycle_for(tmp_path, f)
    record = ManagedSandboxOperationRecord(
        operation_id=f.artifact.manifest.operation_id,
        session_id="session-one",
        status="freezing",
        config_revision=1,
        created_at_ms=1,
        updated_at_ms=1,
    )
    review = await lifecycle._save_retained(
        record,
        SimpleNamespace(
            snapshot=f.baseline.snapshot,
            publish_root=None,
        ),
        f.artifact,
    )
    saved = record.model_copy(
        update=dict(
            status="awaiting_approval",
            artifact_id=f.artifact.manifest.artifact_id,
            artifact_sha256=f.artifact.archive_sha256,
            review_sha256=review,
        )
    )
    if tamper == "unreleased":
        saved = saved.model_copy(update={"publication": f.binding, "execution_released": False})
    await store.create(saved)
    if tamper == "archive":
        f.artifact.archive_path.write_bytes(b"tampered")
    elif tamper == "receipt":
        payload, signature = await store.load_retained(record.operation_id)
        data = json.loads(payload)
        data["session_id"] = "foreign"
        await store.save_retained(record.operation_id, json.dumps(data), signature)
    try:
        assert await lifecycle.recover_startup() == (record.operation_id,)
        assert (await lifecycle.get(record.operation_id)).status == "interrupted"
        assert not lifecycle._live
    finally:
        await lifecycle.shutdown()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,cleanup_pending", [("closed", False), ("revoked", False), ("closed", True)]
)
async def test_only_normally_closed_and_clean_execution_enables_publication(state, cleanup_pending):
    identity = SimpleNamespace(operation_id="sandbox-" + "1" * 32)
    lifecycle = SimpleNamespace(
        release_execution=AsyncMock(),
        confirm_execution_released=AsyncMock(),
    )
    automation = SimpleNamespace(
        prepared=SimpleNamespace(scope=SimpleNamespace(identity=identity)),
        _adopted=True,
        _lifecycle=lifecycle,
        cancel_if_possible=AsyncMock(),
        runtime=SimpleNamespace(
            finish=AsyncMock(),
            store=SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(state=state, cleanup_pending=cleanup_pending),
                )
            ),
        ),
    )
    if cleanup_pending:
        with pytest.raises(ExecutionDenied):
            await ApprovedCodingAutomation.close(automation)
    else:
        await ApprovedCodingAutomation.close(automation)
    if state == "closed" and not cleanup_pending:
        lifecycle.confirm_execution_released.assert_awaited_once_with(identity.operation_id)
        automation.cancel_if_possible.assert_not_awaited()
    else:
        lifecycle.confirm_execution_released.assert_not_awaited()
        automation.cancel_if_possible.assert_awaited_once_with(identity.operation_id)
