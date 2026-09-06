"""Offline tests for request-scoped coding workspace operations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coding_sandbox import (
    FakeSandboxBackend,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxFileEntry,
    SandboxLimits,
    SandboxOperation,
    SandboxOperationManager,
    SandboxOutputChunk,
    SandboxWorkspaceError,
    apply_file_patch,
    get_current_coding_workspace,
    parse_unified_diff,
    validate_workspace_relative_path,
)


def _command_result(
    payload: dict[str, object] | None = None,
    *,
    exit_code: int = 0,
    stdout: str | None = None,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id="queued",
        exit_code=exit_code,
        stdout=stdout if stdout is not None else json.dumps(payload),
        started_at_ms=1,
        finished_at_ms=2,
    )


def _spec(*, operation_id: str = "workspace-test") -> SandboxCreateSpec:
    return SandboxCreateSpec(
        operation_id=operation_id,
        runtime_id="base",
        limits=SandboxLimits(
            lifetime_seconds=60,
            command_timeout_seconds=20,
            max_output_bytes=1024 * 1024,
        ),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "value",
    ("", ".", "../secret", "src/../secret", "/etc/passwd", "a\\b", "a//b", "x\x00y"),
)
def test_workspace_paths_reject_unsafe_or_unnormalized_values(value: str) -> None:
    with pytest.raises(SandboxWorkspaceError) as exc_info:
        validate_workspace_relative_path(value)
    assert exc_info.value.code == "unsafe_path"


def test_workspace_root_is_only_allowed_explicitly() -> None:
    assert validate_workspace_relative_path(".", allow_root=True) == "."
    assert validate_workspace_relative_path("src/main.py") == "src/main.py"


def test_unified_patch_supports_modify_add_delete_and_marker_content() -> None:
    patch = """--- a/existing.txt
+++ b/existing.txt
@@ -1,2 +1,2 @@
 alpha
-old
+--- replacement
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new
--- a/delete.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""
    existing, added, deleted = parse_unified_diff(patch)

    assert apply_file_patch("alpha\nold\n", existing) == "alpha\n--- replacement\n"
    assert apply_file_patch(None, added) == "new\n"
    assert apply_file_patch("gone\n", deleted) is None


def test_unified_patch_rejects_traversal_rename_duplicate_and_conflict() -> None:
    invalid = (
        "--- a/../secret\n+++ b/../secret\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/one\n+++ b/two\n@@ -1 +1 @@\n-a\n+b\n",
        (
            "--- a/one\n+++ b/one\n@@ -1 +1 @@\n-a\n+b\n"
            "--- a/one\n+++ b/one\n@@ -1 +1 @@\n-b\n+c\n"
        ),
    )
    for patch in invalid:
        with pytest.raises(SandboxWorkspaceError) as exc_info:
            parse_unified_diff(patch)
        assert exc_info.value.code in {"unsafe_path", "patch_invalid"}

    parsed = parse_unified_diff(
        "--- a/one\n+++ b/one\n@@ -1 +1 @@\n-expected\n+replacement\n"
    )[0]
    with pytest.raises(SandboxWorkspaceError) as exc_info:
        apply_file_patch("different\n", parsed)
    assert exc_info.value.code == "patch_conflict"


@pytest.mark.asyncio
async def test_operation_scope_binds_and_always_destroys(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-scoped")
    manager = SandboxOperationManager(backend=backend, staging_root=tmp_path / "stage")

    with pytest.raises(SandboxWorkspaceError) as before:
        get_current_coding_workspace()
    assert before.value.code == "no_active_operation"

    with pytest.raises(RuntimeError, match="business failure"):
        async with manager.scope(_spec()) as operation:
            assert get_current_coding_workspace() is operation
            raise RuntimeError("business failure")

    assert backend.destroyed_sandbox_ids == ["fake-scoped"]
    with pytest.raises(SandboxWorkspaceError) as after:
        get_current_coding_workspace()
    assert after.value.code == "no_active_operation"


@pytest.mark.asyncio
async def test_operation_constructor_failure_destroys_created_sandbox(
    tmp_path: Path,
) -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-constructor")
    manager = SandboxOperationManager(backend=backend, staging_root=tmp_path / "stage")
    too_many = (
        SandboxFileEntry(path="a", size=1, sha256="0" * 64),
        SandboxFileEntry(path="b", size=1, sha256="1" * 64),
    )
    spec = _spec().model_copy(
        update={"limits": _spec().limits.model_copy(update={"max_file_count": 1})}
    )

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        async with manager.scope(spec, baseline_entries=too_many):
            pass
    assert exc_info.value.code == "resource_limit"
    assert backend.destroyed_sandbox_ids == ["fake-constructor"]


@pytest.mark.asyncio
async def test_operation_rejects_duplicate_baseline_paths(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(id_factory=lambda: "fake-duplicate")
    manager = SandboxOperationManager(backend=backend, staging_root=tmp_path / "stage")
    duplicate = SandboxFileEntry(path="a", size=1, sha256="0" * 64)

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        async with manager.scope(
            _spec(),
            baseline_entries=(duplicate, duplicate),
        ):
            pass
    assert exc_info.value.code == "resource_limit"
    assert backend.destroyed_sandbox_ids == ["fake-duplicate"]


@pytest.mark.asyncio
async def test_operation_maps_helper_payloads_and_uses_fixed_argv(tmp_path: Path) -> None:
    file_payload = {
        "path": "src/app.py",
        "content": "print('ok')\n",
        "size": 12,
        "sha256": _digest("print('ok')\n"),
    }
    backend = FakeSandboxBackend(
        id_factory=lambda: "fake-helper",
        command_results=(
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "root": ".",
                        "files": [
                            {
                                "path": "src/app.py",
                                "size": 12,
                                "sha256": _digest("print('ok')\n"),
                            }
                        ],
                        "truncated": False,
                    },
                }
            ),
            _command_result({"ok": True, "result": file_payload}),
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "query": "print",
                        "root": ".",
                        "matches": [
                            {
                                "path": "src/app.py",
                                "line": 1,
                                "column": 1,
                                "text": "print('ok')",
                            }
                        ],
                        "truncated": False,
                    },
                }
            ),
            _command_result(
                {"ok": False, "error": "not_found"},
                exit_code=2,
            ),
        ),
    )
    handle = await backend.create(_spec())
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir="/workspace",
        limits=_spec().limits,
        staging_root=tmp_path / "stage",
    )

    listing = await operation.list_files()
    read = await operation.read_file("src/app.py")
    search = await operation.search("print")
    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.delete_file("missing.py")

    assert listing.files[0].path == "src/app.py"
    assert read.content == "print('ok')\n"
    assert search.matches[0].line == 1
    assert exc_info.value.code == "not_found"
    assert all(
        command.argv[:5] == ("python3", "-I", "-S", "-B", "-c")
        for command in backend.executed_commands
    )
    assert len({command.argv[5] for command in backend.executed_commands}) == 1
    assert all(command.cwd == "/workspace" for command in backend.executed_commands)


