"""Request-scoped high-level operations over a provider-neutral sandbox backend."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar, cast
from uuid import uuid4

from pydantic import ValidationError

from .access import OperationExecutionGuard
from .artifact import (
    MAX_ARTIFACT_METADATA_BYTES,
    ArtifactSigner,
    OutputEvidence,
    SandboxArtifactContents,
    SandboxOutputArtifact,
    SandboxRemoteArtifactReceipt,
    baseline_manifest_digest,
    canonical_json_bytes,
    create_artifact_signature,
    hash_regular_file,
    is_binary_content,
    validate_sandbox_artifact_archive,
    validation_evidence_bytes,
)
from .backend import SandboxBackend
from .bash import BASH_ARGV, BashBackend, BashRequest
from .errors import SandboxError
from .models import (
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxHandle,
    SandboxLimits,
    SandboxOutputCallback,
    validate_sandbox_path,
)
from .output_evidence import BashArtifactScope, BashOutputIntegrityEvidence
from .remote_artifact_helper import REMOTE_ARTIFACT_HELPER
from .remote_workspace_helper import REMOTE_WORKSPACE_HELPER
from .snapshot import (
    ProjectSnapshot,
    SnapshotError,
    SnapshotPolicy,
    is_snapshot_path_excluded,
    read_snapshot_file,
    validate_snapshot_archive,
)
from .unified_patch import UnifiedFilePatch, apply_file_patch, parse_unified_diff
from .validation import (
    MAX_VALIDATION_CONFIG_BYTES,
    SandboxValidationCheck,
    SandboxValidationCheckEvidence,
    SandboxValidationCheckStatus,
    SandboxValidationEvidence,
    SandboxValidationFailureCode,
    SandboxValidationPlan,
    SandboxWorkspaceFingerprint,
    digest_captured_text,
)
from .workspace_models import (
    CodingWorkspace,
    SandboxDeleteResult,
    SandboxDiffEntry,
    SandboxDiffResult,
    SandboxFileEntry,
    SandboxFileList,
    SandboxFileRead,
    SandboxSearchResult,
    SandboxWorkspaceError,
    SandboxWriteResult,
    WorkspaceErrorCode,
    validate_workspace_relative_path,
)

BaselineReader = Callable[[str], Awaitable[bytes | None]]
MutationResult = TypeVar("MutationResult")
_CURRENT_WORKSPACE: ContextVar[CodingWorkspace | None] = ContextVar(
    "coding_sandbox_current_workspace",
    default=None,
)
_HELPER_ERROR_CODES: frozenset[str] = frozenset(
    {
        "unsafe_path",
        "not_found",
        "not_file",
        "file_too_large",
        "invalid_encoding",
        "resource_limit",
        "command_failed",
        "protocol_error",
        "patch_invalid",
        "patch_conflict",
    }
)


class SandboxOperation(CodingWorkspace):
    """One disposable workspace owned by exactly one request context."""

    def __init__(
        self,
        *,
        backend: SandboxBackend,
        handle: SandboxHandle,
        workdir: str,
        limits: SandboxLimits,
        staging_root: Path,
        baseline_entries: Iterable[SandboxFileEntry] = (),
        baseline_reader: BaselineReader | None = None,
        validation_plan: SandboxValidationPlan | None = None,
        artifact_signer: ArtifactSigner | None = None,
        snapshot_policy: SnapshotPolicy | None = None,
        clock_ms: Callable[[], int] | None = None,
        execution_guard: OperationExecutionGuard | None = None,
        require_execution_grant: bool = False,
        bash_artifact_scope: BashArtifactScope | None = None,
        output_path_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        resolved_staging = staging_root.resolve(strict=False)
        if not resolved_staging.is_absolute():
            raise SandboxWorkspaceError("unsafe_path")
        validate_sandbox_path(workdir)
        self._backend = backend
        self._handle = handle
        self._workdir = workdir
        self._limits = limits
        self._staging_root = resolved_staging
        self._snapshot_policy = snapshot_policy or SnapshotPolicy.from_limits(limits)
        baseline = tuple(
            entry
            for entry in baseline_entries
            if not is_snapshot_path_excluded(entry.path, self._snapshot_policy)
        )
        self._baseline = {entry.path: entry for entry in baseline}
        if (
            len(self._baseline) != len(baseline)
            or len(self._baseline) > limits.max_file_count
            or any(entry.size > limits.max_file_bytes for entry in baseline)
        ):
            raise SandboxWorkspaceError("resource_limit")
        self._baseline_reader = baseline_reader
        if validation_plan is not None and any(
            check.timeout_seconds > limits.command_timeout_seconds
            for check in validation_plan.required_checks
        ):
            raise SandboxWorkspaceError("validation_config_invalid")
        self._validation_plan = validation_plan
        self._last_validation_evidence: SandboxValidationEvidence | None = None
        self._bash_artifact_scope = bash_artifact_scope
        self._bash_result: tuple[BashRequest, SandboxCommandResult, int] | None = None
        self._output_path_allowed = output_path_allowed
        self._artifact_signer = artifact_signer
        self._frozen = False
        self._output_artifact: SandboxOutputArtifact | None = None
        self._workspace_revision = 0
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = asyncio.Lock()
        self._closed = False
        self._execution_guard = execution_guard
        self._require_execution_grant = (
            require_execution_grant
            or execution_guard is not None
            or handle.provider == "local_docker"
        )

    async def _check_access(self, *, writing: bool) -> None:
        if self._execution_guard is not None:
            await self._execution_guard.check(self._handle, writing=writing)
        elif self._require_execution_grant:
            raise SandboxWorkspaceError("execution_approval_required")

    @property
    def operation_id(self) -> str:
        return self._handle.operation_id

    @property
    def handle(self) -> SandboxHandle:
        return self._handle

    @property
    def workspace_revision(self) -> int:
        return self._workspace_revision

    @property
    def last_validation_evidence(self) -> SandboxValidationEvidence | None:
        return self._last_validation_evidence

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def output_artifact(self) -> SandboxOutputArtifact | None:
        return self._output_artifact

    @property
    def standalone_bash(self) -> bool:
        return self._bash_artifact_scope is not None

    async def list_files(
        self,
        path: str = ".",
        *,
        max_files: int = 1000,
    ) -> SandboxFileList:
        async with self._lock:
            await self._check_access(writing=False)
            return await self._list_files_unlocked(path, max_files=max_files)

    async def seed_from_snapshot(self, snapshot: ProjectSnapshot) -> None:
        """Initialize a newly created remote workspace from one trusted snapshot."""
        async with self._lock:
            self._ensure_mutable()
            if self._workspace_revision != 0:
                raise SandboxWorkspaceError("protocol_error")
            try:
                manifest = validate_snapshot_archive(
                    snapshot.archive_path,
                    policy=snapshot.policy,
                )
            except SnapshotError as exc:
                raise SandboxWorkspaceError("protocol_error") from exc
            if manifest != snapshot.manifest:
                raise SandboxWorkspaceError("protocol_error")
            baseline = tuple(
                SandboxFileEntry(
                    path=entry.path,
                    size=entry.size,
                    sha256=entry.sha256,
                )
                for entry in manifest.entries
            )
            if {entry.path: entry for entry in baseline} != self._baseline:
                raise SandboxWorkspaceError("protocol_error")
            initial = await self._workspace_fingerprint_unlocked()
            if initial.file_count != 0 or initial.total_bytes != 0:
                raise SandboxWorkspaceError("protocol_error")
            for entry in baseline:
                try:
                    data = await asyncio.to_thread(
                        read_snapshot_file,
                        snapshot,
                        entry.path,
                    )
                except SnapshotError as exc:
                    raise SandboxWorkspaceError(
                        "protocol_error",
                        relative_path=entry.path,
                    ) from exc
                if data is None:
                    raise SandboxWorkspaceError(
                        "protocol_error",
                        relative_path=entry.path,
                    )
                await self._seed_file_unlocked(entry, data)
            observed = await self._workspace_fingerprint_unlocked()
            expected_fingerprint = _fingerprint_entries(baseline)
            if (
                observed.file_count != len(baseline)
                or observed.total_bytes != sum(entry.size for entry in baseline)
                or observed.sha256 != expected_fingerprint
            ):
                raise SandboxWorkspaceError("protocol_error")

    async def read_file(
        self,
        path: str,
        *,
        max_bytes: int = 64 * 1024,
    ) -> SandboxFileRead:
        async with self._lock:
            await self._check_access(writing=False)
            return await self._read_file_unlocked(path, max_bytes=max_bytes)

    async def search(
        self,
        query: str,
        *,
        path: str = ".",
        case_sensitive: bool = True,
        max_matches: int = 200,
    ) -> SandboxSearchResult:
        validate_workspace_relative_path(path, allow_root=True)
        if (
            not query
            or len(query.encode("utf-8")) > 4096
            or max_matches < 1
            or max_matches > 10_000
        ):
            raise SandboxWorkspaceError("resource_limit")
        safe_matches = min(max_matches, max(1, self._limits.max_output_bytes // 1024))
        async with self._lock:
            await self._check_access(writing=False)
            payload = await self._helper(
                "search",
                path,
                query,
                "1" if case_sensitive else "0",
                str(safe_matches),
                str(self._limits.max_file_bytes),
                str(self._limits.max_file_count),
            )
        try:
            return SandboxSearchResult.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError("protocol_error") from exc

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = True,
    ) -> SandboxWriteResult:
        async with self._lock:
            return await self._mutate(
                {"action": "write", "path": path, "content": content, "overwrite": overwrite},
                lambda: self._write_file_unlocked(path, content, overwrite=overwrite),
            )

    async def apply_patch(self, patch: str) -> SandboxDiffResult:
        file_patches = parse_unified_diff(patch)
        async with self._lock:
            return await self._mutate(
                {"action": "patch", "patch": patch},
                lambda: self._apply_patch_unlocked(file_patches),
            )

    async def _apply_patch_unlocked(
        self, file_patches: tuple[UnifiedFilePatch, ...]
    ) -> SandboxDiffResult:
        self._ensure_mutable()
        prepared: list[tuple[str, str | None]] = []
        for file_patch in file_patches:
            original: str | None
            try:
                original = (
                    await self._read_file_unlocked(
                        file_patch.path,
                        max_bytes=self._maximum_text_read_bytes(),
                    )
                ).content
            except SandboxWorkspaceError as exc:
                if exc.code != "not_found":
                    raise
                original = None
            prepared.append((file_patch.path, apply_file_patch(original, file_patch)))
        for path, content in prepared:
            if content is None:
                await self._delete_file_unlocked(path)
            else:
                await self._write_file_unlocked(path, content, overwrite=True)
        return await self._diff_unlocked(max_patch_bytes=256 * 1024)

    async def delete_file(self, path: str) -> SandboxDeleteResult:
        async with self._lock:
            return await self._mutate(
                {"action": "delete", "path": path}, lambda: self._delete_file_unlocked(path)
            )

    async def _mutate(
        self, payload: object, invoke: Callable[[], Awaitable[MutationResult]]
    ) -> MutationResult:
        self._ensure_mutable()
        await self._check_access(writing=True)
        if self._execution_guard is None:
            return await invoke()
        return await self._execution_guard.mutate(
            self._handle,
            payload=canonical_json_bytes(payload),
            operation_revision=self._workspace_revision,
            timeout_seconds=min(30, self._limits.command_timeout_seconds),
            invoke=invoke,
        )

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: int | None = None,
        on_output: SandboxOutputCallback | None = None,
        signal: asyncio.Event | None = None,
    ) -> SandboxCommandResult:
        self._ensure_open()
        if (
            not argv
            or len(argv) > 128
            or any(not part or len(part.encode("utf-8")) > 8192 or "\x00" in part for part in argv)
        ):
            raise SandboxWorkspaceError("command_failed")
        relative_cwd = validate_workspace_relative_path(cwd, allow_root=True)
        remote_cwd = self._remote_path(relative_cwd)
        timeout = timeout_seconds or self._limits.command_timeout_seconds
        if timeout < 1 or timeout > self._limits.command_timeout_seconds:
            raise SandboxWorkspaceError("resource_limit")
        try:
            command = SandboxCommand(
                command_id=f"run-{uuid4().hex}",
                argv=argv,
                cwd=remote_cwd,
                timeout_seconds=timeout,
                max_output_bytes=self._limits.max_output_bytes,
            )
        except ValidationError as exc:
            raise SandboxWorkspaceError("command_failed") from exc
        async with self._lock:
            self._ensure_open()
            self._ensure_mutable()
            try:
                return await self._execute_user_command(command, signal=signal, on_output=on_output)
            except SandboxError as exc:
                raise _map_backend_error(exc) from exc

    async def run_bash(
        self,
        request: BashRequest,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult:
        if self._handle.provider != "local_docker" or not isinstance(self._backend, BashBackend):
            raise SandboxWorkspaceError("bash_unavailable")
        if self._execution_guard is None:
            raise SandboxWorkspaceError("execution_approval_required")
        command = SandboxCommand(
            command_id=f"bash-{uuid4().hex}",
            argv=BASH_ARGV,
            cwd=self._remote_path(request.cwd),
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=min(16 * 1024, self._limits.max_output_bytes),
        )
        if command.timeout_seconds > self._limits.command_timeout_seconds:
            raise SandboxWorkspaceError("resource_limit")
        async with self._lock:
            self._ensure_mutable()
            result = await self._execute_user_command(
                command, signal=signal, on_output=on_output, bash=request
            )
            self._bash_result = (request, result, self._workspace_revision)
            return result

    async def _execute_user_command(
        self,
        command: SandboxCommand,
        *,
        signal: asyncio.Event | None,
        on_output: SandboxOutputCallback | None,
        validation: bool = False,
        bash: BashRequest | None = None,
    ) -> SandboxCommandResult:
        await self._check_access(writing=True)

        async def invoke(revoked: asyncio.Event) -> SandboxCommandResult:
            if revoked.is_set() or signal is not None and signal.is_set():
                raise asyncio.CancelledError
            if not validation:
                self._mark_workspace_may_change()
            if bash is not None:
                assert isinstance(self._backend, BashBackend)
                return await self._backend.execute_bash(
                    self._handle,
                    command,
                    script=bash.script,
                    signal=signal or revoked,
                    on_output=on_output,
                )
            return await self._backend.execute(
                self._handle,
                command,
                signal=signal or revoked,
                on_output=on_output,
            )

        if self._execution_guard is None:
            return await invoke(asyncio.Event())
        return await self._execution_guard.execute(
            self._handle,
            command,
            kind="validation" if validation else ("bash" if bash else "argv"),
            script_sha256=None if bash is None else bash.sha256,
            operation_revision=self._workspace_revision,
            invoke=invoke,
        )

    async def diff(self, *, max_patch_bytes: int = 256 * 1024) -> SandboxDiffResult:
        async with self._lock:
            await self._check_access(writing=False)
            return await self._diff_unlocked(max_patch_bytes=max_patch_bytes)

    async def validate_required_checks(
        self,
        *,
        on_output: SandboxOutputCallback | None = None,
        signal: asyncio.Event | None = None,
    ) -> SandboxValidationEvidence:
        async with self._lock:
            self._ensure_mutable()
            await self._check_access(writing=True)
            return await self._validate_required_checks_unlocked(
                on_output=on_output,
                signal=signal,
            )

    async def require_current_validation(self) -> SandboxValidationEvidence:
        async with self._lock:
            return await self._require_current_validation_unlocked()

    async def freeze_output_artifact(self) -> SandboxOutputArtifact:
        """Freeze the validated workspace, download, rehash and sign its artifact."""
        async with self._lock:
            if self._output_artifact is not None:
                return self._output_artifact
            return await self._freeze_output_artifact_unlocked()

    async def refreeze_output_artifact(self) -> SandboxOutputArtifact:
        """Create a fresh signed artifact from the same immutable Sandbox files."""
        async with self._lock:
            self._ensure_open()
            previous = self._output_artifact
            if not self._frozen or previous is None:
                raise SandboxWorkspaceError("artifact_invalid")
            self._output_artifact = None
            try:
                return await self._freeze_output_artifact_unlocked()
            except BaseException:
                self._output_artifact = previous
                raise

    async def close(self) -> None:
        if self._closed:
            return
        await self._backend.destroy(self._handle)
        self._closed = True

    async def _list_files_unlocked(
        self,
        path: str,
        *,
        max_files: int,
    ) -> SandboxFileList:
        validate_workspace_relative_path(path, allow_root=True)
        if max_files < 1:
            raise SandboxWorkspaceError("resource_limit")
        safe_maximum = min(
            max_files,
            self._limits.max_file_count,
            max(1, self._limits.max_output_bytes // 200),
        )
        payload = await self._helper("scan", path, str(safe_maximum))
        try:
            return SandboxFileList.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError("protocol_error") from exc

    async def _require_current_validation_unlocked(
        self,
    ) -> SandboxValidationEvidence:
        self._ensure_open()
        evidence = self._last_validation_evidence
        if (
            evidence is None
            or not evidence.passed
            or evidence.workspace_revision != self._workspace_revision
        ):
            raise SandboxWorkspaceError("validation_stale")
        if not await self._validation_source_matches_unlocked():
            self._last_validation_evidence = None
            raise SandboxWorkspaceError("validation_stale")
        try:
            current = await self._workspace_fingerprint_unlocked()
        except SandboxWorkspaceError:
            self._last_validation_evidence = None
            raise SandboxWorkspaceError("validation_stale") from None
        if current.sha256 != evidence.workspace_sha256_after:
            self._last_validation_evidence = None
            raise SandboxWorkspaceError("validation_stale")
        return evidence

    async def _freeze_output_artifact_unlocked(self) -> SandboxOutputArtifact:
        self._ensure_open()
        signer = self._artifact_signer
        if signer is None:
            raise SandboxWorkspaceError("artifact_signing_unavailable")
        evidence: OutputEvidence
        if self._bash_artifact_scope is not None:
            await self._check_access(writing=True)
            scope = self._bash_artifact_scope
            recorded = self._bash_result
            if recorded is None:
                raise SandboxWorkspaceError("validation_stale")
            request, result, revision = recorded
            if (
                not result.succeeded
                or revision != self._workspace_revision
                or request.sha256 != scope.script_sha256
                or request.cwd != scope.cwd
                or self._handle.provider != "local_docker"
            ):
                raise SandboxWorkspaceError("validation_stale")
            fingerprint = await self._workspace_fingerprint_unlocked()
            evidence = BashOutputIntegrityEvidence(
                **scope.model_dump(),
                operation_id=self.operation_id,
                command_id=result.command_id,
                workspace_revision=revision,
                workspace_sha256_after=fingerprint.sha256,
                result_sha256=hashlib.sha256(
                    canonical_json_bytes(result.model_dump(mode="json"))
                ).hexdigest(),
            )
        else:
            evidence = await self._require_current_validation_unlocked()
        artifact_id = f"artifact-{uuid4().hex}"
        created_at_ms = self._clock_ms()
        (
            request_bytes,
            expected_current,
            expected_baseline_binary,
        ) = await self._build_artifact_request_unlocked(
            artifact_id=artifact_id,
            created_at_ms=created_at_ms,
            evidence=evidence,
        )
        # Fail closed: once a verified workspace starts export, no command or file
        # mutation may run again even when transfer/signing later fails.
        self._frozen = True
        # The signed manifest binds artifact_id; repeating it in this temporary
        # filename needlessly exceeds Windows MAX_PATH under user data roots.
        local_download = self._new_staging_path("artifact-download")
        try:
            remote = await self._export_remote_artifact_unlocked(
                request_bytes,
                artifact_id=artifact_id,
            )
            if remote.workspace_sha256 != evidence.workspace_sha256_after:
                raise SandboxWorkspaceError("artifact_stale")
            if remote.size > self._limits.max_upload_bytes:
                raise SandboxWorkspaceError("resource_limit")
            try:
                transfer = await self._backend.download_file(
                    self._handle,
                    remote_path=remote.remote_path,
                    local_path=local_download,
                    expected_sha256=remote.sha256,
                )
            except SandboxError as exc:
                raise SandboxWorkspaceError("artifact_export_failed") from exc
            try:
                local_size, local_sha256 = await asyncio.to_thread(
                    hash_regular_file,
                    local_download,
                    max_bytes=self._limits.max_upload_bytes,
                )
            except (OSError, ValueError) as exc:
                raise SandboxWorkspaceError("artifact_invalid") from exc
            if (
                transfer.size != remote.size
                or transfer.sha256 != remote.sha256
                or local_size != remote.size
                or local_sha256 != remote.sha256
            ):
                raise SandboxWorkspaceError("artifact_invalid")
            try:
                contents = await asyncio.to_thread(
                    validate_sandbox_artifact_archive,
                    local_download,
                    max_archive_bytes=self._limits.max_upload_bytes,
                    max_file_bytes=self._limits.max_file_bytes,
                    max_file_count=self._limits.max_file_count,
                )
            except (OSError, ValueError) as exc:
                raise SandboxWorkspaceError("artifact_invalid") from exc
            self._verify_artifact_contents(
                contents,
                artifact_id=artifact_id,
                created_at_ms=created_at_ms,
                evidence=evidence,
                expected_current=expected_current,
                expected_baseline_binary=expected_baseline_binary,
                expected_manifest_sha256=remote.manifest_sha256,
            )
            if isinstance(evidence, SandboxValidationEvidence) and not (
                await self._validation_source_matches_unlocked()
            ):
                self._last_validation_evidence = None
                raise SandboxWorkspaceError("artifact_stale")
            try:
                final_fingerprint = await self._workspace_fingerprint_unlocked()
            except SandboxWorkspaceError:
                self._last_validation_evidence = None
                raise SandboxWorkspaceError("artifact_stale") from None
            if final_fingerprint.sha256 != evidence.workspace_sha256_after:
                self._last_validation_evidence = None
                raise SandboxWorkspaceError("artifact_stale")
            if self._execution_guard is not None:
                await self._check_access(writing=True)
            signature = create_artifact_signature(
                signer,
                archive_sha256=local_sha256,
                manifest_sha256=contents.manifest.manifest_sha256,
                signed_at_ms=self._clock_ms(),
            )
            try:
                final_path = await asyncio.to_thread(
                    self._persist_verified_artifact,
                    local_download,
                    archive_sha256=local_sha256,
                    archive_size=local_size,
                )
            except (OSError, ValueError) as exc:
                raise SandboxWorkspaceError("artifact_export_failed") from exc
            artifact = SandboxOutputArtifact(
                archive_path=final_path,
                archive_size=local_size,
                archive_sha256=local_sha256,
                manifest=contents.manifest,
                validation_evidence=contents.validation_evidence,
                signature=signature,
            )
            self._output_artifact = artifact
            return artifact
        finally:
            await asyncio.to_thread(local_download.unlink, missing_ok=True)

    async def _build_artifact_request_unlocked(
        self,
        *,
        artifact_id: str,
        created_at_ms: int,
        evidence: OutputEvidence,
    ) -> tuple[bytes, dict[str, SandboxFileEntry], dict[str, bool]]:
        current_list = await self._list_files_unlocked(
            ".",
            max_files=self._limits.max_file_count,
        )
        if current_list.truncated:
            raise SandboxWorkspaceError("resource_limit")
        if self._output_path_allowed is not None:
            raw_current = {entry.path: entry for entry in current_list.files}
            for path in set(self._baseline) | set(raw_current):
                if self._baseline.get(path) != raw_current.get(path) and not (
                    self._output_path_allowed(path)
                ):
                    raise SandboxWorkspaceError("unsafe_path", relative_path=path)
        current = {
            entry.path: entry
            for entry in current_list.files
            if not is_snapshot_path_excluded(entry.path, self._snapshot_policy)
        }
        baseline_entries = tuple(sorted(self._baseline.values(), key=lambda entry: entry.path))
        baseline_binary: dict[str, bool] = {}
        request_baseline: list[dict[str, object]] = []
        for entry in baseline_entries:
            observed = current.get(entry.path)
            changed = observed is None or (
                observed.size != entry.size or observed.sha256 != entry.sha256
            )
            binary: bool | None = None
            if changed:
                if self._baseline_reader is None:
                    raise SandboxWorkspaceError("artifact_baseline_unavailable")
                try:
                    content = await self._baseline_reader(entry.path)
                except Exception as exc:
                    raise SandboxWorkspaceError(
                        "artifact_baseline_unavailable",
                        relative_path=entry.path,
                    ) from exc
                if (
                    content is None
                    or len(content) != entry.size
                    or hashlib.sha256(content).hexdigest() != entry.sha256
                ):
                    raise SandboxWorkspaceError(
                        "artifact_baseline_unavailable",
                        relative_path=entry.path,
                    )
                binary = is_binary_content(content)
                baseline_binary[entry.path] = binary
            request_baseline.append(
                {
                    "path": entry.path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "binary": binary,
                }
            )
        payload = {
            "schema_version": (
                "pi-agent-bash-export/v1"
                if isinstance(evidence, BashOutputIntegrityEvidence)
                else "pi-agent-artifact-export/v1"
            ),
            "artifact_id": artifact_id,
            "operation_id": self.operation_id,
            "workspace_revision": evidence.workspace_revision,
            "created_at_ms": created_at_ms,
            "expected_workspace_sha256": evidence.workspace_sha256_after,
            "baseline_sha256": baseline_manifest_digest(baseline_entries),
            "baseline": request_baseline,
            "validation_evidence": evidence.model_dump(mode="json"),
            "excluded_directory_names": list(self._snapshot_policy.excluded_directory_names),
            "excluded_file_names": list(self._snapshot_policy.excluded_file_names),
            "excluded_globs": list(self._snapshot_policy.excluded_globs),
            "max_file_count": self._limits.max_file_count,
            "max_file_bytes": self._limits.max_file_bytes,
            "max_total_bytes": self._limits.max_upload_bytes,
            "max_archive_bytes": self._limits.max_upload_bytes,
        }
        request_bytes = canonical_json_bytes(payload)
        if len(request_bytes) > min(
            self._limits.max_upload_bytes,
            MAX_ARTIFACT_METADATA_BYTES,
        ):
            raise SandboxWorkspaceError("resource_limit")
        return request_bytes, current, baseline_binary

    async def _export_remote_artifact_unlocked(
        self,
        request_bytes: bytes,
        *,
        artifact_id: str,
    ) -> SandboxRemoteArtifactReceipt:
        local_request = await asyncio.to_thread(
            self._write_staging_file,
            request_bytes,
        )
        remote_request = f"/tmp/pi-agent-{artifact_id}-request.json"
        digest = hashlib.sha256(request_bytes).hexdigest()
        try:
            try:
                receipt = await self._backend.upload_file(
                    self._handle,
                    local_path=local_request,
                    remote_path=remote_request,
                    expected_sha256=digest,
                )
            except SandboxError as exc:
                raise SandboxWorkspaceError("artifact_export_failed") from exc
            if receipt.size != len(request_bytes) or receipt.sha256 != digest:
                raise SandboxWorkspaceError("artifact_export_failed")
            command = SandboxCommand(
                command_id=f"artifact-{uuid4().hex}",
                argv=(
                    "python3",
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    REMOTE_ARTIFACT_HELPER,
                    "export",
                    self._workdir,
                    remote_request,
                    digest,
                ),
                cwd=self._workdir,
                timeout_seconds=self._limits.command_timeout_seconds,
                max_output_bytes=self._limits.max_output_bytes,
            )
            try:
                result = await self._backend.execute(self._handle, command)
            except SandboxError as exc:
                raise SandboxWorkspaceError("artifact_export_failed") from exc
            try:
                payload = _decode_helper_result(result)
            except SandboxWorkspaceError as exc:
                raise SandboxWorkspaceError("artifact_export_failed") from exc
            if payload.get("ok") is not True:
                code = payload.get("error")
                if code == "workspace_changed":
                    self._last_validation_evidence = None
                    raise SandboxWorkspaceError("artifact_stale")
                if code == "resource_limit":
                    raise SandboxWorkspaceError("resource_limit")
                raise SandboxWorkspaceError("artifact_export_failed")
            raw = payload.get("result")
            if not isinstance(raw, dict):
                raise SandboxWorkspaceError("artifact_export_failed")
            try:
                remote_receipt = SandboxRemoteArtifactReceipt.model_validate(raw)
            except ValidationError as exc:
                raise SandboxWorkspaceError("artifact_export_failed") from exc
            if remote_receipt.remote_path != f"/tmp/pi-agent-{artifact_id}.tar":
                raise SandboxWorkspaceError("artifact_export_failed")
            return remote_receipt
        finally:
            await asyncio.to_thread(local_request.unlink, missing_ok=True)

    def _verify_artifact_contents(
        self,
        contents: SandboxArtifactContents,
        *,
        artifact_id: str,
        created_at_ms: int,
        evidence: OutputEvidence,
        expected_current: dict[str, SandboxFileEntry],
        expected_baseline_binary: dict[str, bool],
        expected_manifest_sha256: str,
    ) -> None:
        manifest = contents.manifest
        if (
            contents.validation_evidence != evidence
            or hashlib.sha256(validation_evidence_bytes(evidence)).hexdigest()
            != manifest.validation_evidence_sha256
            or manifest.artifact_id != artifact_id
            or manifest.operation_id != self.operation_id
            or manifest.workspace_revision != self._workspace_revision
            or manifest.created_at_ms != created_at_ms
            or manifest.workspace_sha256 != evidence.workspace_sha256_after
            or manifest.baseline_sha256
            != baseline_manifest_digest(
                tuple(sorted(self._baseline.values(), key=lambda item: item.path))
            )
            or manifest.manifest_sha256 != expected_manifest_sha256
        ):
            raise SandboxWorkspaceError("artifact_invalid")
        expected_changes: dict[str, tuple[str, SandboxFileEntry | None, SandboxFileEntry]] = {}
        expected_deletions: dict[str, SandboxFileEntry] = {}
        for path in sorted(set(self._baseline) | set(expected_current)):
            before = self._baseline.get(path)
            after = expected_current.get(path)
            if before is None and after is not None:
                expected_changes[path] = ("added", None, after)
            elif before is not None and after is None:
                expected_deletions[path] = before
            elif (
                before is not None
                and after is not None
                and (before.size != after.size or before.sha256 != after.sha256)
            ):
                expected_changes[path] = ("modified", before, after)
        if {entry.path for entry in manifest.changed_files} != set(expected_changes):
            raise SandboxWorkspaceError("artifact_invalid")
        if {entry.path for entry in manifest.deleted_files} != set(expected_deletions):
            raise SandboxWorkspaceError("artifact_invalid")
        for changed_entry in manifest.changed_files:
            status, before, after = expected_changes[changed_entry.path]
            if (
                changed_entry.status != status
                or changed_entry.before_size != (None if before is None else before.size)
                or changed_entry.before_sha256 != (None if before is None else before.sha256)
                or changed_entry.after_size != after.size
                or changed_entry.after_sha256 != after.sha256
                or changed_entry.before_binary
                != (None if before is None else expected_baseline_binary.get(changed_entry.path))
            ):
                raise SandboxWorkspaceError("artifact_invalid")
        for deleted_entry in manifest.deleted_files:
            before = expected_deletions[deleted_entry.path]
            if (
                deleted_entry.before_size != before.size
                or deleted_entry.before_sha256 != before.sha256
                or deleted_entry.before_binary != expected_baseline_binary.get(deleted_entry.path)
            ):
                raise SandboxWorkspaceError("artifact_invalid")

    def _new_staging_path(self, name: str) -> Path:
        self._ensure_staging_directory()
        return self._staging_root / f"{name}-{uuid4().hex}.tmp"

    def _ensure_staging_directory(self) -> None:
        self._staging_root.mkdir(parents=True, exist_ok=True)
        if self._staging_root.is_symlink() or not self._staging_root.is_dir():
            raise SandboxWorkspaceError("unsafe_path")

    def _persist_verified_artifact(
        self,
        source: Path,
        *,
        archive_sha256: str,
        archive_size: int,
    ) -> Path:
        self._ensure_staging_directory()
        artifact_root = self._staging_root / "artifacts"
        artifact_root.mkdir(mode=0o700, exist_ok=True)
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            raise ValueError("artifact output directory is unsafe")
        target = artifact_root / f"{archive_sha256}.tar"
        if target.exists():
            existing_size, existing_digest = hash_regular_file(
                target,
                max_bytes=self._limits.max_upload_bytes,
            )
            if existing_size != archive_size or existing_digest != archive_sha256:
                raise ValueError("content-addressed artifact collision")
            source.unlink(missing_ok=True)
            return target.resolve(strict=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o400)
        try:
            output_digest = hashlib.sha256()
            size = 0
            with source.open("rb") as input_stream:
                while chunk := input_stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self._limits.max_upload_bytes:
                        raise ValueError("artifact exceeds transfer limit")
                    output_digest.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        offset += os.write(descriptor, chunk[offset:])
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            target.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        if size != archive_size or output_digest.hexdigest() != archive_sha256:
            target.unlink(missing_ok=True)
            raise ValueError("persisted artifact digest mismatch")
        target.chmod(0o400)
        source.unlink(missing_ok=True)
        return target.resolve(strict=True)

    async def _validate_required_checks_unlocked(
        self,
        *,
        on_output: SandboxOutputCallback | None,
        signal: asyncio.Event | None,
    ) -> SandboxValidationEvidence:
        self._ensure_open()
        plan = self._validation_plan
        if plan is None:
            raise SandboxWorkspaceError("validation_not_configured")
        self._last_validation_evidence = None
        started_at_ms = self._clock_ms()
        revision = self._workspace_revision

        if not await self._validation_source_matches_unlocked():
            return self._store_validation_evidence(
                plan=plan,
                revision=revision,
                started_at_ms=started_at_ms,
                workspace_before=None,
                workspace_after=None,
                checks=tuple(self._not_run_check_evidence(check) for check in plan.required_checks),
                failure_code="configuration_changed",
            )

        try:
            workspace_before = await self._workspace_fingerprint_unlocked()
        except SandboxWorkspaceError:
            return self._store_validation_evidence(
                plan=plan,
                revision=revision,
                started_at_ms=started_at_ms,
                workspace_before=None,
                workspace_after=None,
                checks=tuple(self._not_run_check_evidence(check) for check in plan.required_checks),
                failure_code="execution_error",
            )

        checks: list[SandboxValidationCheckEvidence] = []
        halted = signal is not None and signal.is_set()
        for check in plan.required_checks:
            if halted:
                checks.append(self._not_run_check_evidence(check))
                continue
            evidence = await self._execute_validation_check_unlocked(
                check,
                capture_bytes=min(plan.capture_bytes, self._limits.max_output_bytes),
                on_output=on_output,
                signal=signal,
            )
            checks.append(evidence)
            if evidence.status in {
                "cancelled",
                "sandbox_lost",
                "execution_error",
            }:
                halted = True
            elif signal is not None and signal.is_set():
                halted = True

        try:
            workspace_after = await self._workspace_fingerprint_unlocked()
        except SandboxWorkspaceError:
            workspace_after = None

        source_matches = await self._validation_source_matches_unlocked()
        failure_code = _validation_failure_code(
            checks,
            source_matches=source_matches,
            workspace_before=workspace_before,
            workspace_after=workspace_after,
            cancel_requested=signal is not None and signal.is_set(),
        )
        return self._store_validation_evidence(
            plan=plan,
            revision=revision,
            started_at_ms=started_at_ms,
            workspace_before=workspace_before,
            workspace_after=workspace_after,
            checks=tuple(checks),
            failure_code=failure_code,
        )

    async def _execute_validation_check_unlocked(
        self,
        check: SandboxValidationCheck,
        *,
        capture_bytes: int,
        on_output: SandboxOutputCallback | None,
        signal: asyncio.Event | None,
    ) -> SandboxValidationCheckEvidence:
        started_at_ms = self._clock_ms()
        try:
            command = SandboxCommand(
                command_id=f"validation-{uuid4().hex}",
                argv=check.argv,
                cwd=self._remote_path(check.cwd),
                timeout_seconds=check.timeout_seconds,
                max_output_bytes=capture_bytes,
            )
            result = await self._execute_user_command(
                command,
                signal=signal,
                on_output=on_output,
                validation=True,
            )
        except asyncio.CancelledError:
            raise
        except SandboxError as exc:
            status = _validation_backend_error_status(exc)
            finished_at_ms = self._clock_ms()
            return self._empty_check_evidence(
                check,
                status=status,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
            )
        except Exception:
            finished_at_ms = self._clock_ms()
            return self._empty_check_evidence(
                check,
                status="execution_error",
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
            )

        status = _validation_result_status(result)
        return SandboxValidationCheckEvidence(
            check_id=check.check_id,
            argv=check.argv,
            cwd=check.cwd,
            timeout_seconds=check.timeout_seconds,
            status=status,
            exit_code=result.exit_code,
            termination_reason=result.termination_reason,
            started_at_ms=result.started_at_ms,
            finished_at_ms=result.finished_at_ms,
            duration_ms=max(0, result.finished_at_ms - result.started_at_ms),
            stdout=result.stdout,
            stderr=result.stderr,
            output_truncated=result.output_truncated,
            captured_stdout_sha256=digest_captured_text(result.stdout),
            captured_stderr_sha256=digest_captured_text(result.stderr),
        )

    def _empty_check_evidence(
        self,
        check: SandboxValidationCheck,
        *,
        status: SandboxValidationCheckStatus,
        started_at_ms: int,
        finished_at_ms: int,
    ) -> SandboxValidationCheckEvidence:
        return SandboxValidationCheckEvidence(
            check_id=check.check_id,
            argv=check.argv,
            cwd=check.cwd,
            timeout_seconds=check.timeout_seconds,
            status=status,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            duration_ms=max(0, finished_at_ms - started_at_ms),
            captured_stdout_sha256=digest_captured_text(""),
            captured_stderr_sha256=digest_captured_text(""),
        )

    def _not_run_check_evidence(
        self,
        check: SandboxValidationCheck,
    ) -> SandboxValidationCheckEvidence:
        observed_at_ms = self._clock_ms()
        return self._empty_check_evidence(
            check,
            status="not_run",
            started_at_ms=observed_at_ms,
            finished_at_ms=observed_at_ms,
        )

    async def _validation_source_matches_unlocked(self) -> bool:
        plan = self._validation_plan
        if plan is None:
            return False
        maximum = min(MAX_VALIDATION_CONFIG_BYTES, self._maximum_text_read_bytes())
        try:
            source = await self._read_file_unlocked(
                plan.source_path,
                max_bytes=maximum,
            )
        except SandboxWorkspaceError:
            return False
        return source.sha256 == plan.source_sha256

    async def _workspace_fingerprint_unlocked(self) -> SandboxWorkspaceFingerprint:
        payload = await self._helper(
            "fingerprint",
            str(self._limits.max_file_count),
            str(self._limits.max_upload_bytes),
        )
        try:
            return SandboxWorkspaceFingerprint.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError("protocol_error") from exc

    def _store_validation_evidence(
        self,
        *,
        plan: SandboxValidationPlan,
        revision: int,
        started_at_ms: int,
        workspace_before: SandboxWorkspaceFingerprint | None,
        workspace_after: SandboxWorkspaceFingerprint | None,
        checks: tuple[SandboxValidationCheckEvidence, ...],
        failure_code: SandboxValidationFailureCode | None,
    ) -> SandboxValidationEvidence:
        finished_at_ms = self._clock_ms()
        evidence = SandboxValidationEvidence(
            evidence_id=f"validation-{uuid4().hex}",
            operation_id=self.operation_id,
            source_path=plan.source_path,
            source_sha256=plan.source_sha256,
            plan_sha256=plan.plan_sha256,
            workspace_revision=revision,
            workspace_sha256_before=(None if workspace_before is None else workspace_before.sha256),
            workspace_sha256_after=(None if workspace_after is None else workspace_after.sha256),
            checks=checks,
            passed=failure_code is None,
            failure_code=failure_code,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            duration_ms=max(0, finished_at_ms - started_at_ms),
        )
        self._last_validation_evidence = evidence
        return evidence

    async def _read_file_unlocked(
        self,
        path: str,
        *,
        max_bytes: int,
    ) -> SandboxFileRead:
        validate_workspace_relative_path(path)
        safe_output_limit = self._maximum_text_read_bytes()
        if max_bytes < 1 or max_bytes > min(self._limits.max_file_bytes, safe_output_limit):
            raise SandboxWorkspaceError("resource_limit", relative_path=path)
        payload = await self._helper("read", path, str(max_bytes))
        try:
            result = SandboxFileRead.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError("protocol_error") from exc
        data = result.content.encode("utf-8")
        if result.size != len(data) or result.sha256 != hashlib.sha256(data).hexdigest():
            raise SandboxWorkspaceError("protocol_error", relative_path=path)
        return result

    async def _write_file_unlocked(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool,
    ) -> SandboxWriteResult:
        validate_workspace_relative_path(path)
        data = content.encode("utf-8")
        if len(data) > self._limits.max_file_bytes:
            raise SandboxWorkspaceError("file_too_large", relative_path=path)
        local_path = await asyncio.to_thread(self._write_staging_file, data)
        remote_staging = f"/tmp/pi-agent-upload-{self.operation_id}-{uuid4().hex}"
        digest = hashlib.sha256(data).hexdigest()
        try:
            await self._backend.upload_file(
                self._handle,
                local_path=local_path,
                remote_path=remote_staging,
                expected_sha256=digest,
            )
            self._mark_workspace_may_change()
            payload = await self._helper(
                "write",
                path,
                remote_staging,
                "1" if overwrite else "0",
                str(self._limits.max_file_bytes),
            )
        except SandboxError as exc:
            raise _map_backend_error(exc, relative_path=path) from exc
        finally:
            await asyncio.to_thread(local_path.unlink, missing_ok=True)
        try:
            result = SandboxWriteResult.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError("protocol_error", relative_path=path) from exc
        if result.size != len(data) or result.sha256 != digest:
            raise SandboxWorkspaceError("protocol_error", relative_path=path)
        return result

    async def _seed_file_unlocked(
        self,
        entry: SandboxFileEntry,
        data: bytes,
    ) -> None:
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise SandboxWorkspaceError("protocol_error", relative_path=entry.path)
        local_path = await asyncio.to_thread(self._write_staging_file, data)
        remote_staging = f"/tmp/pi-agent-seed-{self.operation_id}-{uuid4().hex}"
        try:
            await self._backend.upload_file(
                self._handle,
                local_path=local_path,
                remote_path=remote_staging,
                expected_sha256=entry.sha256,
            )
            payload = await self._helper(
                "write",
                entry.path,
                remote_staging,
                "0",
                str(self._limits.max_file_bytes),
            )
        except SandboxError as exc:
            raise _map_backend_error(exc, relative_path=entry.path) from exc
        finally:
            await asyncio.to_thread(local_path.unlink, missing_ok=True)
        try:
            result = SandboxWriteResult.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError(
                "protocol_error",
                relative_path=entry.path,
            ) from exc
        if result.size != entry.size or result.sha256 != entry.sha256:
            raise SandboxWorkspaceError("protocol_error", relative_path=entry.path)

    async def _delete_file_unlocked(self, path: str) -> SandboxDeleteResult:
        validate_workspace_relative_path(path)
        self._mark_workspace_may_change()
        payload = await self._helper("delete", path)
        try:
            return SandboxDeleteResult.model_validate(payload)
        except ValidationError as exc:
            raise SandboxWorkspaceError("protocol_error", relative_path=path) from exc

    async def _diff_unlocked(self, *, max_patch_bytes: int) -> SandboxDiffResult:
        if max_patch_bytes < 0 or max_patch_bytes > self._limits.max_output_bytes:
            raise SandboxWorkspaceError("resource_limit")
        current_list = await self._list_files_unlocked(
            ".",
            max_files=self._limits.max_file_count,
        )
        if current_list.truncated:
            raise SandboxWorkspaceError("resource_limit")
        current = {
            entry.path: entry
            for entry in current_list.files
            if not is_snapshot_path_excluded(entry.path, self._snapshot_policy)
        }
        paths = sorted(set(self._baseline) | set(current))
        entries: list[SandboxDiffEntry] = []
        patch_parts: list[str] = []
        for path in paths:
            before = self._baseline.get(path)
            after = current.get(path)
            if before is None and after is not None:
                status = "added"
            elif before is not None and after is None:
                status = "deleted"
            elif before is not None and after is not None and before.sha256 != after.sha256:
                status = "modified"
            else:
                continue
            entries.append(
                SandboxDiffEntry(
                    path=path,
                    status=status,
                    before_sha256=None if before is None else before.sha256,
                    after_sha256=None if after is None else after.sha256,
                )
            )
            patch_text = await self._text_diff(path, before=before, after=after)
            if patch_text:
                patch_parts.append(patch_text)
        patch, truncated = _truncate_utf8("".join(patch_parts), max_patch_bytes)
        return SandboxDiffResult(
            entries=tuple(entries),
            patch=patch,
            patch_truncated=truncated,
        )

    async def _text_diff(
        self,
        path: str,
        *,
        before: SandboxFileEntry | None,
        after: SandboxFileEntry | None,
    ) -> str:
        before_bytes: bytes | None = None
        if before is not None and self._baseline_reader is not None:
            before_bytes = await self._baseline_reader(path)
        after_text: str | None = None
        if after is not None:
            try:
                after_text = (
                    await self._read_file_unlocked(
                        path,
                        max_bytes=self._maximum_text_read_bytes(),
                    )
                ).content
            except SandboxWorkspaceError:
                return ""
        try:
            before_text = None if before_bytes is None else before_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        if before is not None and before_text is None:
            return ""
        old_lines = [] if before_text is None else before_text.splitlines(keepends=True)
        new_lines = [] if after_text is None else after_text.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile="/dev/null" if before is None else f"a/{path}",
                tofile="/dev/null" if after is None else f"b/{path}",
            )
        )

    async def _helper(self, action: str, *arguments: str) -> Mapping[str, Any]:
        self._ensure_open()
        command = SandboxCommand(
            command_id=f"helper-{uuid4().hex}",
            argv=(
                "python3",
                "-I",
                "-S",
                "-B",
                "-c",
                REMOTE_WORKSPACE_HELPER,
                action,
                self._workdir,
                *arguments,
            ),
            cwd=self._workdir,
            timeout_seconds=self._limits.command_timeout_seconds,
            max_output_bytes=self._limits.max_output_bytes,
        )
        try:
            result = await self._backend.execute(self._handle, command)
        except SandboxError as exc:
            raise _map_backend_error(exc) from exc
        payload = _decode_helper_result(result)
        if payload.get("ok") is not True:
            raw_code = payload.get("error")
            code: WorkspaceErrorCode = (
                cast(WorkspaceErrorCode, raw_code)
                if isinstance(raw_code, str) and raw_code in _HELPER_ERROR_CODES
                else "protocol_error"
            )
            raise SandboxWorkspaceError(code)
        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            raise SandboxWorkspaceError("protocol_error")
        return cast("dict[str, Any]", raw_result)

    def _remote_path(self, relative_path: str) -> str:
        if relative_path == ".":
            return self._workdir
        return validate_sandbox_path(
            str(PurePosixPath(self._workdir).joinpath(PurePosixPath(relative_path)))
        )

    def _maximum_text_read_bytes(self) -> int:
        # JSON may expand one input byte to a six-byte ``\u00xx`` escape. Keep
        # room for the fixed response envelope and metadata as well.
        safe_output_limit = max(1, (self._limits.max_output_bytes - 4096) // 6)
        return min(self._limits.max_file_bytes, safe_output_limit)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxWorkspaceError("operation_closed")

    def _ensure_mutable(self) -> None:
        self._ensure_open()
        if self._frozen:
            raise SandboxWorkspaceError("operation_frozen")

    def _mark_workspace_may_change(self) -> None:
        self._ensure_mutable()
        self._workspace_revision += 1
        self._last_validation_evidence = None

    def _write_staging_file(self, data: bytes) -> Path:
        self._ensure_staging_directory()
        target = self._staging_root / f"sandbox-upload-{uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags, 0o600)
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            target.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
        return target


class SandboxOperationManager:
    """Create, bind and always destroy one operation per async request scope."""

    def __init__(
        self,
        *,
        backend: SandboxBackend,
        staging_root: Path,
        artifact_signer: ArtifactSigner | None = None,
    ) -> None:
        self._backend = backend
        self._staging_root = staging_root.resolve(strict=False)
        self._artifact_signer = artifact_signer

    @asynccontextmanager
    async def scope(
        self,
        spec: SandboxCreateSpec,
        *,
        baseline_entries: Iterable[SandboxFileEntry] = (),
        baseline_reader: BaselineReader | None = None,
        validation_plan: SandboxValidationPlan | None = None,
    ) -> AsyncIterator[SandboxOperation]:
        if _CURRENT_WORKSPACE.get() is not None:
            raise SandboxWorkspaceError("protocol_error")
        handle = await self._backend.create(spec)
        try:
            operation = SandboxOperation(
                backend=self._backend,
                handle=handle,
                workdir=spec.workdir,
                limits=spec.limits,
                staging_root=self._staging_root,
                baseline_entries=baseline_entries,
                baseline_reader=baseline_reader,
                validation_plan=validation_plan,
                artifact_signer=self._artifact_signer,
            )
        except BaseException:
            try:
                await self._backend.destroy(handle)
            except BaseException:
                pass
            raise
        token: Token[CodingWorkspace | None] = _CURRENT_WORKSPACE.set(operation)
        try:
            yield operation
        except BaseException:
            _CURRENT_WORKSPACE.reset(token)
            try:
                await operation.close()
            except BaseException:
                pass
            raise
        else:
            _CURRENT_WORKSPACE.reset(token)
            await operation.close()


@contextmanager
def bind_coding_workspace(workspace: CodingWorkspace) -> Iterator[None]:
    """Server-only binding shared by scoped tools; always restore the caller."""
    token = _CURRENT_WORKSPACE.set(workspace)
    try:
        yield
    finally:
        _CURRENT_WORKSPACE.reset(token)


def get_current_coding_workspace() -> CodingWorkspace:
    workspace = _CURRENT_WORKSPACE.get()
    if workspace is None:
        raise SandboxWorkspaceError("no_active_operation")
    return workspace


def _fingerprint_entries(entries: Iterable[SandboxFileEntry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.path):
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validation_result_status(
    result: SandboxCommandResult,
) -> SandboxValidationCheckStatus:
    if result.termination_reason == "timed_out":
        return "timed_out"
    if result.termination_reason == "cancelled":
        return "cancelled"
    if result.termination_reason == "sandbox_lost":
        return "sandbox_lost"
    return "passed" if result.exit_code == 0 else "failed"


def _validation_backend_error_status(
    error: SandboxError,
) -> SandboxValidationCheckStatus:
    if error.code == "command_timeout":
        return "timed_out"
    if error.code == "cancelled":
        return "cancelled"
    if error.code in {"sandbox_not_found", "sandbox_not_ready"}:
        return "sandbox_lost"
    return "execution_error"


def _validation_failure_code(
    checks: list[SandboxValidationCheckEvidence],
    *,
    source_matches: bool,
    workspace_before: SandboxWorkspaceFingerprint,
    workspace_after: SandboxWorkspaceFingerprint | None,
    cancel_requested: bool,
) -> SandboxValidationFailureCode | None:
    if not source_matches:
        return "configuration_changed"
    statuses = {check.status for check in checks}
    if "sandbox_lost" in statuses:
        return "sandbox_lost"
    if "cancelled" in statuses or (cancel_requested and statuses == {"not_run"}):
        return "cancelled"
    if "execution_error" in statuses or workspace_after is None:
        return "execution_error"
    if workspace_before.sha256 != workspace_after.sha256:
        return "workspace_changed"
    if statuses != {"passed"}:
        return "check_failed"
    return None


def _decode_helper_result(result: SandboxCommandResult) -> dict[str, Any]:
    if result.output_truncated or result.termination_reason != "exited":
        raise SandboxWorkspaceError("protocol_error")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise SandboxWorkspaceError("protocol_error") from exc
    if not isinstance(payload, dict):
        raise SandboxWorkspaceError("protocol_error")
    if payload.get("ok") is True and result.exit_code != 0:
        raise SandboxWorkspaceError("protocol_error")
    return cast("dict[str, Any]", payload)


def _map_backend_error(
    error: SandboxError,
    *,
    relative_path: str | None = None,
) -> SandboxWorkspaceError:
    if error.code == "resource_limit":
        code: WorkspaceErrorCode = "resource_limit"
    elif error.code in {"command_timeout", "cancelled"}:
        code = "command_failed"
    else:
        code = "protocol_error"
    return SandboxWorkspaceError(code, relative_path=relative_path)


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    return encoded[:maximum].decode("utf-8", errors="ignore"), True


__all__ = [
    "BaselineReader",
    "SandboxOperation",
    "SandboxOperationManager",
    "get_current_coding_workspace",
]
