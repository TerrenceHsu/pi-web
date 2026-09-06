"""Product adapter between ``agent_workspace`` and ``coding_sandbox``."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath
from uuid import uuid4

from agent_workspace.store import (
    FileStoreError,
    WorkspaceMaterializationEntry,
    WorkspacePublishChange,
    WorkspacePublishPolicyError,
    WorkspaceStore,
    WorkspaceTreeConflictError,
    WorkspaceVersionConflictError,
    is_sandbox_publishable_workspace_path,
)
from coding_sandbox.artifact import ArtifactSigner, SandboxOutputArtifact
from coding_sandbox.lifecycle import SandboxBaseline
from coding_sandbox.publisher import (
    LocalTransactionalPublisher,
    PublisherError,
    PublisherResult,
)
from coding_sandbox.snapshot import (
    ProjectSnapshot,
    SnapshotError,
    SnapshotPolicy,
    build_project_snapshot,
    read_snapshot_file,
)

_VALIDATION_CONFIG_PATH = ".pi-agent/sandbox.toml"


class WorkspaceSandboxBaselineProvider:
    """Materialize one Session revision and build its immutable Sandbox archive."""

    def __init__(self, store: WorkspaceStore, *, materialization_root: Path) -> None:
        if not materialization_root.is_absolute():
            raise ValueError("Sandbox materialization root must be absolute")
        self._store = store
        self._materialization_root = materialization_root.resolve(strict=False)

    async def current_version(self, session_id: str) -> tuple[int, str]:
        """Activation-time revision/hash check; does not create a fresh input copy."""
        return await self._store.inspect_workspace_revision(session_id)

    async def __call__(
        self,
        session_id: str,
        archive_path: Path,
        policy: SnapshotPolicy,
        validation_config: bytes,
    ) -> SandboxBaseline:
        key = hashlib.sha256(str(archive_path).encode("utf-8")).hexdigest()[:32]
        materialized_root = self._materialization_root / f"workspace-{key}"
        try:
            materialization = await self._store.materialize_workspace_revision(
                session_id,
                materialized_root,
            )
        except (FileStoreError, OSError) as exc:
            raise SnapshotError("file_changed") from exc
        try:
            config_path = materialization.root_path / _VALIDATION_CONFIG_PATH
            config_path.parent.mkdir(parents=True, exist_ok=False)
            _write_new_file(config_path, validation_config)
            snapshot = await build_project_snapshot(
                materialization.root_path,
                archive_path,
                policy=policy,
            )
            expected_paths = {
                *(entry.logical_path for entry in materialization.entries),
                _VALIDATION_CONFIG_PATH,
            }
            actual_paths = {entry.path for entry in snapshot.manifest.entries}
            if actual_paths != expected_paths:
                missing = sorted(expected_paths - actual_paths)
                await asyncio.to_thread(archive_path.unlink, missing_ok=True)
                raise SnapshotError(
                    "unsupported_file",
                    relative_path=missing[0] if missing else None,
                )
            return SandboxBaseline(
                snapshot=snapshot,
                source_workspace_revision=materialization.revision,
                source_workspace_sha256=materialization.tree_sha256,
                publish_root=None,
            )
        finally:
            await asyncio.to_thread(
                shutil.rmtree,
                materialized_root,
                ignore_errors=True,
            )


class WorkspaceSandboxArtifactPublisher:
    """Verify a frozen artifact in isolation, then commit it to WorkspaceStore."""

    def __init__(self, store: WorkspaceStore, *, staging_root: Path) -> None:
        if not staging_root.is_absolute():
            raise ValueError("Workspace Sandbox publisher staging root must be absolute")
        self._store = store
        self._staging_root = staging_root.resolve(strict=False)

    async def publish(
        self,
        artifact: SandboxOutputArtifact,
        *,
        baseline: ProjectSnapshot,
        signer: ArtifactSigner,
        session_id: str,
        expected_workspace_revision: int,
        expected_workspace_sha256: str,
    ) -> PublisherResult:
        for changed_entry in artifact.manifest.changed_files:
            if not is_sandbox_publishable_workspace_path(changed_entry.path):
                raise PublisherError("unsafe_path", relative_path=changed_entry.path)
        for deleted_entry in artifact.manifest.deleted_files:
            if not is_sandbox_publishable_workspace_path(deleted_entry.path):
                raise PublisherError("unsafe_path", relative_path=deleted_entry.path)

        # Keep disposable directory keys short and place Publisher state next
        # to (not below) the artifact mirror. Journals add a project digest,
        # transaction ID and backup filename, which can otherwise cross the
        # traditional Windows MAX_PATH boundary.
        run_key = uuid4().hex[:16]
        run_root = self._staging_root / (f"wp-{artifact.manifest.artifact_id[9:17]}-{run_key}")
        publisher_state_root = self._staging_root / f"ps-{run_key}"
        project_root = run_root / "project"
        current_root = run_root / "current"
        try:
            try:
                run_root.mkdir(parents=True, exist_ok=False)
                _restore_snapshot_tree(baseline, project_root)
                current = await self._store.materialize_workspace_revision(
                    session_id,
                    current_root,
                )
            except FileStoreError as exc:
                raise PublisherError("publish_conflict") from exc
            if _snapshot_workspace_digest(baseline) != expected_workspace_sha256:
                raise PublisherError("baseline_invalid")
            if current.revision < expected_workspace_revision or (
                current.revision == expected_workspace_revision
                and current.tree_sha256 != expected_workspace_sha256
            ):
                raise PublisherError("publish_conflict")
            _validate_touched_paths_are_current(artifact, baseline, current.entries)

            local_publisher = LocalTransactionalPublisher(
                project_root=project_root,
                state_root=publisher_state_root,
            )
            local_result = await local_publisher.publish(
                artifact,
                baseline=baseline,
                signer=signer,
            )
            changed_by_path = {entry.path: entry for entry in artifact.manifest.changed_files}
            changes = tuple(
                WorkspacePublishChange(
                    logical_path=path,
                    source_path=project_root.joinpath(*PurePosixPath(path).parts),
                    size=changed_by_path[path].after_size,
                    sha256=changed_by_path[path].after_sha256,
                )
                for path in local_result.changed_paths
            )
            store_result = await self._store.publish_workspace_changes(
                session_id,
                transaction_id=local_result.transaction_id,
                expected_workspace_revision=current.revision,
                expected_workspace_sha256=current.tree_sha256,
                changes=changes,
                deleted_paths=local_result.deleted_paths,
            )
            return local_result.model_copy(
                update={
                    "workspace_revision": store_result.revision,
                }
            )
        except PublisherError:
            raise
        except (WorkspaceVersionConflictError, WorkspaceTreeConflictError) as exc:
            raise PublisherError("publish_conflict") from exc
        except WorkspacePublishPolicyError as exc:
            raise PublisherError("unsafe_path") from exc
        except SnapshotError as exc:
            raise PublisherError("baseline_invalid") from exc
        except (FileStoreError, OSError) as exc:
            raise PublisherError("io_failure") from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, run_root, ignore_errors=True)
            await asyncio.to_thread(
                shutil.rmtree,
                publisher_state_root,
                ignore_errors=True,
            )


def _write_new_file(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if written <= 0:
                raise OSError("Sandbox validation config write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_snapshot_tree(snapshot: ProjectSnapshot, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for entry in snapshot.manifest.entries:
        content = read_snapshot_file(snapshot, entry.path)
        if content is None:
            raise SnapshotError("archive_invalid", relative_path=entry.path)
        target = destination.joinpath(*PurePosixPath(entry.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_new_file(target, content)
        os.chmod(target, entry.mode)


def _snapshot_workspace_digest(snapshot: ProjectSnapshot) -> str:
    digest = hashlib.sha256()
    entries = sorted(
        (entry for entry in snapshot.manifest.entries if entry.path != _VALIDATION_CONFIG_PATH),
        key=lambda entry: entry.path,
    )
    for entry in entries:
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_touched_paths_are_current(
    artifact: SandboxOutputArtifact,
    baseline: ProjectSnapshot,
    current_entries: tuple[WorkspaceMaterializationEntry, ...],
) -> None:
    baseline_by_path = {
        entry.path: entry
        for entry in baseline.manifest.entries
        if entry.path != _VALIDATION_CONFIG_PATH
    }
    current_by_path = {entry.logical_path: entry for entry in current_entries}
    current_folded = {path.casefold() for path in current_by_path}
    for changed_entry in artifact.manifest.changed_files:
        before = baseline_by_path.get(changed_entry.path)
        current = current_by_path.get(changed_entry.path)
        if changed_entry.status == "added":
            if before is not None or changed_entry.path.casefold() in current_folded:
                raise PublisherError("publish_conflict", relative_path=changed_entry.path)
            continue
        if (
            before is None
            or current is None
            or current.size != changed_entry.before_size
            or current.sha256 != changed_entry.before_sha256
        ):
            raise PublisherError("publish_conflict", relative_path=changed_entry.path)
    for deleted_entry in artifact.manifest.deleted_files:
        before = baseline_by_path.get(deleted_entry.path)
        current = current_by_path.get(deleted_entry.path)
        if (
            before is None
            or current is None
            or current.size != deleted_entry.before_size
            or current.sha256 != deleted_entry.before_sha256
        ):
            raise PublisherError("publish_conflict", relative_path=deleted_entry.path)


__all__ = [
    "WorkspaceSandboxArtifactPublisher",
    "WorkspaceSandboxBaselineProvider",
]
