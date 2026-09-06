"""Request-scoped automation for managed coding Sandbox operations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from coding_sandbox.lifecycle import (
    ManagedSandboxLifecycle,
    ManagedSandboxOperationRecord,
    SandboxLifecycleError,
)
from pi_agent_core_py.ai.llm_messages import LLMMessage
from pi_agent_core_py.ai.model_client import ModelClient
from pi_agent_core_py.ai.stream_events import StreamEvent, ToolCallEndEvent, ToolCallEvent
from pi_agent_core_py.ai.tooling import ToolDef

AUTOMATED_CODING_PROMPT = """Automated Coding mode is active for this request.

Work only through the available coding_* tools. First call coding_list_files once. Your next
action MUST be coding_write_file or coding_apply_patch on scripts/**; create the requested
file before any coding_run, package inspection, dependency installation, or coding_validate.
Do not install dependencies unless the user explicitly asks for installation. Dependency-heavy
source can be syntax-checked without importing optional packages. Do not draft the requested
source code only in reasoning or chat. Put non-code deliverables in artifacts/** or shared notes
in docs/notes/**. Treat upload/**, inputs/**, HANDOFF.md, tasks/**, fixed docs summaries,
and documents/** as read-only. Copy uploaded source to scripts/** before editing it.
After a file exists, run the relevant checks and use coding_validate. If validation
fails, diagnose the output, repair the files, and validate again. A Coding turn with no file diff
is a failure. Do not write directly to the Session Workspace and do not claim that changes were
published. After your turn, the server will run an independent fixed validation and freeze the
exact artifact for the user's explicit approval.
"""

AUTOMATED_CODING_REPAIR_PROMPT = """# Automatic no-change repair

The preceding attempt ended without changing any Sandbox file. This is the only automatic
repair attempt. Do not inspect packages, install dependencies, call coding_run, or explain the
failure first. Immediately call coding_write_file or coding_apply_patch to implement the user's
original request under scripts/**. Keep the first written file concise and valid; add more files
or details with later tool calls. Then validate the written files.
"""

_AttemptResult = TypeVar("_AttemptResult")

_USABLE_STATUSES = frozenset({"ready", "validation_failed", "validated"})
_CREATE_PENDING_STATUSES = frozenset({"creating"})
_VALIDATION_PENDING_STATUSES = frozenset({"validating"})
_FREEZE_PENDING_STATUSES = frozenset({"freezing"})


class CodingSandboxAutomationError(RuntimeError):
    """Stable, secret-free failure raised by automatic Coding mode."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation_id = operation_id


@dataclass(frozen=True)
class AutomatedCodingResult:
    operation_id: str
    status: str
    workspace_revision: int
    artifact_id: str | None

    def public(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "workspace_revision": self.workspace_revision,
            "artifact_id": self.artifact_id,
            "approval_required": self.status == "awaiting_approval",
        }


class CodingToolBootstrapModelClient(ModelClient):
    """Force a bounded list-then-mutate bootstrap for code-generation requests."""

    _MUTATION_TOOL_NAMES = frozenset({"coding_write_file", "coding_apply_patch"})

    def __init__(self, delegate: ModelClient, *, list_first: bool) -> None:
        super().__init__(delegate.adapter)
        self._delegate = delegate
        self._stage = "list" if list_first else "mutation"

    async def stream(
        self,
        *,
        system_prompt: str,
        messages: list[LLMMessage],
        tools: list[ToolDef] | None = None,
        signal: asyncio.Event | None = None,
        thinking_level: Literal[
            "off", "minimal", "low", "medium", "high", "xhigh", "max"
        ] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        selected_tools = tools
        request_metadata = dict(metadata or {})
        if self._stage == "list":
            selected_tools = [
                tool for tool in (tools or []) if tool.name == "coding_list_files"
            ]
        elif self._stage == "mutation":
            selected_tools = [
                tool for tool in (tools or []) if tool.name in self._MUTATION_TOOL_NAMES
            ]
        if self._stage != "open" and selected_tools:
            if self._delegate.provider_id == "glm":
                # Zhipu currently documents tool_choice=auto as the only
                # supported mode. Disable its default dynamic thinking for
                # these two mechanical bootstrap calls so it reaches the
                # sole visible action before exhausting the output budget.
                request_metadata["pi_agent_thinking"] = "disabled"
            else:
                request_metadata["pi_agent_tool_choice"] = "required"

        async for event in self._delegate.stream(
            system_prompt=system_prompt,
            messages=messages,
            tools=selected_tools,
            signal=signal,
            thinking_level=thinking_level,
            metadata=request_metadata,
        ):
            if isinstance(event, (ToolCallEvent, ToolCallEndEvent)):
                if self._stage == "list" and event.tool_call.name == "coding_list_files":
                    self._stage = "mutation"
                elif (
                    self._stage == "mutation"
                    and event.tool_call.name in self._MUTATION_TOOL_NAMES
                ):
                    self._stage = "open"
            yield event

    async def close(self) -> None:
        """The request runtime owns and closes the delegated client."""
        return None


class CodingSandboxAutomation:
    """Create/reuse, validate and freeze one Session-owned Sandbox operation."""

    def __init__(
        self,
        lifecycle: ManagedSandboxLifecycle,
        *,
        wait_timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self._lifecycle = lifecycle
        self._wait_timeout_seconds = wait_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def prepare(
        self,
        session_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ManagedSandboxOperationRecord:
        """Return a mutable ready operation, creating one when necessary."""
        self._raise_if_cancelled(cancelled)
        try:
            record = await self._lifecycle.latest_for_session(session_id)
            if (
                record is not None
                and record.status == "awaiting_approval"
                and not record.changed_paths
                and not record.deleted_paths
            ):
                # Older versions could freeze an empty diff because a syntax
                # check over an empty scripts directory exits successfully.
                # Such an artifact has nothing the user can approve; discard
                # it automatically so the Coding request gets a writable
                # operation instead of remaining permanently blocked.
                record = await self._lifecycle.discard(record.operation_id)
            if record is None or record.terminal:
                record = await self._lifecycle.start(session_id)
            if record.status in _CREATE_PENDING_STATUSES:
                record = await self._wait_while(
                    record,
                    _CREATE_PENDING_STATUSES,
                    cancelled=cancelled,
                    timeout_code="coding_sandbox_start_timeout",
                )
        except CodingSandboxAutomationError:
            raise
        except SandboxLifecycleError as exc:
            raise self._lifecycle_error(exc, "coding_sandbox_start_failed") from None

        if record.status not in _USABLE_STATUSES:
            code = (
                "coding_approval_pending"
                if record.status == "awaiting_approval"
                else "coding_sandbox_not_ready"
            )
            message = (
                "A frozen Coding artifact already awaits approval. Publish or discard it "
                "before starting another Coding request."
                if code == "coding_approval_pending"
                else "The Coding Sandbox is not ready for an automated request."
            )
            raise CodingSandboxAutomationError(
                code,
                message,
                operation_id=record.operation_id,
            )
        return record

    async def validate_and_freeze(
        self,
        operation_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> AutomatedCodingResult:
        """Run a fresh server-selected validation and freeze only on success."""
        self._raise_if_cancelled(cancelled, operation_id=operation_id)
        try:
            record = await self._lifecycle.get(operation_id)
            if record.status == "awaiting_approval":
                return self._result(record)
            if "validate" not in record.allowed_actions:
                raise CodingSandboxAutomationError(
                    "coding_validation_unavailable",
                    "The Coding Sandbox cannot run final validation in its current state.",
                    operation_id=operation_id,
                )
            record = await self._lifecycle.validate(operation_id)
            record = await self._wait_while(
                record,
                _VALIDATION_PENDING_STATUSES,
                cancelled=cancelled,
                timeout_code="coding_validation_timeout",
            )
            if record.status != "validated" or record.validation is None:
                raise CodingSandboxAutomationError(
                    "coding_validation_failed",
                    "Automatic Coding validation failed. The Sandbox remains available "
                    "for a repair request.",
                    operation_id=operation_id,
                )

            diff = record.diff
            if diff is None:
                diff = await self._lifecycle.diff(operation_id)
            if not diff.entries:
                raise CodingSandboxAutomationError(
                    "coding_no_changes",
                    "The Coding request produced no file changes. The Sandbox remains "
                    "writable for a repair request.",
                    operation_id=operation_id,
                )

            self._raise_if_cancelled(cancelled, operation_id=operation_id)
            record = await self._lifecycle.prepare_publish(operation_id)
            record = await self._wait_while(
                record,
                _FREEZE_PENDING_STATUSES,
                cancelled=cancelled,
                timeout_code="coding_freeze_timeout",
            )
        except CodingSandboxAutomationError:
            raise
        except SandboxLifecycleError as exc:
            raise self._lifecycle_error(exc, "coding_sandbox_finalize_failed") from None

        if record.status != "awaiting_approval":
            raise CodingSandboxAutomationError(
                "coding_freeze_failed",
                "The validated Coding artifact could not be frozen for approval.",
                operation_id=operation_id,
            )
        return self._result(record)

    async def run_with_no_change_retry(
        self,
        operation_id: str,
        run_attempt: Callable[[bool], Awaitable[_AttemptResult]],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> _AttemptResult:
        """Run once normally, then make one strict repair attempt for an empty diff."""
        for repair in (False, True):
            self._raise_if_cancelled(cancelled, operation_id=operation_id)
            result = await run_attempt(repair)
            self._raise_if_cancelled(cancelled, operation_id=operation_id)
            try:
                diff = await self._lifecycle.diff(operation_id)
            except SandboxLifecycleError as exc:
                raise self._lifecycle_error(exc, "coding_diff_failed") from None
            if diff.entries:
                return result
        raise CodingSandboxAutomationError(
            "coding_no_changes",
            "The Coding request produced no file changes after one automatic repair "
            "attempt. The Sandbox remains writable.",
            operation_id=operation_id,
        )

    async def cancel_if_possible(self, operation_id: str | None) -> None:
        if operation_id is None:
            return
        try:
            record = await self._lifecycle.get(operation_id)
            if "cancel" in record.allowed_actions:
                await self._lifecycle.cancel(operation_id)
        except Exception:
            # Cleanup is best-effort; the lifecycle shutdown/recovery path remains authoritative.
            return

    async def _wait_while(
        self,
        record: ManagedSandboxOperationRecord,
        pending_statuses: frozenset[str],
        *,
        cancelled: Callable[[], bool] | None,
        timeout_code: str,
    ) -> ManagedSandboxOperationRecord:
        deadline = asyncio.get_running_loop().time() + self._wait_timeout_seconds
        while record.status in pending_statuses:
            self._raise_if_cancelled(cancelled, operation_id=record.operation_id)
            if asyncio.get_running_loop().time() >= deadline:
                raise CodingSandboxAutomationError(
                    timeout_code,
                    "The automated Coding Sandbox step timed out.",
                    operation_id=record.operation_id,
                )
            await asyncio.sleep(self._poll_interval_seconds)
            record = await self._lifecycle.get(record.operation_id)
        return record

    @staticmethod
    def _raise_if_cancelled(
        cancelled: Callable[[], bool] | None,
        *,
        operation_id: str | None = None,
    ) -> None:
        if cancelled is not None and cancelled():
            raise CodingSandboxAutomationError(
                "coding_request_aborted",
                "The automated Coding request was stopped.",
                operation_id=operation_id,
            )

    @staticmethod
    def _lifecycle_error(
        exc: SandboxLifecycleError,
        fallback_code: str,
    ) -> CodingSandboxAutomationError:
        messages = {
            "sandbox_disabled": "Managed Coding Sandbox is disabled.",
            "sandbox_not_configured": "Managed Coding Sandbox credentials are not configured.",
            "operation_conflict": "Another Coding Sandbox operation is already active.",
            "project_invalid": "The Workspace cannot be prepared for Coding Sandbox execution.",
            "provider_error": "The managed Coding provider could not complete the request.",
            "validation_required": "A fresh successful validation is required before freezing.",
            "no_changes": "A Coding artifact must contain at least one file change.",
        }
        return CodingSandboxAutomationError(
            f"coding_{exc.code}" if exc.code in messages else fallback_code,
            messages.get(exc.code, "The automated Coding Sandbox workflow failed."),
            operation_id=exc.operation_id,
        )

    @staticmethod
    def _result(record: ManagedSandboxOperationRecord) -> AutomatedCodingResult:
        return AutomatedCodingResult(
            operation_id=record.operation_id,
            status=record.status,
            workspace_revision=record.workspace_revision,
            artifact_id=record.artifact_id,
        )


__all__ = [
    "AUTOMATED_CODING_PROMPT",
    "AUTOMATED_CODING_REPAIR_PROMPT",
    "AutomatedCodingResult",
    "CodingToolBootstrapModelClient",
    "CodingSandboxAutomation",
    "CodingSandboxAutomationError",
]
