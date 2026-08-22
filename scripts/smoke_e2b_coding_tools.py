"""Run coding tools and the fixed validation gate against persisted E2B.

This diagnostic never accepts or prints an API key. It resolves the existing
credential through the application's Keyring-backed CredentialService.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import time
from pathlib import Path
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from coding_sandbox import (  # noqa: E402
    E2BSandboxBackend,
    HMACSHA256ArtifactSigner,
    LocalTransactionalPublisher,
    PublisherError,
    SandboxCreateSpec,
    SandboxOperationManager,
    SandboxWorkspaceError,
    build_project_snapshot,
    parse_sandbox_validation_config,
    verify_output_artifact,
)
from coding_sandbox.admin import SQLiteSandboxConfigStore  # noqa: E402
from pi_agent_core_py.tools import (  # noqa: E402
    create_coding_sandbox_tools,
    create_coding_validation_tool,
)
from pi_agent_core_py.web.credentials.runtime import (  # noqa: E402
    build_credential_runtime_config,
    credential_runtime_context,
)
from pi_agent_core_py.web.local_web_security import WebSecurityConfig  # noqa: E402


class SmokeError(RuntimeError):
    """Safe, secret-free smoke failure."""


VALIDATION_CONFIG = (
    b'version = 1\n\n[[required_checks]]\nid = "python"\n'
    b'argv = ["python3", "-c", "import pathlib,sys;'
    b"p=pathlib.Path('src/sandbox_smoke.py');"
    b"sys.exit(0 if p.is_file() and 'sandbox-ok' in p.read_text() else 1)\"]\n"
    b'cwd = "."\ntimeout_seconds = 20\n'
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise all coding tools in a disposable E2B sandbox.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--workspace-db",
        type=Path,
        help=(
            "Absolute workspace.sqlite path. Omit when .pi-agent-data contains "
            "exactly one user workspace."
        ),
    )
    return parser.parse_args()


def _resolve_workspace_database(configured: Path | None) -> Path:
    if configured is not None:
        resolved = configured.expanduser().resolve(strict=False)
        if not resolved.is_absolute() or resolved.name != "workspace.sqlite":
            raise SmokeError("--workspace-db must be an absolute path ending in workspace.sqlite")
        if not resolved.is_file():
            raise SmokeError("the selected workspace database does not exist")
        return resolved
    candidates = sorted(
        path.resolve()
        for path in (REPOSITORY_ROOT / ".pi-agent-data" / "users").glob("*/workspace.sqlite")
        if path.is_file()
    )
    if len(candidates) != 1:
        raise SmokeError("expected exactly one workspace database; pass --workspace-db explicitly")
    return candidates[0]


async def _require_success(name: str, result: object) -> None:
    is_error = getattr(result, "is_error", True)
    if is_error:
        details = getattr(result, "details", {})
        error_code = details.get("error_code", "tool_error")
        raise SmokeError(f"{name} failed ({error_code})")


def _workspace_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


async def _smoke(database_path: Path) -> None:
    credential_config = build_credential_runtime_config(
        database_path=database_path,
        secret_backend_mode="keyring",
        web_security=WebSecurityConfig(),
    )
    async with credential_runtime_context(credential_config) as credential_runtime:
        sandbox_store = await SQLiteSandboxConfigStore.open(database_path)
        try:
            record = await sandbox_store.get()
            config = record.config
            if not config.enabled or config.credential_id is None:
                raise SmokeError("the persisted E2B sandbox configuration is unavailable")
            api_key = await credential_runtime.service.resolve_secret_for_request(
                config.credential_id
            )
            try:
                backend = E2BSandboxBackend(
                    api_key=api_key,
                    default_limits=config.limits,
                )
            finally:
                del api_key

            artifact_signer = HMACSHA256ArtifactSigner(
                key_id="e2b-smoke-ephemeral",
                secret=secrets.token_bytes(32),
            )
            publisher_smoke_root = (
                REPOSITORY_ROOT
                / ".test-tmp"
                / f"e2b-publisher-smoke-{uuid4().hex}"
            )
            publisher_project_root = publisher_smoke_root / "project"
            publisher_state_root = publisher_smoke_root / "state"
            publisher_project_root.mkdir(parents=True)
            baseline = await build_project_snapshot(
                publisher_project_root,
                publisher_smoke_root / "baseline.tar.gz",
            )
            manager = SandboxOperationManager(
                backend=backend,
                staging_root=(
                    REPOSITORY_ROOT
                    / ".test-tmp"
                    / "e2b-coding-smoke"
                ),
                artifact_signer=artifact_signer,
            )
            spec = SandboxCreateSpec(
                operation_id=f"coding-smoke-{uuid4().hex}",
                runtime_id=config.runtime_id,
                workdir=config.workdir,
                network=config.network,
                limits=config.limits,
            )
            validation_plan = parse_sandbox_validation_config(VALIDATION_CONFIG)
            async with manager.scope(
                spec,
                validation_plan=validation_plan,
            ) as operation:
                await operation.write_file(
                    ".pi-agent/sandbox.toml",
                    VALIDATION_CONFIG.decode("utf-8"),
                )
                tools = {tool.name: tool for tool in create_coding_sandbox_tools()}
                validation_tool = create_coding_validation_tool()
                tools[validation_tool.name] = validation_tool
                results = []
                results.append(
                    (
                        "coding_list_files",
                        await tools["coding_list_files"].execute("list", {}),
                    )
                )
                results.append(
                    (
                        "coding_write_file",
                        await tools["coding_write_file"].execute(
                            "write",
                            {
                                "path": "src/sandbox_smoke.py",
                                "content": 'print("before")\n',
                            },
                        ),
                    )
                )
                results.append(
                    (
                        "coding_read_file",
                        await tools["coding_read_file"].execute(
                            "read", {"path": "src/sandbox_smoke.py"}
                        ),
                    )
                )
                results.append(
                    (
                        "coding_apply_patch",
                        await tools["coding_apply_patch"].execute(
                            "patch",
                            {
                                "patch": (
                                    "--- a/src/sandbox_smoke.py\n"
                                    "+++ b/src/sandbox_smoke.py\n"
                                    "@@ -1 +1 @@\n"
                                    '-print("before")\n'
                                    '+print("sandbox-ok")\n'
                                )
                            },
                        ),
                    )
                )
                results.append(
                    (
                        "coding_search",
                        await tools["coding_search"].execute(
                            "search", {"query": "sandbox-ok", "path": "src"}
                        ),
                    )
                )
                run_result = await tools["coding_run"].execute(
                    "run", {"argv": ["python3", "src/sandbox_smoke.py"]}
                )
                results.append(("coding_run", run_result))
                results.append(
                    (
                        "coding_diff",
                        await tools["coding_diff"].execute("diff", {}),
                    )
                )
                for name, result in results:
                    await _require_success(name, result)
                first_validation = await tools["coding_validate"].execute(
                    "validate-before-delete",
                    {},
                )
                await _require_success("coding_validate", first_validation)
                delete_result = await tools["coding_delete_file"].execute(
                    "delete", {"path": "src/sandbox_smoke.py"}
                )
                await _require_success("coding_delete_file", delete_result)
                try:
                    await operation.require_current_validation()
                except SandboxWorkspaceError as exc:
                    if exc.code != "validation_stale":
                        raise
                else:
                    raise SmokeError("workspace mutation did not invalidate validation")
                local_before_failed_validation = _workspace_bytes(
                    publisher_project_root
                )
                failed_validation = await tools["coding_validate"].execute(
                    "validate-expected-failure",
                    {},
                )
                if (
                    not failed_validation.is_error
                    or failed_validation.details.get("failure_code") != "check_failed"
                ):
                    raise SmokeError("required validation failure was not enforced")
                if _workspace_bytes(publisher_project_root) != local_before_failed_validation:
                    raise SmokeError("failed validation changed the local project")
                try:
                    await operation.freeze_output_artifact()
                except SandboxWorkspaceError as exc:
                    if exc.code != "validation_stale":
                        raise
                else:
                    raise SmokeError("failed validation still allowed artifact freeze")
                restore_result = await tools["coding_write_file"].execute(
                    "restore",
                    {
                        "path": "src/sandbox_smoke.py",
                        "content": 'print("sandbox-ok")\n',
                    },
                )
                await _require_success("coding_write_file", restore_result)
                final_validation = await tools["coding_validate"].execute(
                    "validate-after-restore",
                    {},
                )
                await _require_success("coding_validate", final_validation)
                await operation.require_current_validation()
                artifact = await operation.freeze_output_artifact()
                verified = verify_output_artifact(
                    artifact,
                    artifact_signer,
                    max_archive_bytes=config.limits.max_upload_bytes,
                    max_file_bytes=config.limits.max_file_bytes,
                    max_file_count=config.limits.max_file_count,
                )
                if verified.manifest.changed_file_count != 2:
                    raise SmokeError("frozen artifact did not contain the expected files")
                publisher = LocalTransactionalPublisher(
                    project_root=publisher_project_root,
                    state_root=publisher_state_root,
                )
                conflict_path = publisher_project_root / "local-conflict.txt"
                conflict_path.write_bytes(b"local change must survive\n")
                conflict_before = _workspace_bytes(publisher_project_root)
                try:
                    await publisher.publish(
                        artifact,
                        baseline=baseline,
                        signer=artifact_signer,
                    )
                except PublisherError as exc:
                    if exc.code != "publish_conflict":
                        raise
                else:
                    raise SmokeError("Publisher accepted a changed baseline")
                if _workspace_bytes(publisher_project_root) != conflict_before:
                    raise SmokeError("Publisher conflict changed the local project")
                conflict_path.unlink()
                publish_result = await publisher.publish(
                    artifact,
                    baseline=baseline,
                    signer=artifact_signer,
                )
                if publish_result.status != "published":
                    raise SmokeError("local Publisher did not commit the artifact")
                published_files = _workspace_bytes(publisher_project_root)
                if published_files != {
                    ".pi-agent/sandbox.toml": VALIDATION_CONFIG,
                    "src/sandbox_smoke.py": b'print("sandbox-ok")\n',
                }:
                    raise SmokeError("local Publisher output did not match the artifact")
                recovery = await publisher.recover_pending()
                if recovery.recovered_rollbacks:
                    raise SmokeError("committed Publisher transaction was rolled back")
                retry = await publisher.publish(
                    artifact,
                    baseline=baseline,
                    signer=artifact_signer,
                )
                if retry.status != "already_published":
                    raise SmokeError("local Publisher retry was not idempotent")
                try:
                    await operation.run(("python3", "-V"))
                except SandboxWorkspaceError as frozen_error:
                    if frozen_error.code != "operation_frozen":
                        raise
                else:
                    raise SmokeError("frozen operation still accepted a command")
                run_text = "\n".join(block.text for block in run_result.content)
                if "sandbox-ok" not in run_text:
                    raise SmokeError("coding_run output did not contain the expected marker")
                if operation.handle.operation_id != spec.operation_id:
                    raise SmokeError("operation handle ownership mismatch")
        finally:
            await sandbox_store.close()


def main() -> None:
    started = time.monotonic()
    try:
        asyncio.run(_smoke(_resolve_workspace_database(_parse_args().workspace_db)))
    except KeyboardInterrupt:
        print("smoke_cancelled", file=sys.stderr)
        raise SystemExit(130) from None
    except SmokeError as exc:
        print(f"smoke_error={exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(f"smoke_error_type={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    print("smoke_ok=true")
    print("tools_exercised=9")
    print("validation_rerun=true")
    print("validation_failure_blocked=true")
    print("artifact_frozen=true")
    print("artifact_signed=true")
    print("publisher_conflict_unchanged=true")
    print("publisher_committed=true")
    print("publisher_retry_idempotent=true")
    print("sandbox_destroyed=true")
    print(f"duration_ms={int((time.monotonic() - started) * 1000)}")


if __name__ == "__main__":
    main()
