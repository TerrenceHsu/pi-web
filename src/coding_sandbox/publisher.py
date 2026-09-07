"""Crash-recoverable local publisher for verified coding Sandbox artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import shutil
import stat
import sys
import tarfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .artifact import (
    ArtifactSigner,
    SandboxOutputArtifact,
    baseline_manifest_digest,
    canonical_json_bytes,
    hash_regular_file,
    verify_output_artifact,
)
from .snapshot import (
    ProjectSnapshot,
    SnapshotError,
    SnapshotManifest,
    SnapshotPolicy,
    scan_project_manifest,
    validate_snapshot_archive,
)
from .workspace_models import SandboxFileEntry, validate_workspace_relative_path

PUBLISHER_JOURNAL_SCHEMA: Literal["pi-agent-publisher-journal/v1"] = "pi-agent-publisher-journal/v1"
PUBLISHER_INTENT_SCHEMA: Literal["pi-agent-publisher-intent/v1"] = "pi-agent-publisher-intent/v1"
_COPY_CHUNK_SIZE = 1024 * 1024
_TEMP_PREFIX = ".pi-agent-publish-"
_T = TypeVar("_T")

PublisherErrorCode = Literal[
    "invalid_project_root",
    "invalid_state_root",
    "lock_timeout",
    "artifact_invalid",
    "baseline_invalid",
    "publish_conflict",
    "unsafe_path",
    "resource_limit",
    "journal_invalid",
    "io_failure",
    "rollback_failed",
    "recovery_conflict",
]

_ERROR_MESSAGES: dict[PublisherErrorCode, str] = {
    "invalid_project_root": "The local project root is invalid.",
    "invalid_state_root": "The publisher state root is invalid.",
    "lock_timeout": "The local project is locked by another publisher.",
    "artifact_invalid": "The signed Sandbox artifact is invalid.",
    "baseline_invalid": "The trusted project baseline is invalid.",
    "publish_conflict": "The local project changed after the Sandbox baseline.",
    "unsafe_path": "The publish plan contains an unsafe local path.",
    "resource_limit": "The publish transaction exceeds a fixed resource limit.",
    "journal_invalid": "The publisher recovery journal is invalid.",
    "io_failure": "The local publish transaction failed.",
    "rollback_failed": "The local publish transaction could not be rolled back.",
    "recovery_conflict": "Recovery found an unrelated local file change.",
}


class PublisherError(Exception):
    """Fixed-code Publisher failure that never includes file contents or OS text."""

    def __init__(
        self,
        code: PublisherErrorCode,
        *,
        relative_path: str | None = None,
    ) -> None:
        super().__init__(_ERROR_MESSAGES[code])
        self.code = code
        self.relative_path = relative_path

    def __repr__(self) -> str:
        return f"PublisherError(code={self.code!r}, relative_path={self.relative_path!r})"


PublisherAction = Literal["add", "replace", "delete"]


class PublisherPlanEntry(BaseModel):
    """One exact byte transition anchored to the trusted project baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    path: str
    action: PublisherAction
    before_size: int | None = Field(default=None, ge=0)
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    before_mode: int | None = Field(default=None, ge=0, le=0o777)
    after_size: int | None = Field(default=None, ge=0)
    after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    target_mode: int | None = Field(default=None, ge=0, le=0o777)
    artifact_member: str | None = None

    @model_validator(mode="after")
    def _validate_transition(self) -> PublisherPlanEntry:
        _validate_publish_path(self.path)
        before = (self.before_size, self.before_sha256, self.before_mode)
        after = (self.after_size, self.after_sha256, self.target_mode)
        if self.action == "add" and (
            before != (None, None, None)
            or None in after
            or self.artifact_member != "files/" + self.path
        ):
            raise ValueError("publisher add transition is inconsistent")
        if self.action == "replace" and (
            None in before or None in after or self.artifact_member != "files/" + self.path
        ):
            raise ValueError("publisher replace transition is inconsistent")
        if self.action == "delete" and (
            None in before or after != (None, None, None) or self.artifact_member is not None
        ):
            raise ValueError("publisher delete transition is inconsistent")
        return self


