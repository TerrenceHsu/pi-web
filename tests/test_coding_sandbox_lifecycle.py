"""Offline lifecycle, persistence, recovery and refresh contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from coding_sandbox import (
    HMACSHA256ArtifactSigner,
    ManagedSandboxLifecycle,
    ManagedSandboxOperationRecord,
    SandboxCommandResult,
    SandboxFileEntry,
    SandboxLifecycleError,
    SQLiteSandboxOperationStore,
)
from coding_sandbox.admin import SandboxAdminConfig, SandboxConfigRecord
from coding_sandbox.fake import FakeSandboxBackend
from coding_sandbox.lifecycle import DEFAULT_VALIDATION_CONFIG


def _helper(result: dict[str, object]) -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id="queued",
        stdout=json.dumps({"ok": True, "result": result}),
        started_at_ms=10,
        finished_at_ms=11,
    )


def _fingerprint(entries: tuple[SandboxFileEntry, ...]) -> dict[str, object]:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.path):
        digest.update(entry.path.encode())
        digest.update(b"\0")
        digest.update(str(entry.size).encode())
        digest.update(b"\0")
        digest.update(entry.sha256.encode())
        digest.update(b"\n")
    return {
        "file_count": len(entries),
        "total_bytes": sum(entry.size for entry in entries),
        "sha256": digest.hexdigest(),
    }


def _record(*, operation_id: str, status: str = "creating") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "session_id": "session-one",
        "status": status,
        "config_revision": 1,
        "created_at_ms": 100,
        "updated_at_ms": 100,
    }


def _workspace_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


async def _wait_for_status(
    lifecycle: ManagedSandboxLifecycle,
    operation_id: str,
    expected: str,
) -> ManagedSandboxOperationRecord:
    for _ in range(100):
        record = await lifecycle.get(operation_id)
        if record.status == expected:
            return record
        if record.terminal:
            await asyncio.sleep(0.05)
            page = await lifecycle.events(operation_id)
            detail = page.events[-1].payload if page.events else {}
            pytest.fail(f"operation became {record.status}: {record.error_code}; {detail}")
        await asyncio.sleep(0.01)
    pytest.fail(f"operation did not reach {expected}")


@pytest.mark.asyncio
async def test_operation_store_persists_records_and_bounded_replay(tmp_path: Path) -> None:
    database = tmp_path / "operations.db"
    store = await SQLiteSandboxOperationStore.open(database, now_ms=lambda: 200)
    record = ManagedSandboxOperationRecord.model_validate(
        _record(operation_id="sandbox-" + "a" * 32)
    )
    await store.create(record)
    event = await store.append_event(
        record.operation_id,
        record.session_id,
        "sandbox_operation_creating",
        {"status": "creating"},
    )
    await store.close()

    reopened = await SQLiteSandboxOperationStore.open(database, now_ms=lambda: 300)
    try:
        assert await reopened.latest_for_session(record.session_id) == record
        page = await reopened.list_events(record.operation_id)
        assert page.events == (event,)
        assert page.first_available_sequence == 1
        assert page.last_available_sequence == 1
        assert page.gap is False
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_startup_recovery_marks_state_without_replaying_work(tmp_path: Path) -> None:
    store = await SQLiteSandboxOperationStore.open(tmp_path / "recovery.db")
    record = ManagedSandboxOperationRecord.model_validate(
        _record(operation_id="sandbox-" + "b" * 32, status="validating")
    )
    await store.create(record)
    backend_calls = 0

    async def config_provider() -> SandboxConfigRecord:
        raise AssertionError("recovery must not resolve configuration")

    async def backend_resolver(_config: SandboxAdminConfig) -> FakeSandboxBackend:
        nonlocal backend_calls
        backend_calls += 1
        raise AssertionError("recovery must not create a Sandbox")

    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=config_provider,
        backend_resolver=backend_resolver,
        artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"k" * 32),
        session_exists=lambda _session_id: asyncio.sleep(0, result=True),
        projects_root=tmp_path / "projects",
        state_root=tmp_path / "publisher",
        staging_root=tmp_path / "staging",
    )
    try:
        assert await lifecycle.recover_startup() == (record.operation_id,)
        recovered = await lifecycle.get(record.operation_id)
        assert recovered.status == "interrupted"
        assert recovered.error_code == "server_restarted"
        assert backend_calls == 0
        page = await lifecycle.events(record.operation_id)
        assert page.events[-1].event_type == "sandbox_operation_interrupted"
    finally:
        await lifecycle.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_lifecycle_creates_seeds_validates_and_cancels(tmp_path: Path) -> None:
    config_data = DEFAULT_VALIDATION_CONFIG
    config_entry = SandboxFileEntry(
        path=".pi-agent/sandbox.toml",
        size=len(config_data),
        sha256=hashlib.sha256(config_data).hexdigest(),
    )
    baseline = (config_entry,)
    backend = FakeSandboxBackend(
        id_factory=lambda: "managed-lifecycle",
        command_results=(
            _helper(_fingerprint(())),
            _helper(
                {
                    "path": config_entry.path,
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                    "created": True,
                }
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(_fingerprint(baseline)),
            SandboxCommandResult(
                command_id="check",
                stdout="syntax ok\n",
                started_at_ms=20,
                finished_at_ms=25,
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(
                {
                    "root": ".",
                    "files": [config_entry.model_dump(mode="json")],
                    "truncated": False,
                }
            ),
        ),
    )
    config = SandboxAdminConfig(enabled=True, credential_id="credential-one")

    async def config_provider() -> SandboxConfigRecord:
        return SandboxConfigRecord(config=config, revision=3, updated_at_ms=1)

    async def backend_resolver(_config: SandboxAdminConfig) -> FakeSandboxBackend:
        return backend

    store = await SQLiteSandboxOperationStore.open(tmp_path / "lifecycle.db")
    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=config_provider,
        backend_resolver=backend_resolver,
        artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"k" * 32),
        session_exists=lambda _session_id: asyncio.sleep(0, result=True),
        projects_root=tmp_path / "projects",
        state_root=tmp_path / "publisher",
        staging_root=tmp_path / "staging",
    )
    try:
        creating = await lifecycle.start("session-one")
        ready = await _wait_for_status(lifecycle, creating.operation_id, "ready")
        assert ready.config_revision == 3
        assert lifecycle.workspace_for_session("session-one").workspace_revision == 0

        validating = await lifecycle.validate(ready.operation_id)
        assert validating.status == "validating"
        validated = await _wait_for_status(lifecycle, ready.operation_id, "validated")
        assert validated.validation is not None
        assert validated.validation.passed is True
        assert validated.diff is not None
        assert validated.diff.entries == ()

        cancelled = await lifecycle.cancel(ready.operation_id)
        assert cancelled.status == "cancelled"
        assert backend.destroyed_sandbox_ids == ["managed-lifecycle"]
        page = await lifecycle.events(ready.operation_id)
        assert [event.sequence for event in page.events] == list(range(1, len(page.events) + 1))
        assert page.events[-1].event_type == "sandbox_operation_cancelled"
    finally:
        await lifecycle.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_failed_validation_cannot_publish_or_mutate_local_project(
    tmp_path: Path,
) -> None:
    config_data = DEFAULT_VALIDATION_CONFIG
    config_entry = SandboxFileEntry(
        path=".pi-agent/sandbox.toml",
        size=len(config_data),
        sha256=hashlib.sha256(config_data).hexdigest(),
    )
    baseline = (config_entry,)
    backend = FakeSandboxBackend(
        id_factory=lambda: "managed-validation-failure",
        command_results=(
            _helper(_fingerprint(())),
            _helper(
                {
                    "path": config_entry.path,
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                    "created": True,
                }
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(_fingerprint(baseline)),
            SandboxCommandResult(
                command_id="failed-check",
                exit_code=1,
                stderr="syntax failure\n",
                started_at_ms=20,
                finished_at_ms=25,
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(
                {
                    "root": ".",
                    "files": [config_entry.model_dump(mode="json")],
                    "truncated": False,
                }
            ),
        ),
    )
    config = SandboxAdminConfig(enabled=True, credential_id="credential-one")

    async def config_provider() -> SandboxConfigRecord:
        return SandboxConfigRecord(config=config, revision=4, updated_at_ms=1)

    async def backend_resolver(_config: SandboxAdminConfig) -> FakeSandboxBackend:
        return backend

    projects_root = tmp_path / "projects"
    store = await SQLiteSandboxOperationStore.open(tmp_path / "validation-failure.db")
    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=config_provider,
        backend_resolver=backend_resolver,
        artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"k" * 32),
        session_exists=lambda _session_id: asyncio.sleep(0, result=True),
        projects_root=projects_root,
        state_root=tmp_path / "publisher",
        staging_root=tmp_path / "staging",
    )
    try:
        creating = await lifecycle.start("session-one")
        ready = await _wait_for_status(lifecycle, creating.operation_id, "ready")
        project_root = next(path for path in projects_root.iterdir() if path.is_dir())
        before = _workspace_bytes(project_root)

        await lifecycle.validate(ready.operation_id)
        failed = await _wait_for_status(
            lifecycle,
            ready.operation_id,
            "validation_failed",
        )
        assert failed.validation is not None
        assert failed.validation.passed is False
        assert failed.validation.failure_code == "check_failed"
        assert failed.validation.checks[0].stderr == "syntax failure\n"

        with pytest.raises(SandboxLifecycleError) as raised:
            await lifecycle.prepare_publish(ready.operation_id)
        assert raised.value.code == "validation_required"
        assert _workspace_bytes(project_root) == before

        discarded = await lifecycle.discard(ready.operation_id)
        assert discarded.status == "discarded"
        assert _workspace_bytes(project_root) == before
        assert backend.destroyed_sandbox_ids == ["managed-validation-failure"]
    finally:
        await lifecycle.shutdown()
        await store.close()
