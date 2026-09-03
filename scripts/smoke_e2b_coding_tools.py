"""Run coding tools and managed Workspace publishing against persisted E2B.

This diagnostic never accepts or prints an API key. It resolves the existing
credential through the application's Keyring-backed CredentialService.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_workspace import WorkspaceStore  # noqa: E402
from coding_agent_app.sandbox.automation import (  # noqa: E402
    CodingSandboxAutomation,
)
from coding_agent_app.sandbox_workspace import (  # noqa: E402
    WorkspaceSandboxArtifactPublisher,
    WorkspaceSandboxBaselineProvider,
)
from coding_sandbox import (  # noqa: E402
    ArtifactSigner,
    CodingWorkspace,
    E2BSandboxBackend,
    HMACSHA256ArtifactSigner,
    ManagedSandboxLifecycle,
    ManagedSandboxOperationRecord,
    ProjectSnapshot,
    PublisherError,
    PublisherResult,
    SandboxLifecycleError,
    SandboxOutputArtifact,
    SQLiteSandboxOperationStore,
)
from coding_sandbox.admin import (  # noqa: E402
    SandboxAdminConfig,
    SandboxConfigRecord,
    SQLiteSandboxConfigStore,
)
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


class _DiagnosingWorkspacePublisher:
    """Capture only fixed-code/OS-category evidence from release-smoke failures."""

    def __init__(self, delegate: WorkspaceSandboxArtifactPublisher) -> None:
        self._delegate = delegate
        self.error_detail: str | None = None

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
        try:
            return await self._delegate.publish(
                artifact,
                baseline=baseline,
                signer=signer,
                session_id=session_id,
                expected_workspace_revision=expected_workspace_revision,
                expected_workspace_sha256=expected_workspace_sha256,
            )
        except PublisherError as exc:
            cause = exc.__cause__
            self.error_detail = (
                f"{exc.code}:cause={type(cause).__name__ if cause else 'none'}:"
                f"winerror={getattr(cause, 'winerror', None)}"
            )
            raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise all coding tools and approval-bound Workspace publishing "
            "in a disposable E2B sandbox."
        ),
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


async def _wait_for_status(
    lifecycle: ManagedSandboxLifecycle,
    operation_id: str,
    expected: str,
    *,
    timeout_seconds: float = 120,
) -> ManagedSandboxOperationRecord:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = await lifecycle.get(operation_id)
        if record.status == expected:
            return record
        if record.terminal:
            page = await lifecycle.events(operation_id)
            error_type = (
                page.events[-1].payload.get("error_type", "unknown") if page.events else "unknown"
            )
            raise SmokeError(
                f"operation failed before {expected}: {record.status} "
                f"({record.error_code or 'no_error_code'}; type={error_type})"
            )
        await asyncio.sleep(0.1)
    raise SmokeError(f"operation did not reach {expected}")


async def _smoke(database_path: Path) -> None:
    smoke_root = REPOSITORY_ROOT / ".test-tmp" / f"e2b-workspace-smoke-{uuid4().hex}"
    operation_store: SQLiteSandboxOperationStore | None = None
    lifecycle: ManagedSandboxLifecycle | None = None
    credential_config = build_credential_runtime_config(
        database_path=database_path,
        secret_backend_mode="keyring",
        web_security=WebSecurityConfig(),
    )
    try:
        async with credential_runtime_context(credential_config) as credential_runtime:
            sandbox_store = await SQLiteSandboxConfigStore.open(database_path)
            try:
                config_record = await sandbox_store.get()
                config = config_record.config
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

                workspace_store = WorkspaceStore(smoke_root / "uploads")
                await workspace_store.init()
                session_id = f"session-e2b-smoke-{uuid4().hex}"
                await workspace_store.ensure_session_workspace(session_id)
                operation_store = await SQLiteSandboxOperationStore.open(
                    smoke_root / "operations.sqlite"
                )

                async def config_provider() -> SandboxConfigRecord:
                    return config_record

                async def backend_resolver(
                    _config: SandboxAdminConfig,
                ) -> E2BSandboxBackend:
                    return backend

                async def session_exists(candidate: str) -> bool:
                    return candidate == session_id

                workspace_publisher = _DiagnosingWorkspacePublisher(
                    WorkspaceSandboxArtifactPublisher(
                        workspace_store,
                        staging_root=smoke_root / "workspace-publishes",
                    )
                )
                lifecycle = ManagedSandboxLifecycle(
                    store=operation_store,
                    config_provider=config_provider,
                    backend_resolver=backend_resolver,
                    artifact_signer=HMACSHA256ArtifactSigner(
                        key_id="e2b-smoke-ephemeral",
                        secret=secrets.token_bytes(32),
                    ),
                    session_exists=session_exists,
                    projects_root=None,
                    state_root=smoke_root / "publisher-state",
                    staging_root=smoke_root / "staging",
                    baseline_provider=WorkspaceSandboxBaselineProvider(
                        workspace_store,
                        materialization_root=smoke_root / "materialized",
                    ),
                    artifact_publisher=workspace_publisher,
                )
                automation = CodingSandboxAutomation(lifecycle)
                ready = await automation.prepare(session_id)
                if (
                    ready.baseline_workspace_revision is None
                    or ready.baseline_workspace_sha256 is None
                    or not ready.publish_available
                ):
                    raise SmokeError("managed operation did not bind a Workspace baseline")

                def workspace_getter() -> CodingWorkspace:
                    assert lifecycle is not None
                    return lifecycle.workspace_for_session(session_id)

                tools = {
                    tool.name: tool
                    for tool in create_coding_sandbox_tools(workspace_getter=workspace_getter)
                }
                validation_tool = create_coding_validation_tool(workspace_getter=workspace_getter)
                tools[validation_tool.name] = validation_tool
                initial_results = [
                    (
                        "coding_list_files",
                        await tools["coding_list_files"].execute("list", {}),
                    ),
                    (
                        "coding_write_file",
                        await tools["coding_write_file"].execute(
                            "write-invalid",
                            {
                                "path": "scripts/sandbox_smoke.py",
                                "content": "def broken(:\n",
                            },
                        ),
                    ),
                    (
                        "coding_read_file",
                        await tools["coding_read_file"].execute(
                            "read",
                            {"path": "scripts/sandbox_smoke.py"},
                        ),
                    ),
                    (
                        "coding_search",
                        await tools["coding_search"].execute(
                            "search",
                            {"query": "broken", "path": "scripts"},
                        ),
                    ),
                    (
                        "coding_diff",
                        await tools["coding_diff"].execute("diff", {}),
                    ),
                ]
                for name, result in initial_results:
                    await _require_success(name, result)

                await lifecycle.validate(ready.operation_id)
                failed = await _wait_for_status(
                    lifecycle,
                    ready.operation_id,
                    "validation_failed",
                )
                if failed.error_code != "check_failed":
                    raise SmokeError("invalid Python did not fail the fixed validation gate")
                if (
                    await workspace_store.get_by_logical_path(
                        session_id,
                        "scripts/sandbox_smoke.py",
                    )
                    is not None
                ):
                    raise SmokeError("failed validation changed WorkspaceStore")
                try:
                    await lifecycle.prepare_publish(ready.operation_id)
                except SandboxLifecycleError as freeze_error:
                    if freeze_error.code != "validation_required":
                        raise
                else:
                    raise SmokeError("failed validation still allowed freeze")

                patch_result = await tools["coding_apply_patch"].execute(
                    "fix",
                    {
                        "patch": (
                            "--- a/scripts/sandbox_smoke.py\n"
                            "+++ b/scripts/sandbox_smoke.py\n"
                            "@@ -1 +1 @@\n"
                            "-def broken(:\n"
                            '+print("sandbox-ok")\n'
                        )
                    },
                )
                await _require_success("coding_apply_patch", patch_result)
                temporary = await tools["coding_write_file"].execute(
                    "write-temporary",
                    {
                        "path": "scripts/delete_me.py",
                        "content": "print('delete me')\n",
                    },
                )
                await _require_success("coding_write_file", temporary)
                deleted = await tools["coding_delete_file"].execute(
                    "delete",
                    {"path": "scripts/delete_me.py"},
                )
                await _require_success("coding_delete_file", deleted)
                run_result = await tools["coding_run"].execute(
                    "run",
                    {"argv": ["python3", "scripts/sandbox_smoke.py"]},
                )
                await _require_success("coding_run", run_result)
                tool_validation = await tools["coding_validate"].execute(
                    "validate-tool",
                    {},
                )
                await _require_success("coding_validate", tool_validation)

                automated = await automation.validate_and_freeze(ready.operation_id)
                validated = await lifecycle.get(ready.operation_id)
                if validated.validation is None or not validated.validation.passed:
                    raise SmokeError("managed validation evidence was not persisted")
                if automated.status != "awaiting_approval":
                    raise SmokeError("automation did not stop at approval")
                awaiting = await lifecycle.get(ready.operation_id)
                if awaiting.artifact_id is None or "publish" not in awaiting.allowed_actions:
                    raise SmokeError("freeze did not produce an approval-bound artifact")
                await lifecycle.publish(ready.operation_id)
                try:
                    published = await _wait_for_status(
                        lifecycle,
                        ready.operation_id,
                        "published",
                    )
                except SmokeError as exc:
                    if workspace_publisher.error_detail is not None:
                        raise SmokeError(
                            f"Workspace publisher failed ({workspace_publisher.error_detail})"
                        ) from exc
                    raise
                expected_revision = ready.baseline_workspace_revision + 1
                if published.published_workspace_revision != expected_revision:
                    raise SmokeError("Workspace revision did not advance exactly once")
                published_file = await workspace_store.get_by_logical_path(
                    session_id,
                    "scripts/sandbox_smoke.py",
                )
                if published_file is None:
                    raise SmokeError("approved artifact was not written to WorkspaceStore")
                published_content = await asyncio.to_thread(
                    Path(published_file.path).read_text,
                    encoding="utf-8",
                )
                if published_content != 'print("sandbox-ok")\n':
                    raise SmokeError("WorkspaceStore content did not match the artifact")
                run_text = "\n".join(block.text for block in run_result.content)
                if "sandbox-ok" not in run_text:
                    raise SmokeError("coding_run output did not contain the expected marker")
            finally:
                await sandbox_store.close()
    finally:
        if lifecycle is not None:
            await lifecycle.shutdown()
        if operation_store is not None:
            await operation_store.close()
        await asyncio.to_thread(shutil.rmtree, smoke_root, ignore_errors=True)


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
    print("approval_required=true")
    print("workspace_store_published=true")
    print("workspace_revision_advanced=true")
    print("sandbox_destroyed=true")
    print(f"duration_ms={int((time.monotonic() - started) * 1000)}")


if __name__ == "__main__":
    main()
