"""Durable Web-facing lifecycle for managed coding Sandbox operations."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tarfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .admin.models import SandboxAdminConfig, SandboxConfigRecord
from .artifact import (
    ArtifactSigner,
    SandboxOutputArtifact,
    hash_regular_file,
    verify_artifact_signature,
)
from .backend import SandboxBackend
from .models import SandboxCreateSpec, SandboxHandle, SandboxOutputChunk
from .operation import SandboxOperation
from .publisher import LocalTransactionalPublisher, PublisherError, PublisherResult
from .snapshot import (
    ProjectSnapshot,
    SnapshotError,
    SnapshotPolicy,
    build_project_snapshot,
    read_snapshot_file,
)
from .validation import (
    SandboxValidationConfigError,
    SandboxValidationEvidence,
    load_sandbox_validation_plan,
)
from .workspace_models import (
    CodingWorkspace,
    SandboxDiffResult,
    SandboxFileEntry,
    SandboxWorkspaceError,
    validate_workspace_relative_path,
)

MANAGED_OPERATION_SCHEMA: Literal["pi-agent-managed-sandbox-operation/v1"] = (
    "pi-agent-managed-sandbox-operation/v1"
)
MANAGED_EVENT_SCHEMA: Literal["pi-agent-managed-sandbox-event/v1"] = (
    "pi-agent-managed-sandbox-event/v1"
)
SANDBOX_STATE_MACHINE_VERSION: Literal["pi-agent-managed-sandbox-state/v2"] = (
    "pi-agent-managed-sandbox-state/v2"
)
_STORE_SCHEMA_VERSION = 2
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_MAX_EVENT_BYTES = 512 * 1024
_MAX_EVENTS_PER_OPERATION = 2_000

_PYTHON_SYNTAX_CHECK = (
    "import pathlib;[compile(p.read_bytes(),str(p),'exec')for p in pathlib.Path('.').rglob('*.py')]"
)
DEFAULT_VALIDATION_CONFIG = (
    "version = 1\n\n"
    "[[required_checks]]\n"
    'id = "python-syntax"\n'
    f'argv = ["python3", "-c", "{_PYTHON_SYNTAX_CHECK}"]\n'
    'cwd = "."\n'
    "timeout_seconds = 60\n"
).encode()

ManagedOperationStatus = Literal[
    "creating",
    "ready",
    "validating",
    "validation_failed",
    "validated",
    "freezing",
    "awaiting_approval",
    "publishing",
    "publish_conflict",
    "published",
    "cancelling",
    "cancelled",
    "discarding",
    "discarded",
    "failed",
    "interrupted",
]
SandboxOperationAction = Literal[
    "validate",
    "prepare_publish",
    "publish",
    "refreeze",
    "retry_publish",
    "cancel",
    "discard",
]

_TERMINAL_STATUSES: frozenset[ManagedOperationStatus] = frozenset(
    {"published", "cancelled", "discarded", "failed", "interrupted"}
)

_ALLOWED_STATUS_TRANSITIONS: dict[
    ManagedOperationStatus,
    frozenset[ManagedOperationStatus],
] = {
    "creating": frozenset({"ready", "cancelling", "discarding", "failed", "interrupted"}),
    "ready": frozenset({"validating", "cancelling", "discarding", "failed", "interrupted"}),
    "validating": frozenset(
        {
            "validated",
            "validation_failed",
            "cancelling",
            "discarding",
            "failed",
            "interrupted",
        }
    ),
    "validation_failed": frozenset(
        {"ready", "validating", "cancelling", "discarding", "failed", "interrupted"}
    ),
    "validated": frozenset(
        {"ready", "validating", "freezing", "cancelling", "discarding", "failed", "interrupted"}
    ),
    "freezing": frozenset(
        {
            "awaiting_approval",
            "publish_conflict",
            "validation_failed",
            "cancelling",
            "discarding",
            "failed",
            "interrupted",
        }
    ),
    "awaiting_approval": frozenset(
        {"publishing", "cancelling", "discarding", "failed", "interrupted"}
    ),
    "publishing": frozenset({"published", "publish_conflict", "failed", "interrupted"}),
    "publish_conflict": frozenset(
        {"freezing", "publishing", "cancelling", "discarding", "failed", "interrupted"}
    ),
    "published": frozenset(),
    "cancelling": frozenset({"cancelled", "failed", "interrupted"}),
    "cancelled": frozenset(),
    "discarding": frozenset({"discarded", "failed", "interrupted"}),
    "discarded": frozenset(),
    "failed": frozenset({"discarding"}),
    "interrupted": frozenset({"discarding"}),
}

_ALLOWED_ACTIONS: dict[ManagedOperationStatus, tuple[SandboxOperationAction, ...]] = {
    "creating": ("cancel", "discard"),
    "ready": ("validate", "cancel", "discard"),
    "validating": ("cancel", "discard"),
    "validation_failed": ("validate", "cancel", "discard"),
    "validated": ("validate", "prepare_publish", "cancel", "discard"),
    "freezing": ("cancel", "discard"),
    "awaiting_approval": ("publish", "cancel", "discard"),
    "publishing": (),
    "publish_conflict": ("refreeze", "retry_publish", "cancel", "discard"),
    "published": (),
    "cancelling": (),
    "cancelled": (),
    "discarding": (),
    "discarded": (),
    "failed": ("discard",),
    "interrupted": ("discard",),
}


def allowed_sandbox_transitions(
    status: ManagedOperationStatus,
) -> tuple[ManagedOperationStatus, ...]:
    """Return the state machine's stable, public successor list."""
    return tuple(sorted(_ALLOWED_STATUS_TRANSITIONS[status]))


def allowed_sandbox_actions(
    status: ManagedOperationStatus,
) -> tuple[SandboxOperationAction, ...]:
    """Return commands accepted by the state machine in this state."""
    return _ALLOWED_ACTIONS[status]


LifecycleErrorCode = Literal[
    "sandbox_disabled",
    "sandbox_not_configured",
    "operation_not_found",
    "operation_conflict",
    "operation_not_ready",
    "validation_required",
    "approval_required",
    "project_invalid",
    "provider_error",
    "publisher_error",
    "publisher_unavailable",
    "operation_failed",
    "no_changes",
    "artifact_unavailable",
    "artifact_file_not_found",
]

_ERROR_MESSAGES: dict[LifecycleErrorCode, str] = {
    "sandbox_disabled": "Managed coding Sandbox is disabled.",
    "sandbox_not_configured": "Managed coding Sandbox is not configured.",
    "operation_not_found": "Managed Sandbox operation was not found.",
    "operation_conflict": "Another managed Sandbox operation is already active.",
    "operation_not_ready": "Managed Sandbox operation is not ready for this action.",
    "validation_required": "Current Sandbox changes require successful validation.",
    "approval_required": "The frozen Sandbox diff requires explicit approval.",
    "project_invalid": "The managed local project is invalid.",
    "provider_error": "The Sandbox provider operation failed.",
    "publisher_error": "The local Publisher operation failed.",
    "publisher_unavailable": "Workspace publishing is not available for this operation.",
    "operation_failed": "Managed Sandbox operation failed.",
    "no_changes": "A managed Sandbox artifact must contain at least one file change.",
    "artifact_unavailable": "The frozen Sandbox artifact is unavailable.",
    "artifact_file_not_found": "The frozen Sandbox file was not found.",
}