class PublisherIntent(BaseModel):
    """Complete immutable transaction intent stored before project mutation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-publisher-intent/v1"] = PUBLISHER_INTENT_SCHEMA
    transaction_id: str = Field(pattern=r"^publish-[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(pattern=r"^artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at_ms: int = Field(ge=0)
    plan: tuple[PublisherPlanEntry, ...]

    @model_validator(mode="after")
    def _validate_plan(self) -> PublisherIntent:
        indexes = tuple(entry.index for entry in self.plan)
        paths = tuple(entry.path for entry in self.plan)
        if indexes != tuple(range(len(self.plan))) or paths != tuple(sorted(paths)):
            raise ValueError("publisher plan must have sorted contiguous entries")
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("publisher plan paths must be unique")
        return self


PublisherJournalEvent = Literal[
    "begin",
    "backup_prepared",
    "directory_intent",
    "directory_created",
    "stage_intent",
    "stage_prepared",
    "prepare_complete",
    "apply_started",
    "applied",
    "commit",
    "rollback_started",
    "rolled_back",
    "rollback_complete",
    "recovery_started",
    "cleanup_complete",
]


class PublisherJournalRecord(BaseModel):
    """One hash-chained, canonical JSONL journal record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-publisher-journal/v1"] = PUBLISHER_JOURNAL_SCHEMA
    transaction_id: str = Field(pattern=r"^publish-[0-9a-f]{32}$")
    sequence: int = Field(ge=0)
    event: PublisherJournalEvent
    recorded_at_ms: int = Field(ge=0)
    intent: PublisherIntent | None = None
    index: int | None = Field(default=None, ge=0)
    path: str | None = None
    error_code: PublisherErrorCode | None = None
    previous_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_record(self) -> PublisherJournalRecord:
        if self.event == "begin":
            if self.intent is None or self.index is not None or self.path is not None:
                raise ValueError("publisher begin journal record is invalid")
            if self.intent.transaction_id != self.transaction_id:
                raise ValueError("publisher journal transaction id is inconsistent")
        elif self.event in {
            "backup_prepared",
            "stage_intent",
            "stage_prepared",
            "apply_started",
            "applied",
            "rolled_back",
        }:
            if self.intent is not None or self.index is None or self.path is None:
                raise ValueError("publisher indexed journal record is invalid")
            _validate_publish_path(self.path)
        elif self.event in {"directory_intent", "directory_created"}:
            if self.intent is not None or self.index is not None or self.path is None:
                raise ValueError("publisher directory journal record is invalid")
            _validate_publish_path(self.path)
        elif self.intent is not None or self.index is not None or self.path is not None:
            raise ValueError("publisher terminal journal record is invalid")
        if self.sequence == 0 and self.previous_sha256 is not None:
            raise ValueError("first publisher journal record cannot have a predecessor")
        if self.sequence > 0 and self.previous_sha256 is None:
            raise ValueError("publisher journal record requires a predecessor")
        expected = _journal_record_digest(self)
        if self.record_sha256 != expected:
            raise ValueError("publisher journal record digest is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


PublisherResultStatus = Literal["published", "already_published"]


class PublisherResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: PublisherResultStatus
    transaction_id: str = Field(pattern=r"^publish-[0-9a-f]{32}$")
    artifact_id: str = Field(pattern=r"^artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]
    workspace_revision: int | None = Field(default=None, ge=0)
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_times(self) -> PublisherResult:
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("publisher result times are inconsistent")
        return self


class PublisherRecoveryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recovered_rollbacks: tuple[str, ...]
    committed_transactions: tuple[str, ...]
    cleaned_transactions: tuple[str, ...]


PublisherFaultHook = Callable[[str, int | None], None]


class LocalTransactionalPublisher:
    """Publish verified artifact bytes into one local project transactionally."""

    def __init__(
        self,
        *,
        project_root: Path,
        state_root: Path,
        lock_timeout_seconds: float = 10.0,
        clock_ms: Callable[[], int] | None = None,
        fault_hook: PublisherFaultHook | None = None,
    ) -> None:
        if lock_timeout_seconds <= 0 or lock_timeout_seconds > 300:
            raise ValueError("publisher lock timeout must be in (0, 300]")
        self._project_root = _validate_project_root(project_root)
        self._state_root = _validate_state_root_path(state_root)
        try:
            self._state_root.relative_to(self._project_root)
        except ValueError:
            pass
        else:
            raise PublisherError("invalid_state_root")
        self._project_id = hashlib.sha256(
            os.path.normcase(str(self._project_root)).encode("utf-8")
        ).hexdigest()
        # The full digest remains in every durable intent. A 128-bit directory
        # key keeps Windows recovery paths below common legacy path limits.
        self._project_state_key = self._project_id[:32]
        self._lock_timeout_seconds = lock_timeout_seconds
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._fault_hook = fault_hook

    @property
    def project_id(self) -> str:
        return self._project_id

    async def publish(
        self,
        artifact: SandboxOutputArtifact,
        *,
        baseline: ProjectSnapshot,
        signer: ArtifactSigner,
    ) -> PublisherResult:
        task = asyncio.create_task(
            asyncio.to_thread(
                self._publish_sync,
                artifact,
                baseline,
                signer,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def recover_pending(self) -> PublisherRecoveryReport:
        task = asyncio.create_task(asyncio.to_thread(self._recover_sync))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _publish_sync(
        self,
        artifact: SandboxOutputArtifact,
        baseline: ProjectSnapshot,
        signer: ArtifactSigner,
    ) -> PublisherResult:
        project_state = self._ensure_project_state()
        with _exclusive_project_lock(
            project_state / "publisher.lock",
            timeout_seconds=self._lock_timeout_seconds,
        ):
            self._recover_locked(project_state)
            self._validate_baseline(baseline)
            try:
                verify_output_artifact(
                    artifact,
                    signer,
                    max_archive_bytes=baseline.policy.max_total_bytes,
                    max_file_bytes=baseline.policy.max_file_bytes,
                    max_file_count=baseline.policy.max_file_count,
                )
            except (OSError, ValueError) as exc:
                raise PublisherError("artifact_invalid") from exc
            intent = self._build_intent(artifact, baseline)
            existing = self._find_committed(
                project_state,
                intent,
            )
            if existing is not None:
                if not self._workspace_matches_final(
                    baseline.manifest,
                    intent.plan,
                    policy=baseline.policy,
                ):
                    raise PublisherError("publish_conflict")
                return self._result_from_intent(
                    existing,
                    status="already_published",
                )
            try:
                current_manifest = scan_project_manifest(
                    self._project_root,
                    policy=baseline.policy,
                )
            except SnapshotError as exc:
                raise PublisherError("unsafe_path") from exc
            if current_manifest != baseline.manifest:
                raise PublisherError("publish_conflict")

            transaction_root = self._create_transaction_root(project_state, intent)
            journal = _JournalWriter(
                transaction_root / "journal.jsonl",
                transaction_id=intent.transaction_id,
                clock_ms=self._clock_ms,
            )
            journal.append("begin", intent=intent)
            committed = False
            try:
                self._prepare_transaction(
                    transaction_root,
                    journal,
                    intent,
                    artifact,
                )
                journal.append("prepare_complete")
                self._fault("after_prepare", None)
                self._apply_transaction(transaction_root, journal, intent)
                if not self._workspace_matches_final(
                    baseline.manifest,
                    intent.plan,
                    policy=baseline.policy,
                ):
                    raise PublisherError("publish_conflict")
                try:
                    verify_output_artifact(
                        artifact,
                        signer,
                        max_archive_bytes=baseline.policy.max_total_bytes,
                        max_file_bytes=baseline.policy.max_file_bytes,
                        max_file_count=baseline.policy.max_file_count,
                    )
                except (OSError, ValueError) as exc:
                    raise PublisherError("artifact_invalid") from exc
                self._fault("before_commit", None)
                journal.append("commit")
                committed = True
                self._fault("after_commit", None)
            except Exception as exc:
                if not committed:
                    try:
                        journal.append(
                            "rollback_started",
                            error_code=(
                                exc.code if isinstance(exc, PublisherError) else "io_failure"
                            ),
                        )
                        self._rollback_transaction(transaction_root, journal, intent)
                        journal.append("rollback_complete")
                    except Exception as rollback_exc:
                        raise PublisherError("rollback_failed") from rollback_exc
                if isinstance(exc, PublisherError):
                    raise
                raise PublisherError("io_failure") from exc

            try:
                self._cleanup_transaction_material(transaction_root, intent)
                journal.append("cleanup_complete")
            except Exception:
                # Commit is the durable point. Startup recovery can repeat cleanup.
                pass
            return self._result_from_intent(intent, status="published")

    def _recover_sync(self) -> PublisherRecoveryReport:
        project_state = self._ensure_project_state()
        with _exclusive_project_lock(
            project_state / "publisher.lock",
            timeout_seconds=self._lock_timeout_seconds,
        ):
            return self._recover_locked(project_state)

    def _recover_locked(self, project_state: Path) -> PublisherRecoveryReport:
        transactions_root = project_state / "transactions"
        _ensure_directory(transactions_root)
        recovered: list[str] = []
        committed: list[str] = []
        cleaned: list[str] = []
        for transaction_root in sorted(transactions_root.iterdir()):
            if _is_link_or_reparse(transaction_root) or not transaction_root.is_dir():
                raise PublisherError("journal_invalid")
            if not transaction_root.name.startswith("publish-"):
                raise PublisherError("journal_invalid")
            journal_path = transaction_root / "journal.jsonl"
            if not journal_path.exists():
                _remove_internal_tree(transaction_root, transactions_root)
                continue
            records = _load_journal(journal_path, repair_torn_tail=True)
            if not records or records[0].event != "begin" or records[0].intent is None:
                raise PublisherError("journal_invalid")
            intent = records[0].intent
            if (
                intent.project_id != self._project_id
                or intent.transaction_id != transaction_root.name
            ):
                raise PublisherError("journal_invalid")
            events = {record.event for record in records}
            writer = _JournalWriter(
                journal_path,
                transaction_id=intent.transaction_id,
                clock_ms=self._clock_ms,
                records=records,
            )
            if "commit" in events:
                committed.append(intent.transaction_id)
                try:
                    self._cleanup_transaction_material(transaction_root, intent)
                    if "cleanup_complete" not in events:
                        writer.append("cleanup_complete")
                    cleaned.append(intent.transaction_id)
                except Exception as exc:
                    raise PublisherError("io_failure") from exc
                continue
            if "rollback_complete" not in events:
                writer.append("recovery_started")
                try:
                    self._rollback_transaction(transaction_root, writer, intent)
                    writer.append("rollback_complete")
                except Exception as exc:
                    if isinstance(exc, PublisherError):
                        raise
                    raise PublisherError("rollback_failed") from exc
                recovered.append(intent.transaction_id)
            try:
                self._cleanup_transaction_material(transaction_root, intent)
                if "cleanup_complete" not in events:
                    writer.append("cleanup_complete")
                cleaned.append(intent.transaction_id)
            except Exception as exc:
                raise PublisherError("io_failure") from exc
        return PublisherRecoveryReport(
            recovered_rollbacks=tuple(recovered),
            committed_transactions=tuple(committed),
            cleaned_transactions=tuple(cleaned),
        )

    def _validate_baseline(self, baseline: ProjectSnapshot) -> None:
        try:
            size, digest = hash_regular_file(
                baseline.archive_path,
                max_bytes=baseline.policy.max_total_bytes,
            )
            manifest = validate_snapshot_archive(
                baseline.archive_path,
                policy=baseline.policy,
            )
        except (OSError, SnapshotError, ValueError) as exc:
            raise PublisherError("baseline_invalid") from exc
        if (
            size != baseline.archive_size
            or digest != baseline.archive_sha256
            or manifest != baseline.manifest
        ):
            raise PublisherError("baseline_invalid")

    def _build_intent(
        self,
        artifact: SandboxOutputArtifact,
        baseline: ProjectSnapshot,
    ) -> PublisherIntent:
        baseline_entries = {entry.path: entry for entry in baseline.manifest.entries}
        baseline_file_entries = tuple(
            SandboxFileEntry(
                path=entry.path,
                size=entry.size,
                sha256=entry.sha256,
            )
            for entry in baseline.manifest.entries
        )
        expected_baseline = baseline_manifest_digest(baseline_file_entries)
        if artifact.manifest.baseline_sha256 != expected_baseline:
            raise PublisherError("artifact_invalid")
        raw_plan: list[PublisherPlanEntry] = []
        seen: set[str] = set()
        for changed in artifact.manifest.changed_files:
            path = _artifact_publish_path(changed.path)
            if path.casefold() in seen:
                raise PublisherError("artifact_invalid")
            seen.add(path.casefold())
            before = baseline_entries.get(path)
            if changed.status == "added":
                if before is not None:
                    raise PublisherError("artifact_invalid")
                raw_plan.append(
                    PublisherPlanEntry(
                        index=0,
                        path=path,
                        action="add",
                        after_size=changed.after_size,
                        after_sha256=changed.after_sha256,
                        target_mode=_default_target_mode(),
                        artifact_member=changed.member_path,
                    )
                )
            else:
                if (
                    before is None
                    or changed.before_size != before.size
                    or changed.before_sha256 != before.sha256
                ):
                    raise PublisherError("artifact_invalid")
                raw_plan.append(
                    PublisherPlanEntry(
                        index=0,
                        path=path,
                        action="replace",
                        before_size=before.size,
                        before_sha256=before.sha256,
                        before_mode=before.mode,
                        after_size=changed.after_size,
                        after_sha256=changed.after_sha256,
                        target_mode=before.mode,
                        artifact_member=changed.member_path,
                    )
                )
        for deleted in artifact.manifest.deleted_files:
            path = _artifact_publish_path(deleted.path)
            if path.casefold() in seen:
                raise PublisherError("artifact_invalid")
            seen.add(path.casefold())
            before = baseline_entries.get(path)
            if (
                before is None
                or deleted.before_size != before.size
                or deleted.before_sha256 != before.sha256
            ):
                raise PublisherError("artifact_invalid")
            raw_plan.append(
                PublisherPlanEntry(
                    index=0,
                    path=path,
                    action="delete",
                    before_size=before.size,
                    before_sha256=before.sha256,
                    before_mode=before.mode,
                )
            )
        ordered = tuple(
            entry.model_copy(update={"index": index})
            for index, entry in enumerate(sorted(raw_plan, key=lambda item: item.path))
        )
        return PublisherIntent(
            transaction_id=f"publish-{uuid4().hex}",
            project_id=self._project_id,
            artifact_id=artifact.manifest.artifact_id,
            artifact_sha256=artifact.archive_sha256,
            artifact_manifest_sha256=artifact.manifest.manifest_sha256,
            baseline_archive_sha256=baseline.archive_sha256,
            baseline_manifest_sha256=baseline.manifest.manifest_sha256,
            baseline_file_sha256=expected_baseline,
            started_at_ms=self._clock_ms(),
            plan=ordered,
        )

    def _prepare_transaction(
        self,
        transaction_root: Path,
        journal: _JournalWriter,
        intent: PublisherIntent,
        artifact: SandboxOutputArtifact,
    ) -> None:
        backups_root = transaction_root / "backups"
        _ensure_directory(backups_root)
        for entry in intent.plan:
            if entry.action == "add":
                self._require_target_absent(entry.path)
            else:
                target = self._existing_regular_target(entry.path)
                _copy_verified_file(
                    target,
                    self._backup_path(transaction_root, entry.index),
                    expected_size=_required(entry.before_size),
                    expected_sha256=_required(entry.before_sha256),
                    max_bytes=_required(entry.before_size),
                    mode=_backup_file_mode(),
                )
            journal.append(
                "backup_prepared",
                index=entry.index,
                path=entry.path,
            )
            self._fault("after_backup", entry.index)

        with tarfile.open(artifact.archive_path, mode="r:") as archive:
            for entry in intent.plan:
                if entry.action == "delete":
                    continue
                self._ensure_parent_directories(entry.path, journal)
                stage = self._stage_path(intent, entry)
                journal.append(
                    "stage_intent",
                    index=entry.index,
                    path=entry.path,
                )
                self._stage_artifact_member(
                    archive,
                    entry,
                    stage,
                )
                journal.append(
                    "stage_prepared",
                    index=entry.index,
                    path=entry.path,
                )
                self._fault("after_stage", entry.index)

    def _apply_transaction(
        self,
        transaction_root: Path,
        journal: _JournalWriter,
        intent: PublisherIntent,
    ) -> None:
        del transaction_root
        for entry in intent.plan:
            journal.append("apply_started", index=entry.index, path=entry.path)
            self._apply_entry(intent, entry)
            journal.append("applied", index=entry.index, path=entry.path)
            self._fault("after_apply", entry.index)

    def _apply_entry(
        self,
        intent: PublisherIntent,
        entry: PublisherPlanEntry,
    ) -> None:
        target = self._target_path(entry.path)
        parent = target.parent
        _validate_existing_directory(parent)
        stage = self._stage_path(intent, entry)
        if entry.action == "add":
            self._require_target_absent(entry.path)
            try:
                os.link(stage, target, follow_symlinks=False)
            except OSError as exc:
                raise PublisherError("io_failure", relative_path=entry.path) from exc
            stage.unlink(missing_ok=True)
        elif entry.action == "replace":
            self._assert_target_hash(
                entry.path,
                expected_size=_required(entry.before_size),
                expected_sha256=_required(entry.before_sha256),
            )
            os.replace(stage, target)
        else:
            self._assert_target_hash(
                entry.path,
                expected_size=_required(entry.before_size),
                expected_sha256=_required(entry.before_sha256),
            )
            target.unlink()
        _fsync_directory(parent)
        if entry.action != "delete":
            self._assert_target_hash(
                entry.path,
                expected_size=_required(entry.after_size),
                expected_sha256=_required(entry.after_sha256),
            )

    def _rollback_transaction(
        self,
        transaction_root: Path,
        journal: _JournalWriter,
        intent: PublisherIntent,
    ) -> None:
        for entry in reversed(intent.plan):
            self._rollback_entry(transaction_root, intent, entry)
            journal.append("rolled_back", index=entry.index, path=entry.path)
        self._remove_created_directories(journal.records)

    def _rollback_entry(
        self,
        transaction_root: Path,
        intent: PublisherIntent,
        entry: PublisherPlanEntry,
    ) -> None:
        stage = self._stage_path(intent, entry)
        _remove_safe_internal_file(stage, expected_prefix=_TEMP_PREFIX)
        target = self._target_path(entry.path)
        current = self._observe_target(entry.path)
        if entry.action == "add":
            if current is None:
                return
            if current != (_required(entry.after_size), _required(entry.after_sha256)):
                raise PublisherError("recovery_conflict", relative_path=entry.path)
            target.unlink()
            _fsync_directory(target.parent)
            return

        before = (_required(entry.before_size), _required(entry.before_sha256))
        if current == before:
            return
        after = (
            None
            if entry.action == "delete"
            else (_required(entry.after_size), _required(entry.after_sha256))
        )
        if current is not None and current != after:
            raise PublisherError("recovery_conflict", relative_path=entry.path)
        backup = self._backup_path(transaction_root, entry.index)
        try:
            backup_size, backup_digest = hash_regular_file(
                backup,
                max_bytes=_required(entry.before_size),
            )
        except (OSError, ValueError) as exc:
            raise PublisherError("rollback_failed", relative_path=entry.path) from exc
        if backup_size != before[0] or backup_digest != before[1]:
            raise PublisherError("rollback_failed", relative_path=entry.path)
        _validate_existing_directory(target.parent)
        restore = target.parent / (f"{_TEMP_PREFIX}{transaction_root.name}-{entry.index}.restore")
        _copy_verified_file(
            backup,
            restore,
            expected_size=before[0],
            expected_sha256=before[1],
            max_bytes=before[0],
            mode=_required(entry.before_mode),
        )
        os.replace(restore, target)
        _fsync_directory(target.parent)
        self._assert_target_hash(
            entry.path,
            expected_size=before[0],
            expected_sha256=before[1],
        )

    def _ensure_parent_directories(
        self,
        relative_path: str,
        journal: _JournalWriter,
    ) -> None:
        parts = PurePosixPath(relative_path).parts[:-1]
        current = self._project_root
        relative = PurePosixPath()
        for part in parts:
            relative /= part
            current /= part
            if current.exists():
                _validate_existing_directory(current)
                continue
            relative_text = relative.as_posix()
            journal.append("directory_intent", path=relative_text)
            try:
                current.mkdir()
            except OSError as exc:
                raise PublisherError(
                    "publish_conflict",
                    relative_path=relative_text,
                ) from exc
            _validate_existing_directory(current)
            _fsync_directory(current.parent)
            journal.append("directory_created", path=relative_text)

    def _remove_created_directories(
        self,
        records: tuple[PublisherJournalRecord, ...],
    ) -> None:
        intended_paths = {
            record.path
            for record in records
            if record.event == "directory_intent" and record.path is not None
        }
        created_paths = {
            record.path
            for record in records
            if record.event == "directory_created" and record.path is not None
        }
        for relative_path in sorted(
            intended_paths,
            key=lambda value: len(PurePosixPath(value).parts),
            reverse=True,
        ):
            target = self._target_path(relative_path)
            if not target.exists():
                continue
            if relative_path not in created_paths:
                # The durable intent precedes mkdir. Without the completion
                # record, an empty directory could belong to a post-crash user
                # edit, so recovery must stop instead of guessing ownership.
                raise PublisherError(
                    "recovery_conflict",
                    relative_path=relative_path,
                )
            _validate_existing_directory(target)
            try:
                target.rmdir()
            except OSError as exc:
                raise PublisherError(
                    "recovery_conflict",
                    relative_path=relative_path,
                ) from exc
            _fsync_directory(target.parent)

    def _stage_artifact_member(
        self,
        archive: tarfile.TarFile,
        entry: PublisherPlanEntry,
        stage: Path,
    ) -> None:
        member_name = _required(entry.artifact_member)
        try:
            member = archive.getmember(member_name)
            source = archive.extractfile(member)
        except (KeyError, tarfile.TarError) as exc:
            raise PublisherError("artifact_invalid") from exc
        if source is None or not member.isfile():
            raise PublisherError("artifact_invalid")
        _write_verified_stream(
            source,
            stage,
            expected_size=_required(entry.after_size),
            expected_sha256=_required(entry.after_sha256),
            mode=_required(entry.target_mode),
        )

    def _workspace_matches_final(
        self,
        baseline: SnapshotManifest,
        plan: tuple[PublisherPlanEntry, ...],
        *,
        policy: SnapshotPolicy,
    ) -> bool:
        expected = {
            entry.path: (entry.size, entry.sha256, entry.mode) for entry in baseline.entries
        }
        for entry in plan:
            if entry.action == "delete":
                expected.pop(entry.path, None)
            else:
                expected[entry.path] = (
                    _required(entry.after_size),
                    _required(entry.after_sha256),
                    _required(entry.target_mode),
                )
        try:
            current = scan_project_manifest(self._project_root, policy=policy)
        except SnapshotError:
            return False
        observed = {entry.path: (entry.size, entry.sha256, entry.mode) for entry in current.entries}
        return observed == expected

    def _find_committed(
        self,
        project_state: Path,
        candidate: PublisherIntent,
    ) -> PublisherIntent | None:
        transactions_root = project_state / "transactions"
        for transaction_root in sorted(transactions_root.iterdir()):
            journal_path = transaction_root / "journal.jsonl"
            if not journal_path.is_file():
                continue
            records = _load_journal(journal_path, repair_torn_tail=False)
            if not records or records[0].intent is None:
                raise PublisherError("journal_invalid")
            intent = records[0].intent
            if (
                "commit" in {record.event for record in records}
                and intent.artifact_sha256 == candidate.artifact_sha256
                and intent.artifact_manifest_sha256 == candidate.artifact_manifest_sha256
                and intent.baseline_archive_sha256 == candidate.baseline_archive_sha256
            ):
                return intent
        return None

    def _cleanup_transaction_material(
        self,
        transaction_root: Path,
        intent: PublisherIntent,
    ) -> None:
        for entry in intent.plan:
            _remove_safe_internal_file(
                self._stage_path(intent, entry),
                expected_prefix=_TEMP_PREFIX,
            )
            restore = self._target_path(entry.path).parent / (
                f"{_TEMP_PREFIX}{transaction_root.name}-{entry.index}.restore"
            )
            _remove_safe_internal_file(restore, expected_prefix=_TEMP_PREFIX)
        backups = transaction_root / "backups"
        if backups.exists():
            _remove_internal_tree(backups, transaction_root)

    def _create_transaction_root(
        self,
        project_state: Path,
        intent: PublisherIntent,
    ) -> Path:
        transactions = project_state / "transactions"
        _ensure_directory(transactions)
        target = transactions / intent.transaction_id
        try:
            target.mkdir(mode=0o700)
        except OSError as exc:
            raise PublisherError("io_failure") from exc
        _validate_existing_directory(target)
        return target

    def _ensure_project_state(self) -> Path:
        _ensure_directory(self._state_root)
        projects = self._state_root / "projects"
        _ensure_directory(projects)
        project_state = projects / self._project_state_key
        _ensure_directory(project_state)
        _ensure_directory(project_state / "transactions")
        return project_state

    def _target_path(self, relative_path: str) -> Path:
        try:
            safe = _validate_publish_path(relative_path)
        except ValueError as exc:
            raise PublisherError(
                "unsafe_path",
                relative_path=relative_path,
            ) from exc
        target = self._project_root.joinpath(*PurePosixPath(safe).parts)
        try:
            target.relative_to(self._project_root)
        except ValueError as exc:
            raise PublisherError("unsafe_path", relative_path=relative_path) from exc
        return target

    def _stage_path(
        self,
        intent: PublisherIntent,
        entry: PublisherPlanEntry,
    ) -> Path:
        target = self._target_path(entry.path)
        return target.parent / (
            f"{_TEMP_PREFIX}{intent.transaction_id}-{entry.index}-"
            f"{hashlib.sha256(entry.path.encode()).hexdigest()[:16]}.tmp"
        )

    @staticmethod
    def _backup_path(transaction_root: Path, index: int) -> Path:
        return transaction_root / "backups" / f"{index:08d}.bin"

    def _existing_regular_target(self, relative_path: str) -> Path:
        target = self._target_path(relative_path)
        _validate_path_components(self._project_root, relative_path, target_must_exist=True)
        try:
            info = target.stat(follow_symlinks=False)
        except OSError as exc:
            raise PublisherError("publish_conflict", relative_path=relative_path) from exc
        if not stat.S_ISREG(info.st_mode) or _is_link_or_reparse(target):
            raise PublisherError("unsafe_path", relative_path=relative_path)
        return target

    def _require_target_absent(self, relative_path: str) -> None:
        target = self._target_path(relative_path)
        _validate_path_components(self._project_root, relative_path, target_must_exist=False)
        try:
            target.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PublisherError("unsafe_path", relative_path=relative_path) from exc
        raise PublisherError("publish_conflict", relative_path=relative_path)

    def _assert_target_hash(
        self,
        relative_path: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        target = self._existing_regular_target(relative_path)
        try:
            size, digest = hash_regular_file(target, max_bytes=expected_size)
        except (OSError, ValueError) as exc:
            raise PublisherError("publish_conflict", relative_path=relative_path) from exc
        if size != expected_size or digest != expected_sha256:
            raise PublisherError("publish_conflict", relative_path=relative_path)

    def _observe_target(self, relative_path: str) -> tuple[int, str] | None:
        target = self._target_path(relative_path)
        _validate_path_components(self._project_root, relative_path, target_must_exist=False)
        try:
            info = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PublisherError("unsafe_path", relative_path=relative_path) from exc
        if not stat.S_ISREG(info.st_mode) or _is_link_or_reparse(target):
            raise PublisherError("recovery_conflict", relative_path=relative_path)
        try:
            return hash_regular_file(target, max_bytes=max(1, info.st_size))
        except (OSError, ValueError) as exc:
            raise PublisherError("recovery_conflict", relative_path=relative_path) from exc

    def _result_from_intent(
        self,
        intent: PublisherIntent,
        *,
        status: PublisherResultStatus,
    ) -> PublisherResult:
        return PublisherResult(
            status=status,
            transaction_id=intent.transaction_id,
            artifact_id=intent.artifact_id,
            artifact_sha256=intent.artifact_sha256,
            changed_paths=tuple(entry.path for entry in intent.plan if entry.action != "delete"),
            deleted_paths=tuple(entry.path for entry in intent.plan if entry.action == "delete"),
            started_at_ms=intent.started_at_ms,
            finished_at_ms=self._clock_ms(),
        )

    def _fault(self, point: str, index: int | None) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, index)


class _JournalWriter:
    def __init__(
        self,
        path: Path,
        *,
        transaction_id: str,
        clock_ms: Callable[[], int],
        records: tuple[PublisherJournalRecord, ...] = (),
    ) -> None:
        self.path = path
        self.transaction_id = transaction_id
        self.clock_ms = clock_ms
        self._records = list(records)

    @property
    def records(self) -> tuple[PublisherJournalRecord, ...]:
        return tuple(self._records)

    def append(
        self,
        event: PublisherJournalEvent,
        *,
        intent: PublisherIntent | None = None,
        index: int | None = None,
        path: str | None = None,
        error_code: PublisherErrorCode | None = None,
    ) -> PublisherJournalRecord:
        sequence = len(self._records)
        previous = self._records[-1].record_sha256 if self._records else None
        core = {
            "schema_version": PUBLISHER_JOURNAL_SCHEMA,
            "transaction_id": self.transaction_id,
            "sequence": sequence,
            "event": event,
            "recorded_at_ms": self.clock_ms(),
            "intent": None if intent is None else intent.model_dump(mode="json"),
            "index": index,
            "path": path,
            "error_code": error_code,
            "previous_sha256": previous,
        }
        record = PublisherJournalRecord(
            **core,
            record_sha256=hashlib.sha256(canonical_json_bytes(core)).hexdigest(),
        )
        _append_journal_bytes(self.path, record.canonical_bytes() + b"\n")
        self._records.append(record)
        return record


def _journal_record_digest(record: PublisherJournalRecord) -> str:
    core = record.model_dump(mode="json", exclude={"record_sha256"})
    return hashlib.sha256(canonical_json_bytes(core)).hexdigest()


def _append_journal_bytes(path: Path, value: bytes) -> None:
    _validate_existing_directory(path.parent)
    if path.exists() and _is_link_or_reparse(path):
        raise PublisherError("journal_invalid")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise PublisherError("journal_invalid")
            offset = 0
            while offset < len(value):
                offset += os.write(descriptor, value[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("io_failure") from exc


def _load_journal(
    path: Path,
    *,
    repair_torn_tail: bool,
) -> tuple[PublisherJournalRecord, ...]:
    try:
        if _is_link_or_reparse(path):
            raise PublisherError("journal_invalid")
        raw = path.read_bytes()
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("journal_invalid") from exc
    valid_length = len(raw)
    if raw and not raw.endswith(b"\n"):
        marker = raw.rfind(b"\n")
        valid_length = marker + 1
        if not repair_torn_tail:
            raise PublisherError("journal_invalid")
        tail = raw[valid_length:]
        _preserve_torn_tail(path, tail)
        try:
            with path.open("r+b") as stream:
                stream.truncate(valid_length)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise PublisherError("journal_invalid") from exc
        raw = raw[:valid_length]
    records: list[PublisherJournalRecord] = []
    previous: str | None = None
    for sequence, line in enumerate(raw.splitlines()):
        if not line:
            raise PublisherError("journal_invalid")
        try:
            record = PublisherJournalRecord.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise PublisherError("journal_invalid") from exc
        if record.canonical_bytes() != line:
            raise PublisherError("journal_invalid")
        if (
            record.sequence != sequence
            or record.previous_sha256 != previous
            or (records and record.transaction_id != records[0].transaction_id)
        ):
            raise PublisherError("journal_invalid")
        records.append(record)
        previous = record.record_sha256
    return tuple(records)


def _preserve_torn_tail(path: Path, tail: bytes) -> None:
    if not tail:
        return
    digest = hashlib.sha256(tail).hexdigest()[:32]
    target = path.with_name(f"journal.torn-{digest}.bin")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        return
    try:
        offset = 0
        while offset < len(tail):
            offset += os.write(descriptor, tail[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_project_lock(
    path: Path,
    *,
    timeout_seconds: float,
) -> Iterator[None]:
    _validate_existing_directory(path.parent)
    if path.exists() and _is_link_or_reparse(path):
        raise PublisherError("invalid_state_root")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(path, flags, 0o600)
        descriptor_info = os.fstat(descriptor)
        path_info = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or _is_link_or_reparse(path)
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise PublisherError("invalid_state_root")
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _lock_descriptor(descriptor)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PublisherError("lock_timeout") from None
                time.sleep(0.05)
        yield
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("io_failure") from exc
    finally:
        if descriptor is not None:
            if locked:
                try:
                    _unlock_descriptor(descriptor)
                except OSError:
                    # Closing the descriptor releases the process lock as a final
                    # fallback on both supported platforms.
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def _lock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.__dict__["flock"](
            descriptor,
            fcntl.__dict__["LOCK_EX"] | fcntl.__dict__["LOCK_NB"],
        )


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.__dict__["flock"](descriptor, fcntl.__dict__["LOCK_UN"])


def _validate_project_root(path: Path) -> Path:
    try:
        if not path.is_absolute() or not path.is_dir() or _is_link_or_reparse(path):
            raise PublisherError("invalid_project_root")
        return path.resolve(strict=True)
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("invalid_project_root") from exc


def _validate_state_root_path(path: Path) -> Path:
    try:
        if not path.is_absolute():
            raise PublisherError("invalid_state_root")
        resolved = path.resolve(strict=False)
        if path.exists() and (_is_link_or_reparse(path) or not path.is_dir()):
            raise PublisherError("invalid_state_root")
        return resolved
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("invalid_state_root") from exc


def _validate_publish_path(value: str) -> str:
    try:
        validate_workspace_relative_path(value)
    except Exception as exc:
        raise ValueError("publisher path is invalid") from exc
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
    for part in PurePosixPath(value).parts:
        stem = part.split(".", 1)[0].casefold()
        if not part or part.endswith((" ", ".")) or ":" in part or stem in reserved:
            raise ValueError("publisher path is invalid")
    return value


def _artifact_publish_path(value: str) -> str:
    try:
        return _validate_publish_path(value)
    except ValueError as exc:
        raise PublisherError("artifact_invalid", relative_path=value) from exc


def _validate_path_components(
    root: Path,
    relative_path: str,
    *,
    target_must_exist: bool,
) -> None:
    current = root
    try:
        parts = PurePosixPath(_validate_publish_path(relative_path)).parts
    except ValueError as exc:
        raise PublisherError("unsafe_path", relative_path=relative_path) from exc
    for index, part in enumerate(parts):
        current /= part
        is_target = index == len(parts) - 1
        try:
            current.lstat()
        except FileNotFoundError:
            if is_target and not target_must_exist:
                return
            if not is_target and not target_must_exist:
                # Remaining parent directories may be created by the transaction.
                return
            raise PublisherError("publish_conflict", relative_path=relative_path) from None
        except OSError as exc:
            raise PublisherError("unsafe_path", relative_path=relative_path) from exc
        if _is_link_or_reparse(current):
            raise PublisherError("unsafe_path", relative_path=relative_path)
        if not is_target and not current.is_dir():
            raise PublisherError("unsafe_path", relative_path=relative_path)


def _validate_existing_directory(path: Path) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise PublisherError("unsafe_path") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_link_or_reparse(path):
        raise PublisherError("unsafe_path")


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _validate_existing_directory(path)
    except PublisherError:
        raise
    except OSError as exc:
        raise PublisherError("invalid_state_root") from exc


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PublisherError("unsafe_path") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _copy_verified_file(
    source: Path,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_bytes: int,
    mode: int,
) -> None:
    try:
        source_info = source.stat(follow_symlinks=False)
        if _is_link_or_reparse(source) or not stat.S_ISREG(source_info.st_mode):
            raise PublisherError("unsafe_path")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as stream:
                opened_info = os.fstat(stream.fileno())
                while chunk := stream.read(_COPY_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise PublisherError("resource_limit")
                    digest.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(descriptor, chunk[offset:])
                final_info = os.fstat(stream.fileno())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if (
            _file_identity(source_info) != _file_identity(opened_info)
            or _file_identity(source_info) != _file_identity(final_info)
            or size != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            target.unlink(missing_ok=True)
            raise PublisherError("publish_conflict")
        target.chmod(mode)
    except PublisherError:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise PublisherError("io_failure") from exc


def _write_verified_stream(
    source: object,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    mode: int,
) -> None:
    reader = getattr(source, "read", None)
    if reader is None:
        raise PublisherError("artifact_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := reader(_COPY_CHUNK_SIZE):
                if not isinstance(chunk, bytes):
                    raise PublisherError("artifact_invalid")
                size += len(chunk)
                if size > expected_size:
                    raise PublisherError("artifact_invalid")
                digest.update(chunk)
                offset = 0
                while offset < len(chunk):
                    offset += os.write(descriptor, chunk[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise PublisherError("artifact_invalid")
        target.chmod(mode)
    except PublisherError:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise PublisherError("io_failure") from exc


def _remove_safe_internal_file(path: Path, *, expected_prefix: str) -> None:
    if not path.name.startswith(expected_prefix):
        raise PublisherError("unsafe_path")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PublisherError("unsafe_path") from exc
    if not stat.S_ISREG(info.st_mode) or _is_link_or_reparse(path):
        raise PublisherError("unsafe_path")
    try:
        path.chmod(0o600)
        path.unlink()
    except OSError as exc:
        raise PublisherError("io_failure") from exc


def _remove_internal_tree(path: Path, allowed_parent: Path) -> None:
    try:
        resolved_parent = allowed_parent.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_parent)
    except (OSError, ValueError) as exc:
        raise PublisherError("unsafe_path") from exc
    if resolved == resolved_parent or _is_link_or_reparse(path):
        raise PublisherError("unsafe_path")
    shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _default_target_mode() -> int:
    return 0o666 if os.name == "nt" else 0o644


def _backup_file_mode() -> int:
    return 0o600 if os.name == "nt" else 0o400


def _required(value: _T | None) -> _T:
    if value is None:
        raise PublisherError("journal_invalid")
    return value


__all__ = [
    "LocalTransactionalPublisher",
    "PUBLISHER_INTENT_SCHEMA",
    "PUBLISHER_JOURNAL_SCHEMA",
    "PublisherError",
    "PublisherErrorCode",
    "PublisherIntent",
    "PublisherJournalEvent",
    "PublisherJournalRecord",
    "PublisherPlanEntry",
    "PublisherRecoveryReport",
    "PublisherResult",
    "PublisherResultStatus",
]