@pytest.mark.asyncio
async def test_write_stages_upload_and_removes_local_temporary_file(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(
        id_factory=lambda: "fake-write",
        command_results=(
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "path": "src/new.py",
                        "size": 3,
                        "sha256": _digest("new"),
                        "created": True,
                    },
                }
            ),
        ),
    )
    handle = await backend.create(_spec())
    staging = tmp_path / "stage"
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir="/workspace",
        limits=_spec().limits,
        staging_root=staging,
    )

    result = await operation.write_file("src/new.py", "new")

    assert result.created is True
    assert list(staging.iterdir()) == []
    command = backend.executed_commands[0]
    assert command.argv[6:8] == ("write", "/workspace")
    assert command.argv[8] == "src/new.py"


@pytest.mark.asyncio
async def test_apply_patch_uses_bounded_internal_read_limit(tmp_path: Path) -> None:
    before = 'print("before")\n'
    after = 'print("after")\n'
    backend = FakeSandboxBackend(
        command_results=(
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "path": "app.py",
                        "content": before,
                        "size": len(before),
                        "sha256": _digest(before),
                    },
                }
            ),
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "path": "app.py",
                        "size": len(after),
                        "sha256": _digest(after),
                        "created": False,
                    },
                }
            ),
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "root": ".",
                        "files": [
                            {
                                "path": "app.py",
                                "size": len(after),
                                "sha256": _digest(after),
                            }
                        ],
                        "truncated": False,
                    },
                }
            ),
            _command_result(
                {
                    "ok": True,
                    "result": {
                        "path": "app.py",
                        "content": after,
                        "size": len(after),
                        "sha256": _digest(after),
                    },
                }
            ),
        )
    )
    spec = _spec()
    assert spec.limits.max_file_bytes > spec.limits.max_output_bytes
    handle = await backend.create(spec)
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir=spec.workdir,
        limits=spec.limits,
        staging_root=tmp_path / "stage",
    )

    result = await operation.apply_patch(
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
        '-print("before")\n+print("after")\n'
    )

    assert result.entries[0].path == "app.py"
    read_limit = int(backend.executed_commands[0].argv[-1])
    assert read_limit < spec.limits.max_file_bytes


@pytest.mark.asyncio
async def test_run_passes_argv_streaming_and_terminal_result(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(
        id_factory=lambda: "fake-run",
        command_results=(
            _command_result(exit_code=3, stdout="output", payload=None),
        ),
    )
    handle = await backend.create(_spec())
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir="/workspace",
        limits=_spec().limits,
        staging_root=tmp_path / "stage",
    )
    chunks: list[str] = []

    async def on_output(chunk: SandboxOutputChunk) -> None:
        chunks.append(chunk.text)

    result = await operation.run(("python3", "-V"), on_output=on_output)

    assert result.exit_code == 3
    assert chunks == ["output"]
    assert backend.executed_commands[0].argv == ("python3", "-V")

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.run(())
    assert exc_info.value.code == "command_failed"


@pytest.mark.asyncio
async def test_success_payload_with_nonzero_exit_is_protocol_error(tmp_path: Path) -> None:
    backend = FakeSandboxBackend(
        command_results=(
            _command_result(
                {"ok": True, "result": {"root": ".", "files": [], "truncated": False}},
                exit_code=2,
            ),
        )
    )
    handle = await backend.create(_spec())
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir="/workspace",
        limits=_spec().limits,
        staging_root=tmp_path / "stage",
    )

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.list_files()
    assert exc_info.value.code == "protocol_error"