class SandboxLifecycleError(Exception):
    """Safe lifecycle failure with a stable error code."""

    def __init__(
        self,
        code: LifecycleErrorCode,
        *,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(_ERROR_MESSAGES[code])
        self.code = code
        self.operation_id = operation_id


class ManagedSandboxOperationRecord(BaseModel):
    """Persisted, secret-free status projection for one operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-managed-sandbox-operation/v1"] = MANAGED_OPERATION_SCHEMA
    operation_id: str = Field(pattern=r"^sandbox-[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=128)
    status: ManagedOperationStatus
    config_revision: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
    workspace_revision: int = Field(default=0, ge=0)
    baseline_archive_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    baseline_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    baseline_workspace_revision: int | None = Field(default=None, ge=0)
    baseline_workspace_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    publish_available: bool = True
    validation: SandboxValidationEvidence | None = None
    diff: SandboxDiffResult | None = None
    artifact_id: str | None = Field(
        default=None,
        pattern=r"^artifact-[0-9a-f]{32}$",
    )
    artifact_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    publish_transaction_id: str | None = Field(
        default=None,
        pattern=r"^publish-[0-9a-f]{32}$",
    )
    published_workspace_revision: int | None = Field(default=None, ge=0)
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_times(self) -> ManagedSandboxOperationRecord:
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("operation update time precedes creation")
        if self.status in {"awaiting_approval", "publish_conflict"} and self.artifact_id is None:
            raise ValueError("reviewable state requires a frozen artifact")
        if self.status == "published" and self.publish_transaction_id is None:
            raise ValueError("published state requires a Publisher transaction")
        if (self.baseline_workspace_revision is None) != (self.baseline_workspace_sha256 is None):
            raise ValueError("Workspace baseline revision and SHA must be recorded together")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def cancellable(self) -> bool:
        return "cancel" in allowed_sandbox_actions(self.status)

    @property
    def allowed_actions(self) -> tuple[SandboxOperationAction, ...]:
        actions = allowed_sandbox_actions(self.status)
        if self.publish_available:
            return actions
        return tuple(action for action in actions if action not in {"publish", "retry_publish"})

    @property
    def allowed_transitions(self) -> tuple[ManagedOperationStatus, ...]:
        transitions = allowed_sandbox_transitions(self.status)
        if self.publish_available:
            return transitions
        return tuple(status for status in transitions if status != "publishing")


class ManagedSandboxEvent(BaseModel):
    """Durable operation-local event used for refresh replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-managed-sandbox-event/v1"] = MANAGED_EVENT_SCHEMA
    operation_id: str = Field(pattern=r"^sandbox-[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=128)
    recorded_at_ms: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ManagedSandboxEventPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ManagedSandboxEvent, ...]
    first_available_sequence: int | None = Field(default=None, ge=1)
    last_available_sequence: int | None = Field(default=None, ge=1)
    has_more: bool = False
    gap: bool = False


class SandboxOperationStoreError(Exception):
    """Safe persistence failure for managed operation state."""


class SandboxOperationStoreConflictError(SandboxOperationStoreError):
    """The persisted operation no longer matches the caller's old record."""


class SQLiteSandboxOperationStore:
    """SQLite operation records plus bounded per-operation event streams."""

    def __init__(
        self,
        connection: aiosqlite.Connection,
        *,
        owns_connection: bool,
        now_ms: Callable[[], int],
    ) -> None:
        self._connection = connection
        self._owns_connection = owns_connection
        self._now_ms = now_ms
        self._write_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls,
        database_path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> SQLiteSandboxOperationStore:
        path = Path(database_path)
        if not path.is_absolute() or str(path) == ":memory:":
            raise SandboxOperationStoreError(
                "managed Sandbox operations require an absolute SQLite path"
            )
        connection = await aiosqlite.connect(path, isolation_level=None)
        try:
            store = cls(
                connection,
                owns_connection=True,
                now_ms=now_ms or (lambda: int(time.time() * 1000)),
            )
            await store._initialize()
            return store
        except BaseException:
            await connection.close()
            raise

    async def create(
        self,
        record: ManagedSandboxOperationRecord,
    ) -> ManagedSandboxOperationRecord:
        self._ensure_open()
        payload = record.model_dump_json()
        _require_payload_size(payload, _MAX_RECORD_BYTES)
        try:
            await self._connection.execute(
                "INSERT INTO web_coding_sandbox_operations "
                "(operation_id, session_id, status, record_json, created_at_ms, "
                "updated_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.operation_id,
                    record.session_id,
                    record.status,
                    payload,
                    record.created_at_ms,
                    record.updated_at_ms,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise SandboxOperationStoreError("managed Sandbox operation already exists") from exc
        return record

    async def compare_and_swap(
        self,
        expected: ManagedSandboxOperationRecord,
        updated: ManagedSandboxOperationRecord,
    ) -> ManagedSandboxOperationRecord:
        """Replace exactly one previously read record or reject a stale writer."""
        self._ensure_open()
        if expected.operation_id != updated.operation_id:
            raise SandboxOperationStoreError("managed Sandbox operation identity changed")
        expected_payload = expected.model_dump_json()
        updated_payload = updated.model_dump_json()
        _require_payload_size(updated_payload, _MAX_RECORD_BYTES)
        async with self._write_lock:
            cursor = await self._connection.execute(
                "UPDATE web_coding_sandbox_operations SET status = ?, record_json = ?, "
                "updated_at_ms = ? WHERE operation_id = ? AND record_json = ?",
                (
                    updated.status,
                    updated_payload,
                    updated.updated_at_ms,
                    updated.operation_id,
                    expected_payload,
                ),
            )
            changed = cursor.rowcount
            await cursor.close()
        if changed != 1:
            raise SandboxOperationStoreConflictError(
                "managed Sandbox operation changed concurrently"
            )
        return updated

    async def get(self, operation_id: str) -> ManagedSandboxOperationRecord | None:
        self._ensure_open()
        cursor = await self._connection.execute(
            "SELECT record_json FROM web_coding_sandbox_operations WHERE operation_id = ?",
            (operation_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _decode_record(row[0])

    async def latest_for_session(
        self,
        session_id: str,
    ) -> ManagedSandboxOperationRecord | None:
        self._ensure_open()
        cursor = await self._connection.execute(
            "SELECT record_json FROM web_coding_sandbox_operations "
            "WHERE session_id = ? ORDER BY created_at_ms DESC, operation_id DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _decode_record(row[0])

    async def active_records(self) -> tuple[ManagedSandboxOperationRecord, ...]:
        self._ensure_open()
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        cursor = await self._connection.execute(
            "SELECT record_json FROM web_coding_sandbox_operations "
            f"WHERE status NOT IN ({placeholders}) ORDER BY created_at_ms",
            tuple(sorted(_TERMINAL_STATUSES)),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(_decode_record(row[0]) for row in rows)

    async def append_event(
        self,
        operation_id: str,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> ManagedSandboxEvent:
        self._ensure_open()
        async with self._write_lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM "
                    "web_coding_sandbox_operation_events WHERE operation_id = ?",
                    (operation_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                sequence = int(row[0]) + 1 if row is not None else 1
                event = ManagedSandboxEvent(
                    operation_id=operation_id,
                    session_id=session_id,
                    sequence=sequence,
                    event_type=event_type,
                    recorded_at_ms=self._now_ms(),
                    payload=payload,
                )
                serialized = event.model_dump_json()
                _require_payload_size(serialized, _MAX_EVENT_BYTES)
                await self._connection.execute(
                    "INSERT INTO web_coding_sandbox_operation_events "
                    "(operation_id, sequence, event_json) VALUES (?, ?, ?)",
                    (operation_id, sequence, serialized),
                )
                cutoff = sequence - _MAX_EVENTS_PER_OPERATION
                if cutoff > 0:
                    await self._connection.execute(
                        "DELETE FROM web_coding_sandbox_operation_events "
                        "WHERE operation_id = ? AND sequence <= ?",
                        (operation_id, cutoff),
                    )
                await self._connection.execute("COMMIT")
            except BaseException:
                await self._connection.execute("ROLLBACK")
                raise
        return event

    async def list_events(
        self,
        operation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> ManagedSandboxEventPage:
        self._ensure_open()
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise SandboxOperationStoreError("invalid managed event query")
        cursor = await self._connection.execute(
            "SELECT MIN(sequence), MAX(sequence) FROM "
            "web_coding_sandbox_operation_events WHERE operation_id = ?",
            (operation_id,),
        )
        bounds = await cursor.fetchone()
        await cursor.close()
        first = None if bounds is None or bounds[0] is None else int(bounds[0])
        last = None if bounds is None or bounds[1] is None else int(bounds[1])
        cursor = await self._connection.execute(
            "SELECT event_json FROM web_coding_sandbox_operation_events "
            "WHERE operation_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
            (operation_id, after_sequence, limit + 1),
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
        has_more = len(rows) > limit
        selected = rows[:limit]
        try:
            events = tuple(ManagedSandboxEvent.model_validate_json(str(row[0])) for row in selected)
        except ValidationError as exc:
            raise SandboxOperationStoreError("managed Sandbox event is corrupt") from exc
        return ManagedSandboxEventPage(
            events=events,
            first_available_sequence=first,
            last_available_sequence=last,
            has_more=has_more,
            gap=first is not None and after_sequence > 0 and after_sequence < first - 1,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            await self._connection.close()

    async def _initialize(self) -> None:
        await self._connection.execute("PRAGMA busy_timeout=5000")
        await self._connection.execute(
            "CREATE TABLE IF NOT EXISTS web_coding_sandbox_operation_schema "
            "(key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        cursor = await self._connection.execute(
            "SELECT value FROM web_coding_sandbox_operation_schema WHERE key = 'version'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        existing_version = None if row is None else int(row[0])
        if existing_version not in {None, 1, _STORE_SCHEMA_VERSION}:
            raise SandboxOperationStoreError("unsupported managed Sandbox operation schema")
        await self._connection.execute(
            "CREATE TABLE IF NOT EXISTS web_coding_sandbox_operations ("
            "operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "status TEXT NOT NULL, record_json TEXT NOT NULL, "
            "created_at_ms INTEGER NOT NULL, "
            "updated_at_ms INTEGER NOT NULL)"
        )
        if existing_version == 1:
            cursor = await self._connection.execute(
                "PRAGMA table_info(web_coding_sandbox_operations)"
            )
            columns = {str(info[1]) for info in await cursor.fetchall()}
            await cursor.close()
            if "created_at_ms" not in columns:
                await self._connection.execute(
                    "ALTER TABLE web_coding_sandbox_operations "
                    "ADD COLUMN created_at_ms INTEGER NOT NULL DEFAULT 0"
                )
        await self._connection.execute(
            "INSERT INTO web_coding_sandbox_operation_schema (key, value) "
            "VALUES ('version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_STORE_SCHEMA_VERSION,),
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_coding_sandbox_session "
            "ON web_coding_sandbox_operations(session_id, updated_at_ms DESC)"
        )
        await self._connection.execute(
            "CREATE TABLE IF NOT EXISTS web_coding_sandbox_operation_events ("
            "operation_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "event_json TEXT NOT NULL, PRIMARY KEY(operation_id, sequence), "
            "FOREIGN KEY(operation_id) REFERENCES web_coding_sandbox_operations"
            "(operation_id) ON DELETE CASCADE)"
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxOperationStoreError("managed Sandbox store is closed")


SandboxBackendResolver = Callable[[SandboxAdminConfig], Awaitable[SandboxBackend]]
SandboxConfigProvider = Callable[[], Awaitable[SandboxConfigRecord]]
SandboxSessionExists = Callable[[str], Awaitable[bool]]
SandboxLifecycleEventSink = Callable[[ManagedSandboxEvent], Awaitable[None]]


class SandboxBaseline(BaseModel):
    """Provider-neutral snapshot plus its product source provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: ProjectSnapshot
    source_workspace_revision: int | None = Field(default=None, ge=0)
    source_workspace_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    publish_root: Path | None = None

    @model_validator(mode="after")
    def _validate_workspace_provenance(self) -> SandboxBaseline:
        if (self.source_workspace_revision is None) != (self.source_workspace_sha256 is None):
            raise ValueError("Workspace baseline revision and SHA must be paired")
        return self


SandboxBaselineProvider = Callable[
    [str, Path, SnapshotPolicy, bytes],
    Awaitable[SandboxBaseline],
]


class SandboxArtifactPublisher(Protocol):
    """Provider-neutral target for one approved, immutable output artifact."""

    async def publish(
        self,
        artifact: SandboxOutputArtifact,
        *,
        baseline: ProjectSnapshot,
        signer: ArtifactSigner,
        session_id: str,
        expected_workspace_revision: int,
        expected_workspace_sha256: str,
    ) -> PublisherResult: ...


@dataclass
class _LiveOperation:
    operation: SandboxOperation
    snapshot: ProjectSnapshot
    artifact_signer: ArtifactSigner
    publish_root: Path | None
    cancel_event: asyncio.Event
    close_runtime: Callable[[], Awaitable[None]] | None = None
    execution_released: bool = False


@dataclass(frozen=True)
class FrozenSandboxFile:
    """One exact file read from the signed artifact awaiting approval."""

    path: str
    name: str
    content: bytes
    size: int
    sha256: str
    binary: bool


class ManagedSandboxLifecycle:
    """Coordinates one live cloud Sandbox per local application session."""

    def __init__(
        self,
        *,
        store: SQLiteSandboxOperationStore,
        config_provider: SandboxConfigProvider,
        backend_resolver: SandboxBackendResolver,
        artifact_signer: ArtifactSigner,
        session_exists: SandboxSessionExists,
        projects_root: Path | None,
        state_root: Path,
        staging_root: Path,
        baseline_provider: SandboxBaselineProvider | None = None,
        artifact_publisher: SandboxArtifactPublisher | None = None,
        event_sink: SandboxLifecycleEventSink | None = None,
        now_ms: Callable[[], int] | None = None,
        require_execution_approval: bool = False,
    ) -> None:
        self._store = store
        self._config_provider = config_provider
        self._backend_resolver = backend_resolver
        self._artifact_signer = artifact_signer
        self._session_exists = session_exists
        if projects_root is None and baseline_provider is None:
            raise ValueError("managed Sandbox lifecycle requires a baseline provider")
        self._projects_root = None if projects_root is None else _absolute_root(projects_root)
        self._state_root = _absolute_root(state_root)
        self._staging_root = _absolute_root(staging_root)
        self._baseline_provider = baseline_provider
        self._artifact_publisher = artifact_publisher
        self._event_sink = event_sink
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._live: dict[str, _LiveOperation] = {}
        self._session_operation: dict[str, str] = {}
        self._actions: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._require_execution_approval = require_execution_approval

    async def recover_startup(self) -> tuple[str, ...]:
        """Mark old live records interrupted; never replay model or commands."""
        interrupted: list[str] = []
        for record in await self._store.active_records():
            updated = await self._update(
                record,
                status="interrupted",
                error_code="server_restarted",
            )
            await self._emit(updated, "sandbox_operation_interrupted", {})
            interrupted.append(record.operation_id)
        return tuple(interrupted)

    async def start(self, session_id: str) -> ManagedSandboxOperationRecord:
        if self._require_execution_approval:
            raise SandboxLifecycleError("approval_required")
        if self._closing:
            raise SandboxLifecycleError("operation_conflict")
        if not await self._session_exists(session_id):
            raise SandboxLifecycleError("operation_not_found")
        config_record = await self._config_provider()
        if not config_record.config.enabled:
            raise SandboxLifecycleError("sandbox_disabled")
        if config_record.config.credential_id is None:
            raise SandboxLifecycleError("sandbox_not_configured")
        async with self._lock:
            existing_id = self._session_operation.get(session_id)
            if existing_id is not None:
                existing = await self._record(existing_id)
                if not existing.terminal:
                    raise SandboxLifecycleError(
                        "operation_conflict",
                        operation_id=existing.operation_id,
                    )
            if len(self._session_operation) >= config_record.config.max_concurrent_per_user:
                raise SandboxLifecycleError("operation_conflict")
            operation_id = f"sandbox-{uuid4().hex}"
            now = self._now_ms()
            record = ManagedSandboxOperationRecord(
                operation_id=operation_id,
                session_id=session_id,
                status="creating",
                config_revision=config_record.revision,
                created_at_ms=now,
                updated_at_ms=now,
            )
            await self._store.create(record)
            self._session_operation[session_id] = operation_id
            await self._emit(record, "sandbox_operation_creating", {})
            task = asyncio.create_task(
                self._create_operation(record, config_record.config),
                name=f"managed_sandbox_create_{operation_id}",
            )
            self._actions[operation_id] = task
            task.add_done_callback(partial(self._action_done, operation_id))
        return record

    async def adopt_approved_operation(
        self,
        *,
        session_id: str,
        operation: SandboxOperation,
        baseline: SandboxBaseline,
        config_revision: int,
        close_runtime: Callable[[], Awaitable[None]],
        publish_available: bool,
    ) -> ManagedSandboxOperationRecord:
        """Trusted task-runtime handoff; never creates or refreshes a runtime.

        The caller owns the execution grant. This lifecycle retains the signed
        artifact after execution ends, so existing review/publication stays separate.
        """
        if self._closing or not await self._session_exists(session_id):
            raise SandboxLifecycleError("operation_conflict")
        async with self._lock:
            previous = await self._store.latest_for_session(session_id)
            if previous is not None and not previous.terminal:
                raise SandboxLifecycleError("operation_conflict")
            now = self._now_ms()
            record = ManagedSandboxOperationRecord(
                operation_id=operation.operation_id,
                session_id=session_id,
                status="ready",
                config_revision=config_revision,
                created_at_ms=now,
                updated_at_ms=now,
                baseline_archive_sha256=baseline.snapshot.archive_sha256,
                baseline_manifest_sha256=baseline.snapshot.manifest.manifest_sha256,
                baseline_workspace_revision=baseline.source_workspace_revision,
                baseline_workspace_sha256=baseline.source_workspace_sha256,
                publish_available=publish_available and self._artifact_publisher is not None,
            )
            await self._store.create(record)
            self._live[record.operation_id] = _LiveOperation(
                operation, baseline.snapshot, self._artifact_signer, baseline.publish_root,
                asyncio.Event(), close_runtime=close_runtime,
            )
            self._session_operation[session_id] = record.operation_id
        await self._emit(record, "sandbox_operation_ready", {})
        return record

    async def release_execution(self, operation_id: str) -> None:
        """Close compute but retain the already frozen artifact for explicit review."""
        record = await self.get(operation_id)
        live = self._live.get(operation_id)
        if live is None or live.execution_released:
            return
        # The user may already have approved publication when the request's
        # finally block runs. Releasing compute must not cancel publication or
        # strand a retained conflict diff behind a now-closed execution grant.
        if record.status not in {
            "awaiting_approval", "publishing", "publish_conflict", "published",
        } or live.operation.output_artifact is None:
            if "cancel" in record.allowed_actions:
                await self.cancel(operation_id)
            return
        if live.close_runtime is not None:
            await live.close_runtime()
        else:
            await live.operation.close()
        live.execution_released = True

    async def get(self, operation_id: str) -> ManagedSandboxOperationRecord:
        record = await self._record(operation_id)
        live = self._live.get(operation_id)
        if live is not None and live.operation.workspace_revision != record.workspace_revision:
            validation = live.operation.last_validation_evidence
            status = record.status
            if status in {"validated", "validation_failed"} and validation is None:
                status = "ready"
            try:
                record = await self._update(
                    record,
                    status=status,
                    workspace_revision=live.operation.workspace_revision,
                    validation=validation,
                    diff=None,
                    artifact_id=None,
                    artifact_sha256=None,
                    changed_paths=(),
                    deleted_paths=(),
                )
            except SandboxLifecycleError as exc:
                if exc.code != "operation_conflict":
                    raise
                record = await self._record(operation_id)
        return record

    async def latest_for_session(
        self,
        session_id: str,
    ) -> ManagedSandboxOperationRecord | None:
        record = await self._store.latest_for_session(session_id)
        if record is None:
            return None
        return await self.get(record.operation_id)

    async def read_frozen_file(
        self,
        operation_id: str,
        logical_path: str,
    ) -> FrozenSandboxFile:
        """Read one signed, immutable changed file without publishing it."""
        record = await self.get(operation_id)
        if record.status not in {"awaiting_approval", "publish_conflict"}:
            raise SandboxLifecycleError(
                "artifact_unavailable",
                operation_id=operation_id,
            )
        live = self._live.get(operation_id)
        artifact = None if live is None else live.operation.output_artifact
        if (
            live is None
            or artifact is None
            or record.artifact_id != artifact.manifest.artifact_id
            or record.artifact_sha256 != artifact.archive_sha256
        ):
            raise SandboxLifecycleError(
                "artifact_unavailable",
                operation_id=operation_id,
            )
        try:
            normalized = validate_workspace_relative_path(logical_path)
            return await asyncio.to_thread(
                _read_frozen_artifact_file,
                artifact,
                live.artifact_signer,
                normalized,
            )
        except KeyError as exc:
            raise SandboxLifecycleError(
                "artifact_file_not_found",
                operation_id=operation_id,
            ) from exc
        except (OSError, tarfile.TarError, ValueError) as exc:
            raise SandboxLifecycleError(
                "artifact_unavailable",
                operation_id=operation_id,
            ) from exc

    def workspace_for_session(self, session_id: str | None) -> CodingWorkspace:
        if session_id is None:
            raise SandboxWorkspaceError("no_active_operation")
        operation_id = self._session_operation.get(session_id)
        if operation_id is None:
            raise SandboxWorkspaceError("no_active_operation")
        live = self._live.get(operation_id)
        if live is None:
            raise SandboxWorkspaceError("no_active_operation")
        return live.operation

    async def validate(self, operation_id: str) -> ManagedSandboxOperationRecord:
        record = await self.get(operation_id)
        if "validate" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=operation_id,
            )
        return await self._launch_action(
            record,
            status="validating",
            event_type="sandbox_validation_started",
            action=self._run_validation,
        )

    async def prepare_publish(
        self,
        operation_id: str,
    ) -> ManagedSandboxOperationRecord:
        record = await self.get(operation_id)
        if "prepare_publish" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "validation_required",
                operation_id=operation_id,
            )
        diff = await self.diff(operation_id)
        if not diff.entries:
            raise SandboxLifecycleError(
                "no_changes",
                operation_id=operation_id,
            )
        # diff() refreshes the durable record. Re-read it so _launch_action's
        # optimistic update cannot use the pre-diff revision.
        record = await self.get(operation_id)
        if "prepare_publish" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "validation_required",
                operation_id=operation_id,
            )
        return await self._launch_action(
            record,
            status="freezing",
            event_type="sandbox_freeze_started",
            action=self._run_freeze,
        )

    async def publish(self, operation_id: str) -> ManagedSandboxOperationRecord:
        record = await self.get(operation_id)
        if not record.publish_available:
            raise SandboxLifecycleError(
                "publisher_unavailable",
                operation_id=operation_id,
            )
        if "publish" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "approval_required",
                operation_id=operation_id,
            )
        return await self._launch_action(
            record,
            status="publishing",
            event_type="sandbox_publish_started",
            action=self._run_publish,
        )

    async def refreeze(self, operation_id: str) -> ManagedSandboxOperationRecord:
        """Rebuild the signed artifact from the existing frozen Sandbox."""
        record = await self.get(operation_id)
        if "refreeze" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=operation_id,
            )
        return await self._launch_action(
            record,
            status="freezing",
            event_type="sandbox_refreeze_started",
            action=self._run_refreeze,
        )

    async def retry_publish(self, operation_id: str) -> ManagedSandboxOperationRecord:
        """Retry the exact retained artifact without invoking the coding Agent."""
        record = await self.get(operation_id)
        if not record.publish_available:
            raise SandboxLifecycleError(
                "publisher_unavailable",
                operation_id=operation_id,
            )
        if "retry_publish" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=operation_id,
            )
        return await self._launch_action(
            record,
            status="publishing",
            event_type="sandbox_publish_retry_started",
            action=self._run_publish,
        )

    async def diff(self, operation_id: str) -> SandboxDiffResult:
        record = await self.get(operation_id)
        live = self._live.get(operation_id)
        if live is None or live.execution_released:
            if record.diff is not None:
                return record.diff
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=operation_id,
            )
        result = await live.operation.diff()
        await self._update(
            record,
            workspace_revision=live.operation.workspace_revision,
            diff=result,
        )
        return result

    async def cancel(self, operation_id: str) -> ManagedSandboxOperationRecord:
        record = await self.get(operation_id)
        if "cancel" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=operation_id,
            )
        record = await self._update(record, status="cancelling", error_code=None)
        await self._emit(record, "sandbox_cancel_requested", {})
        live = self._live.get(operation_id)
        if live is not None:
            live.cancel_event.set()
        task = self._actions.get(operation_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_live(operation_id)
        record = await self._update(record, status="cancelled", error_code=None)
        await self._emit(record, "sandbox_operation_cancelled", {})
        self._release_session(record)
        return record

    async def discard(self, operation_id: str) -> ManagedSandboxOperationRecord:
        record = await self.get(operation_id)
        if record.status == "discarded":
            return record
        if "discard" not in record.allowed_actions:
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=operation_id,
            )
        record = await self._update(record, status="discarding", error_code=None)
        await self._emit(record, "sandbox_discard_started", {})
        task = self._actions.get(operation_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._close_live(operation_id)
        record = await self._update(record, status="discarded", error_code=None)
        await self._emit(record, "sandbox_operation_discarded", {})
        self._release_session(record)
        return record

    async def events(
        self,
        operation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> ManagedSandboxEventPage:
        await self._record(operation_id)
        return await self._store.list_events(
            operation_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def shutdown(self) -> None:
        self._closing = True
        for live in self._live.values():
            live.cancel_event.set()
        actions = tuple(self._actions.items())
        for operation_id, task in actions:
            if not task.done():
                record = await self._store.get(operation_id)
                if record is None or record.status != "publishing":
                    task.cancel()
        if actions:
            await asyncio.gather(
                *(task for _, task in actions),
                return_exceptions=True,
            )
        for operation_id in tuple(self._live):
            await self._close_live(operation_id)
        for record in await self._store.active_records():
            if record.status in {"publishing", "published"}:
                continue
            updated = await self._update(
                record,
                status="interrupted",
                error_code="server_shutdown",
            )
            await self._emit(updated, "sandbox_operation_interrupted", {})
        self._session_operation.clear()

    async def _create_operation(
        self,
        record: ManagedSandboxOperationRecord,
        config: SandboxAdminConfig,
    ) -> None:
        operation: SandboxOperation | None = None
        backend: SandboxBackend | None = None
        handle: SandboxHandle | None = None
        try:
            snapshot_path = self._staging_root / "snapshots" / (record.operation_id + ".tar.gz")
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            policy = SnapshotPolicy.from_limits(config.limits)
            if self._baseline_provider is None:
                project_root = await asyncio.to_thread(
                    self._ensure_project_root,
                    record.session_id,
                    config.limits.command_timeout_seconds,
                )
                snapshot = await build_project_snapshot(
                    project_root,
                    snapshot_path,
                    policy=policy,
                )
                baseline = SandboxBaseline(
                    snapshot=snapshot,
                    publish_root=project_root,
                )
            else:
                baseline = await self._baseline_provider(
                    record.session_id,
                    snapshot_path,
                    policy,
                    _default_validation_config(config.limits.command_timeout_seconds),
                )
                snapshot = baseline.snapshot
            validation_plan = load_sandbox_validation_plan(snapshot, policy=policy)
            backend = await self._backend_resolver(config)
            handle = await backend.create(
                SandboxCreateSpec(
                    operation_id=record.operation_id,
                    runtime_id=config.runtime_id,
                    workdir=config.workdir,
                    network=config.network,
                    limits=config.limits,
                )
            )
            baseline_entries = tuple(
                SandboxFileEntry(
                    path=entry.path,
                    size=entry.size,
                    sha256=entry.sha256,
                )
                for entry in snapshot.manifest.entries
            )

            async def baseline_reader(path: str) -> bytes | None:
                try:
                    return await asyncio.to_thread(read_snapshot_file, snapshot, path)
                except SnapshotError as exc:
                    raise SandboxWorkspaceError("protocol_error") from exc

            operation = SandboxOperation(
                backend=backend,
                handle=handle,
                workdir=config.workdir,
                limits=config.limits,
                staging_root=self._staging_root / "operation-files",
                baseline_entries=baseline_entries,
                baseline_reader=baseline_reader,
                validation_plan=validation_plan,
                artifact_signer=self._artifact_signer,
                snapshot_policy=policy,
            )
            await operation.seed_from_snapshot(snapshot)
            self._live[record.operation_id] = _LiveOperation(
                operation=operation,
                snapshot=snapshot,
                artifact_signer=self._artifact_signer,
                publish_root=baseline.publish_root,
                cancel_event=asyncio.Event(),
            )
            self._detach_current_action(record.operation_id)
            updated = await self._update(
                record,
                status="ready",
                baseline_archive_sha256=snapshot.archive_sha256,
                baseline_manifest_sha256=snapshot.manifest.manifest_sha256,
                baseline_workspace_revision=baseline.source_workspace_revision,
                baseline_workspace_sha256=baseline.source_workspace_sha256,
                publish_available=(
                    baseline.publish_root is not None
                    or (
                        self._artifact_publisher is not None
                        and baseline.source_workspace_revision is not None
                    )
                ),
                error_code=None,
            )
            await self._emit(updated, "sandbox_operation_ready", {})
        except asyncio.CancelledError:
            if operation is not None:
                await _close_quietly(operation)
            elif backend is not None and handle is not None:
                await _destroy_quietly(backend, handle)
            raise
        except (SandboxValidationConfigError, SnapshotError) as exc:
            if operation is not None:
                await _close_quietly(operation)
            elif backend is not None and handle is not None:
                await _destroy_quietly(backend, handle)
            await self._fail(record, "project_invalid", type(exc).__name__)
        except SandboxLifecycleError as exc:
            if operation is not None:
                await _close_quietly(operation)
                self._live.pop(record.operation_id, None)
            elif backend is not None and handle is not None:
                await _destroy_quietly(backend, handle)
            if exc.code != "operation_conflict":
                await self._fail(record, "operation_failed", type(exc).__name__)
        except Exception as exc:
            if operation is not None:
                await _close_quietly(operation)
            elif backend is not None and handle is not None:
                await _destroy_quietly(backend, handle)
            await self._fail(record, "provider_error", type(exc).__name__)

    async def _run_validation(
        self,
        record: ManagedSandboxOperationRecord,
        live: _LiveOperation,
    ) -> None:
        async def output(chunk: SandboxOutputChunk) -> None:
            await self._emit(
                record,
                "sandbox_validation_output",
                chunk.model_dump(mode="json"),
            )

        try:
            evidence = await live.operation.validate_required_checks(
                on_output=output,
                signal=live.cancel_event,
            )
            diff = await live.operation.diff()
            status: ManagedOperationStatus = "validated" if evidence.passed else "validation_failed"
            self._detach_current_action(record.operation_id)
            updated = await self._update(
                record,
                status=status,
                workspace_revision=live.operation.workspace_revision,
                validation=evidence,
                diff=diff,
                error_code=None if evidence.passed else evidence.failure_code,
            )
            await self._emit(
                updated,
                "sandbox_validation_finished",
                {"passed": evidence.passed, "failure_code": evidence.failure_code},
            )
        except asyncio.CancelledError:
            raise
        except SandboxLifecycleError as exc:
            if exc.code != "operation_conflict":
                await self._fail(record, "operation_failed", type(exc).__name__)
        except SandboxWorkspaceError as exc:
            await self._fail(record, exc.code, type(exc).__name__)
        except Exception as exc:
            await self._fail(record, "operation_failed", type(exc).__name__)

    async def _run_freeze(
        self,
        record: ManagedSandboxOperationRecord,
        live: _LiveOperation,
    ) -> None:
        try:
            artifact = await live.operation.freeze_output_artifact()
            diff = await live.operation.diff()
            self._detach_current_action(record.operation_id)
            updated = await self._update(
                record,
                status="awaiting_approval",
                workspace_revision=live.operation.workspace_revision,
                validation=live.operation.last_validation_evidence,
                diff=diff,
                artifact_id=artifact.manifest.artifact_id,
                artifact_sha256=artifact.archive_sha256,
                changed_paths=tuple(entry.path for entry in artifact.manifest.changed_files),
                deleted_paths=tuple(entry.path for entry in artifact.manifest.deleted_files),
                error_code=None,
            )
            await self._emit(
                updated,
                "sandbox_approval_required",
                {
                    "changed_paths": list(updated.changed_paths),
                    "deleted_paths": list(updated.deleted_paths),
                },
            )
        except SandboxWorkspaceError as exc:
            if exc.code == "validation_stale":
                current = await self._record(record.operation_id)
                if current.status != "freezing":
                    return
                self._detach_current_action(record.operation_id)
                updated = await self._update(
                    current,
                    status="validation_failed",
                    validation=None,
                    artifact_id=None,
                    artifact_sha256=None,
                    error_code="validation_required",
                )
                await self._emit(
                    updated,
                    "sandbox_freeze_rejected",
                    {"error_code": "validation_required"},
                )
            else:
                error_code = (
                    "artifact_stale" if exc.code == "artifact_stale" else "operation_failed"
                )
                await self._fail(record, error_code, type(exc).__name__)
        except SandboxLifecycleError as exc:
            if exc.code != "operation_conflict":
                await self._fail(record, "operation_failed", type(exc).__name__)
        except Exception as exc:
            await self._fail(record, "operation_failed", type(exc).__name__)

    async def _run_publish(
        self,
        record: ManagedSandboxOperationRecord,
        live: _LiveOperation,
    ) -> None:
        artifact = live.operation.output_artifact
        if artifact is None:
            await self._fail(record, "approval_required", "MissingArtifact")
            return
        if live.publish_root is None and self._artifact_publisher is None:
            await self._fail(record, "publisher_unavailable", "MissingPublisher")
            return
        try:
            if live.publish_root is not None:
                publisher = LocalTransactionalPublisher(
                    project_root=live.publish_root,
                    state_root=self._state_root,
                )
                result = await publisher.publish(
                    artifact,
                    baseline=live.snapshot,
                    signer=live.artifact_signer,
                )
            else:
                if (
                    self._artifact_publisher is None
                    or record.baseline_workspace_revision is None
                    or record.baseline_workspace_sha256 is None
                ):
                    raise PublisherError("baseline_invalid")
                result = await self._artifact_publisher.publish(
                    artifact,
                    baseline=live.snapshot,
                    signer=live.artifact_signer,
                    session_id=record.session_id,
                    expected_workspace_revision=record.baseline_workspace_revision,
                    expected_workspace_sha256=record.baseline_workspace_sha256,
                )
            self._detach_current_action(record.operation_id)
            updated = await self._update(
                record,
                status="published",
                publish_transaction_id=result.transaction_id,
                published_workspace_revision=result.workspace_revision,
                changed_paths=result.changed_paths,
                deleted_paths=result.deleted_paths,
                error_code=None,
            )
            await self._emit(
                updated,
                "sandbox_publish_finished",
                {
                    "publish_status": result.status,
                    "workspace_revision": result.workspace_revision,
                    "changed_paths": list(result.changed_paths),
                    "deleted_paths": list(result.deleted_paths),
                },
            )
            await self._close_live(record.operation_id)
            self._release_session(updated)
        except PublisherError as exc:
            if exc.code == "publish_conflict":
                await self._retain_publish_conflict(record)
            else:
                await self._fail(record, exc.code, type(exc).__name__)
        except SandboxLifecycleError as exc:
            if exc.code != "operation_conflict":
                await self._fail(record, "operation_failed", type(exc).__name__)
        except Exception as exc:
            await self._fail(record, "publisher_error", type(exc).__name__)

    async def _run_refreeze(
        self,
        record: ManagedSandboxOperationRecord,
        live: _LiveOperation,
    ) -> None:
        try:
            artifact = await live.operation.refreeze_output_artifact()
            diff = await live.operation.diff()
            self._detach_current_action(record.operation_id)
            updated = await self._update(
                record,
                status="awaiting_approval",
                workspace_revision=live.operation.workspace_revision,
                validation=live.operation.last_validation_evidence,
                diff=diff,
                artifact_id=artifact.manifest.artifact_id,
                artifact_sha256=artifact.archive_sha256,
                changed_paths=tuple(entry.path for entry in artifact.manifest.changed_files),
                deleted_paths=tuple(entry.path for entry in artifact.manifest.deleted_files),
                error_code=None,
            )
            await self._emit(
                updated,
                "sandbox_approval_required",
                {
                    "refrozen": True,
                    "changed_paths": list(updated.changed_paths),
                    "deleted_paths": list(updated.deleted_paths),
                },
            )
        except (SandboxWorkspaceError, SandboxLifecycleError) as exc:
            await self._retain_publish_conflict(
                record,
                error_code=getattr(exc, "code", "artifact_export_failed"),
                event_type="sandbox_refreeze_failed",
            )
        except Exception:
            await self._retain_publish_conflict(
                record,
                error_code="artifact_export_failed",
                event_type="sandbox_refreeze_failed",
            )

    async def _retain_publish_conflict(
        self,
        record: ManagedSandboxOperationRecord,
        *,
        error_code: str = "publish_conflict",
        event_type: str = "sandbox_publish_conflict",
    ) -> None:
        """Keep the live Sandbox and signed artifact available for recovery."""
        self._detach_current_action(record.operation_id)
        current = await self._store.get(record.operation_id) or record
        if current.status not in {"publishing", "freezing"}:
            return
        try:
            updated = await self._update(
                current,
                status="publish_conflict",
                error_code=error_code[:128],
            )
        except SandboxLifecycleError as exc:
            if exc.code == "operation_conflict":
                return
            raise
        await self._emit(
            updated,
            event_type,
            {"error_code": updated.error_code},
        )

    async def _launch_action(
        self,
        record: ManagedSandboxOperationRecord,
        *,
        status: ManagedOperationStatus,
        event_type: str,
        action: Callable[[ManagedSandboxOperationRecord, _LiveOperation], Awaitable[None]],
    ) -> ManagedSandboxOperationRecord:
        live = self._live.get(record.operation_id)
        if live is None:
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=record.operation_id,
            )
        current_task = self._actions.get(record.operation_id)
        if current_task is not None and not current_task.done():
            raise SandboxLifecycleError(
                "operation_conflict",
                operation_id=record.operation_id,
            )
        live.cancel_event = asyncio.Event()
        updated = await self._update(record, status=status, error_code=None)
        start_gate = asyncio.Event()

        async def run_action() -> None:
            await start_gate.wait()
            await action(updated, live)

        task: asyncio.Task[None] = asyncio.create_task(
            run_action(),
            name=f"managed_sandbox_{status}_{record.operation_id}",
        )
        self._actions[record.operation_id] = task
        task.add_done_callback(partial(self._action_done, record.operation_id))
        try:
            await self._emit(updated, event_type, {})
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        start_gate.set()
        return updated

    async def _update(
        self,
        record: ManagedSandboxOperationRecord,
        **updates: Any,
    ) -> ManagedSandboxOperationRecord:
        updates["updated_at_ms"] = self._now_ms()
        updated = ManagedSandboxOperationRecord.model_validate(
            {**record.model_dump(mode="python"), **updates}
        )
        if (
            updated.status != record.status
            and updated.status not in _ALLOWED_STATUS_TRANSITIONS[record.status]
        ):
            raise SandboxLifecycleError(
                "operation_not_ready",
                operation_id=record.operation_id,
            )
        try:
            return await self._store.compare_and_swap(record, updated)
        except SandboxOperationStoreConflictError as exc:
            raise SandboxLifecycleError(
                "operation_conflict",
                operation_id=record.operation_id,
            ) from exc

    async def _fail(
        self,
        record: ManagedSandboxOperationRecord,
        error_code: str,
        error_type: str,
    ) -> None:
        self._detach_current_action(record.operation_id)
        current = await self._store.get(record.operation_id) or record
        if current.status in {
            "cancelling",
            "cancelled",
            "discarding",
            "discarded",
            "published",
        }:
            return
        if current.status in {"failed", "interrupted"}:
            await self._close_live(record.operation_id)
            self._release_session(current)
            return
        try:
            updated = await self._update(
                current,
                status="failed",
                error_code=error_code[:128],
            )
        except SandboxLifecycleError as exc:
            if exc.code == "operation_conflict":
                return
            raise
        await self._emit(
            updated,
            "sandbox_operation_failed",
            {"error_code": updated.error_code, "error_type": error_type[:128]},
        )
        await self._close_live(record.operation_id)
        self._release_session(updated)

    async def _emit(
        self,
        record: ManagedSandboxOperationRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> ManagedSandboxEvent:
        event_payload = {
            "status": record.status,
            "workspace_revision": record.workspace_revision,
            **payload,
        }
        event = await self._store.append_event(
            record.operation_id,
            record.session_id,
            event_type,
            event_payload,
        )
        if self._event_sink is not None:
            try:
                await self._event_sink(event)
            except Exception:
                pass
        return event

    async def _record(self, operation_id: str) -> ManagedSandboxOperationRecord:
        record = await self._store.get(operation_id)
        if record is None:
            raise SandboxLifecycleError(
                "operation_not_found",
                operation_id=operation_id,
            )
        return record

    async def _close_live(self, operation_id: str) -> None:
        live = self._live.pop(operation_id, None)
        if live is not None:
            if live.close_runtime is not None:
                await live.close_runtime()
            else:
                await _close_quietly(live.operation)

    def _release_session(self, record: ManagedSandboxOperationRecord) -> None:
        if self._session_operation.get(record.session_id) == record.operation_id:
            self._session_operation.pop(record.session_id, None)

    def _action_done(self, operation_id: str, completed: asyncio.Task[None]) -> None:
        current = self._actions.get(operation_id)
        if current is completed:
            self._actions.pop(operation_id, None)
        try:
            completed.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _detach_current_action(self, operation_id: str) -> None:
        current = asyncio.current_task()
        if current is not None and self._actions.get(operation_id) is current:
            self._actions.pop(operation_id, None)

    def _ensure_project_root(
        self,
        session_id: str,
        command_timeout_seconds: int,
    ) -> Path:
        if self._projects_root is None:
            raise SandboxLifecycleError("project_invalid")
        self._projects_root.mkdir(parents=True, exist_ok=True)
        _validate_directory(self._projects_root)
        project_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        project_root = self._projects_root / project_key
        created = False
        try:
            project_root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        _validate_directory(project_root)
        config_path = project_root / ".pi-agent" / "sandbox.toml"
        if not config_path.exists():
            has_content = any(project_root.iterdir())
            if has_content and not created:
                raise SandboxLifecycleError("project_invalid")
            config_path.parent.mkdir(mode=0o700)
            _validate_directory(config_path.parent)
            _write_new_file(
                config_path,
                _default_validation_config(command_timeout_seconds),
            )
        return project_root


def _decode_record(value: object) -> ManagedSandboxOperationRecord:
    try:
        return ManagedSandboxOperationRecord.model_validate_json(str(value))
    except ValidationError as exc:
        raise SandboxOperationStoreError("managed Sandbox operation record is corrupt") from exc


def _require_payload_size(value: str, maximum: int) -> None:
    if len(value.encode("utf-8")) > maximum:
        raise SandboxOperationStoreError("managed Sandbox payload is too large")


def _absolute_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("managed Sandbox roots must be absolute")
    return path.resolve(strict=False)


def _validate_directory(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    is_junction = getattr(path, "is_junction", None)
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or (is_junction is not None and is_junction())
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    ):
        raise SandboxLifecycleError("project_invalid")


def _write_new_file(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _default_validation_config(command_timeout_seconds: int) -> bytes:
    timeout = max(1, min(60, command_timeout_seconds))
    return DEFAULT_VALIDATION_CONFIG.replace(
        b"timeout_seconds = 60",
        f"timeout_seconds = {timeout}".encode(),
    )


def _read_frozen_artifact_file(
    artifact: SandboxOutputArtifact,
    signer: ArtifactSigner,
    logical_path: str,
) -> FrozenSandboxFile:
    """Revalidate the signed archive receipt, then read one manifest member."""
    size, digest = hash_regular_file(
        artifact.archive_path,
        max_bytes=max(artifact.archive_size, 1),
    )
    if (
        size != artifact.archive_size
        or digest != artifact.archive_sha256
        or not verify_artifact_signature(signer, artifact.signature)
    ):
        raise ValueError("artifact receipt is invalid")
    entry = next(
        (
            candidate
            for candidate in artifact.manifest.changed_files
            if candidate.path == logical_path
        ),
        None,
    )
    if entry is None:
        raise KeyError(logical_path)
    with tarfile.open(artifact.archive_path, mode="r:") as archive:
        try:
            member = archive.getmember(entry.member_path)
        except KeyError as exc:
            raise ValueError("artifact payload member is missing") from exc
        if (
            not member.isfile()
            or member.name != entry.member_path
            or member.size != entry.after_size
        ):
            raise ValueError("artifact payload member is invalid")
        stream = archive.extractfile(member)
        if stream is None:
            raise ValueError("artifact payload member cannot be read")
        content = stream.read(entry.after_size + 1)
    if (
        len(content) != entry.after_size
        or hashlib.sha256(content).hexdigest() != entry.after_sha256
    ):
        raise ValueError("artifact payload does not match its signed manifest")
    return FrozenSandboxFile(
        path=entry.path,
        name=Path(entry.path).name,
        content=content,
        size=entry.after_size,
        sha256=entry.after_sha256,
        binary=entry.after_binary,
    )


async def _close_quietly(operation: SandboxOperation) -> None:
    try:
        await operation.close()
    except Exception:
        pass


async def _destroy_quietly(
    backend: SandboxBackend,
    handle: SandboxHandle,
) -> None:
    try:
        await backend.destroy(handle)
    except Exception:
        pass


__all__ = [
    "DEFAULT_VALIDATION_CONFIG",
    "FrozenSandboxFile",
    "LifecycleErrorCode",
    "MANAGED_EVENT_SCHEMA",
    "MANAGED_OPERATION_SCHEMA",
    "SANDBOX_STATE_MACHINE_VERSION",
    "SandboxArtifactPublisher",
    "SandboxBaseline",
    "SandboxBaselineProvider",
    "ManagedOperationStatus",
    "ManagedSandboxEvent",
    "ManagedSandboxEventPage",
    "ManagedSandboxLifecycle",
    "ManagedSandboxOperationRecord",
    "SQLiteSandboxOperationStore",
    "SandboxBackendResolver",
    "SandboxLifecycleError",
    "SandboxLifecycleEventSink",
    "SandboxOperationAction",
    "SandboxOperationStoreConflictError",
    "SandboxOperationStoreError",
    "SandboxSessionExists",
    "allowed_sandbox_actions",
    "allowed_sandbox_transitions",
]
