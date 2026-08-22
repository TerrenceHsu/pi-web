"""Fixed validation-plan, evidence and invalidation contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_sandbox import (
    FakeSandboxBackend,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxLimits,
    SandboxOperation,
    SandboxOperationManager,
    SandboxValidationCheckEvidence,
    SandboxValidationConfigError,
    SandboxValidationEvidence,
    SandboxWorkspaceError,
    build_project_snapshot,
    load_sandbox_validation_plan,
    parse_sandbox_validation_config,
)
from pi_agent_core_py.tools import ToolResult, create_coding_validation_tool

CONFIG = b'''version = 1

[[required_checks]]
id = "tests"
argv = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
cwd = "."
timeout_seconds = 20
'''


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _helper_result(
    result: dict[str, object],
    *,
    exit_code: int = 0,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id="queued-helper",
        exit_code=exit_code,
        stdout=json.dumps({"ok": True, "result": result}),
        started_at_ms=10,
        finished_at_ms=11,
    )


def _config_read(content: bytes = CONFIG) -> SandboxCommandResult:
    return _helper_result(
        {
            "path": ".pi-agent/sandbox.toml",
            "content": content.decode("utf-8"),
            "size": len(content),
            "sha256": _digest(content),
        }
    )


def _fingerprint(value: str = "a") -> SandboxCommandResult:
    return _helper_result(
        {
            "file_count": 2,
            "total_bytes": 100,
            "sha256": value * 64,
        }
    )


def _check_result(
    *,
    exit_code: int = 0,
    stdout: str = "tests passed\n",
    stderr: str = "",
    output_truncated: bool = False,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id="queued-check",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        output_truncated=output_truncated,
        started_at_ms=20,
        finished_at_ms=35,
    )


def _spec() -> SandboxCreateSpec:
    return SandboxCreateSpec(
        operation_id="validation-test",
        runtime_id="base",
        limits=SandboxLimits(
            lifetime_seconds=60,
            command_timeout_seconds=20,
            max_output_bytes=1024 * 1024,
        ),
    )


async def _operation(
    tmp_path: Path,
    results: tuple[SandboxCommandResult, ...],
) -> tuple[SandboxOperation, FakeSandboxBackend]:
    backend = FakeSandboxBackend(command_results=results)
    spec = _spec()
    handle = await backend.create(spec)
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir=spec.workdir,
        limits=spec.limits,
        staging_root=tmp_path / "stage",
        validation_plan=parse_sandbox_validation_config(CONFIG),
        clock_ms=iter((1_000, 1_100, 1_200, 1_300, 1_400)).__next__,
    )
    return operation, backend


def test_parser_builds_deterministic_plan_and_pins_source_bytes() -> None:
    equivalent = b'''version=1
[[required_checks]]
id="tests"
argv=["python3","-m","pytest","-q","-p","no:cacheprovider"]
timeout_seconds=20
cwd="."
'''

    first = parse_sandbox_validation_config(CONFIG)
    second = parse_sandbox_validation_config(equivalent)

    assert first.plan_sha256 == second.plan_sha256
    assert first.source_sha256 != second.source_sha256
    assert first.required_checks[0].argv == (
        "python3",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    )
    assert first.capture_bytes == 256 * 1024


@pytest.mark.parametrize(
    ("content", "code"),
    (
        (b"", "resource_limit"),
        (b"\xff", "invalid_encoding"),
        (b"not toml =", "invalid_toml"),
        (b"version=2\nrequired_checks=[]", "unsupported_version"),
        (b"version=1\nrequired_checks=[]", "resource_limit"),
        (b"version=1\nrequired_checks=[]\nextra=true", "invalid_schema"),
        (
            b'version=1\n[[required_checks]]\nid="x"\nargv="pytest"',
            "invalid_schema",
        ),
        (
            b'version=1\n[[required_checks]]\nid="x"\nargv=["pytest"]\ncwd="../x"',
            "invalid_schema",
        ),
        (
            b'version=1\n[[required_checks]]\nid="x"\nargv=["pytest"]\nenv={X="Y"}',
            "invalid_schema",
        ),
    ),
)
def test_parser_rejects_unbounded_or_unapproved_shapes(
    content: bytes,
    code: str,
) -> None:
    with pytest.raises(SandboxValidationConfigError) as exc_info:
        parse_sandbox_validation_config(content)
    assert exc_info.value.code == code
    decoded = content.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in repr(exc_info.value)


def test_parser_rejects_duplicate_check_ids() -> None:
    content = b'''version=1
[[required_checks]]
id="same"
argv=["one"]
[[required_checks]]
id="same"
argv=["two"]
'''
    with pytest.raises(SandboxValidationConfigError) as exc_info:
        parse_sandbox_validation_config(content)
    assert exc_info.value.code == "invalid_schema"


@pytest.mark.asyncio
async def test_loader_pins_plan_to_revalidated_snapshot_member(tmp_path: Path) -> None:
    root = tmp_path / "project"
    config_path = root / ".pi-agent" / "sandbox.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(CONFIG)
    (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
    snapshot = await build_project_snapshot(
        root,
        tmp_path / "artifacts" / "snapshot.tar.gz",
    )

    plan = load_sandbox_validation_plan(snapshot)

    assert plan.source_sha256 == _digest(CONFIG)
    assert plan.required_checks[0].check_id == "tests"


@pytest.mark.asyncio
async def test_loader_rejects_missing_or_tampered_snapshot_config(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing-project"
    missing_root.mkdir()
    (missing_root / "app.py").write_text("pass\n", encoding="utf-8")
    missing_snapshot = await build_project_snapshot(
        missing_root,
        tmp_path / "artifacts" / "missing.tar.gz",
    )
    with pytest.raises(SandboxValidationConfigError) as missing:
        load_sandbox_validation_plan(missing_snapshot)
    assert missing.value.code == "missing_config"

    root = tmp_path / "project"
    config_path = root / ".pi-agent" / "sandbox.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(CONFIG)
    snapshot = await build_project_snapshot(
        root,
        tmp_path / "artifacts" / "tampered.tar.gz",
    )
    snapshot.archive_path.write_bytes(snapshot.archive_path.read_bytes() + b"tamper")
    with pytest.raises(SandboxValidationConfigError) as tampered:
        load_sandbox_validation_plan(snapshot)
    assert tampered.value.code == "snapshot_invalid"


@pytest.mark.asyncio
async def test_validation_runs_fixed_argv_and_records_complete_evidence(
    tmp_path: Path,
) -> None:
    operation, backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint(),
            _check_result(),
            _fingerprint(),
            _config_read(),
            _config_read(),
            _fingerprint(),
        ),
    )

    evidence = await operation.validate_required_checks()

    assert evidence.passed is True
    assert evidence.failure_code is None
    assert evidence.workspace_revision == 0
    assert evidence.workspace_sha256_before == "a" * 64
    assert evidence.workspace_sha256_after == "a" * 64
    assert evidence.checks[0].status == "passed"
    assert evidence.checks[0].duration_ms == 15
    assert evidence.checks[0].stdout == "tests passed\n"
    assert evidence.checks[0].captured_stdout_sha256 == _digest(b"tests passed\n")
    check_command = backend.executed_commands[2]
    assert check_command.argv == (
        "python3",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    )
    assert check_command.cwd == "/workspace"
    assert check_command.max_output_bytes == 256 * 1024
    assert operation.last_validation_evidence is evidence
    assert await operation.require_current_validation() is evidence


@pytest.mark.asyncio
async def test_nonzero_check_preserves_bounded_logs_and_fails_gate(
    tmp_path: Path,
) -> None:
    operation, _backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint(),
            _check_result(
                exit_code=2,
                stdout="partial\n",
                stderr="failed\n",
                output_truncated=True,
            ),
            _fingerprint(),
            _config_read(),
        ),
    )

    evidence = await operation.validate_required_checks()

    assert evidence.passed is False
    assert evidence.failure_code == "check_failed"
    assert evidence.checks[0].status == "failed"
    assert evidence.checks[0].exit_code == 2
    assert evidence.checks[0].output_truncated is True
    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.require_current_validation()
    assert exc_info.value.code == "validation_stale"


@pytest.mark.asyncio
async def test_check_that_changes_workspace_cannot_pass(tmp_path: Path) -> None:
    operation, _backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint("a"),
            _check_result(),
            _fingerprint("b"),
            _config_read(),
        ),
    )

    evidence = await operation.validate_required_checks()

    assert evidence.passed is False
    assert evidence.failure_code == "workspace_changed"
    assert evidence.checks[0].status == "passed"


@pytest.mark.asyncio
async def test_changed_config_never_executes_model_selected_checks(tmp_path: Path) -> None:
    changed = CONFIG.replace(b'argv = ["python3"', b'argv = ["true"')
    operation, backend = await _operation(tmp_path, (_config_read(changed),))

    evidence = await operation.validate_required_checks()

    assert evidence.failure_code == "configuration_changed"
    assert evidence.checks[0].status == "not_run"
    assert len(backend.executed_commands) == 1


@pytest.mark.asyncio
async def test_any_public_run_invalidates_successful_evidence(tmp_path: Path) -> None:
    operation, _backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint(),
            _check_result(),
            _fingerprint(),
            _config_read(),
            _check_result(stdout="manual\n"),
        ),
    )
    evidence = await operation.validate_required_checks()
    assert evidence.passed is True

    await operation.run(("python3", "-V"))

    assert operation.workspace_revision == 1
    assert operation.last_validation_evidence is None
    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.require_current_validation()
    assert exc_info.value.code == "validation_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("write", "delete"))
async def test_file_mutations_invalidate_successful_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    if mutation == "write":
        mutation_result = _helper_result(
            {
                "path": "changed.txt",
                "size": 7,
                "sha256": _digest(b"changed"),
                "created": True,
            }
        )
    else:
        mutation_result = _helper_result({"path": "changed.txt"})
    operation, _backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint(),
            _check_result(),
            _fingerprint(),
            _config_read(),
            mutation_result,
        ),
    )
    assert (await operation.validate_required_checks()).passed is True

    if mutation == "write":
        await operation.write_file("changed.txt", "changed")
    else:
        await operation.delete_file("changed.txt")

    assert operation.workspace_revision == 1
    assert operation.last_validation_evidence is None


@pytest.mark.asyncio
async def test_out_of_band_workspace_change_is_detected_before_consuming_evidence(
    tmp_path: Path,
) -> None:
    operation, _backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint("a"),
            _check_result(),
            _fingerprint("a"),
            _config_read(),
            _config_read(),
            _fingerprint("b"),
        ),
    )
    assert (await operation.validate_required_checks()).passed is True

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.require_current_validation()

    assert exc_info.value.code == "validation_stale"
    assert operation.last_validation_evidence is None


@pytest.mark.asyncio
async def test_pre_cancelled_validation_records_not_run_checks(tmp_path: Path) -> None:
    operation, backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint(),
            _fingerprint(),
            _config_read(),
        ),
    )
    signal = asyncio.Event()
    signal.set()

    evidence = await operation.validate_required_checks(signal=signal)

    assert evidence.failure_code == "cancelled"
    assert evidence.checks[0].status == "not_run"
    assert all(command.argv[3] != "python3" for command in backend.executed_commands)


@pytest.mark.asyncio
async def test_validation_tool_has_no_model_controlled_command_arguments(
    tmp_path: Path,
) -> None:
    operation, backend = await _operation(
        tmp_path,
        (
            _config_read(),
            _fingerprint(),
            _check_result(stdout="streamed\n"),
            _fingerprint(),
            _config_read(),
        ),
    )
    tool = create_coding_validation_tool(workspace_getter=lambda: operation)
    updates: list[ToolResult] = []

    async def on_update(result: ToolResult) -> None:
        updates.append(result)

    result = await tool.execute(
        "validate-call",
        {"argv": ["true"], "pretend_passed": True},
        on_update=on_update,
    )

    assert tool.parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert result.is_error is False
    assert result.details["passed"] is True
    assert backend.executed_commands[2].argv[0:3] == ("python3", "-m", "pytest")
    assert updates[0].content[0].text == "streamed\n"
    assert updates[0].details["partial"] is True


def test_evidence_models_reject_forged_status_and_hash_combinations() -> None:
    with pytest.raises(ValidationError):
        SandboxValidationCheckEvidence(
            check_id="tests",
            argv=("true",),
            cwd=".",
            timeout_seconds=1,
            status="passed",
            exit_code=1,
            termination_reason="exited",
            started_at_ms=1,
            finished_at_ms=2,
            duration_ms=1,
            stdout="forged",
            captured_stdout_sha256="0" * 64,
            captured_stderr_sha256=_digest(b""),
        )

    check = SandboxValidationCheckEvidence(
        check_id="tests",
        argv=("true",),
        cwd=".",
        timeout_seconds=1,
        status="passed",
        exit_code=0,
        termination_reason="exited",
        started_at_ms=1,
        finished_at_ms=2,
        duration_ms=1,
        captured_stdout_sha256=_digest(b""),
        captured_stderr_sha256=_digest(b""),
    )
    with pytest.raises(ValidationError):
        SandboxValidationEvidence(
            evidence_id="validation-" + "0" * 32,
            operation_id="op",
            source_path=".pi-agent/sandbox.toml",
            source_sha256="0" * 64,
            plan_sha256="1" * 64,
            workspace_revision=0,
            workspace_sha256_before="2" * 64,
            workspace_sha256_after="3" * 64,
            checks=(check,),
            passed=True,
            started_at_ms=1,
            finished_at_ms=2,
            duration_ms=1,
        )


@pytest.mark.asyncio
async def test_validation_requires_configured_plan(tmp_path: Path) -> None:
    backend = FakeSandboxBackend()
    spec = _spec()
    handle = await backend.create(spec)
    operation = SandboxOperation(
        backend=backend,
        handle=handle,
        workdir=spec.workdir,
        limits=spec.limits,
        staging_root=tmp_path / "stage",
    )

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        await operation.validate_required_checks()
    assert exc_info.value.code == "validation_not_configured"


@pytest.mark.asyncio
async def test_plan_timeout_above_operation_limit_fails_before_scope_is_bound(
    tmp_path: Path,
) -> None:
    config = CONFIG.replace(b"timeout_seconds = 20", b"timeout_seconds = 21")
    plan = parse_sandbox_validation_config(config)
    backend = FakeSandboxBackend(id_factory=lambda: "invalid-plan")
    manager = SandboxOperationManager(backend=backend, staging_root=tmp_path / "stage")

    with pytest.raises(SandboxWorkspaceError) as exc_info:
        async with manager.scope(_spec(), validation_plan=plan):
            pass

    assert exc_info.value.code == "validation_config_invalid"
    assert backend.destroyed_sandbox_ids == ["invalid-plan"]
