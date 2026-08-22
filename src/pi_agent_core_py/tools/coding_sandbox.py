"""Thin AgentTool adapters for a request-scoped standalone coding workspace."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from coding_sandbox import SandboxOutputChunk
from coding_sandbox.operation import get_current_coding_workspace
from coding_sandbox.workspace_models import (
    CodingWorkspace,
    SandboxDiffResult,
    SandboxWorkspaceError,
)

from ..messages import TextContent
from . import AgentTool, ToolResult, ToolUpdateCallback

WorkspaceGetter = Callable[[], CodingWorkspace]
ToolHandler = Callable[
    [CodingWorkspace, str, dict[str, Any], asyncio.Event | None, ToolUpdateCallback | None],
    Awaitable[ToolResult],
]


class CodingSandboxTool(AgentTool):
    """Configured adapter; the standalone package owns all workspace semantics."""

    execution_mode = "sequential"

    def __init__(
        self,
        *,
        name: str,
        label: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
        workspace_getter: WorkspaceGetter,
    ) -> None:
        self.name = name
        self.label = label
        self.description = description
        self.parameters = parameters
        self._handler = handler
        self._workspace_getter = workspace_getter

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        try:
            workspace = self._workspace_getter()
            return await self._handler(workspace, tool_call_id, args, signal, on_update)
        except SandboxWorkspaceError as exc:
            return _error(
                tool_call_id,
                self.name,
                str(exc),
                error_code=exc.code,
                relative_path=exc.relative_path,
            )
        except Exception as exc:
            return _error(
                tool_call_id,
                self.name,
                "Coding sandbox tool failed.",
                error_code="unexpected_error",
                error_type=type(exc).__name__,
            )


async def _list_files(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    path = _string(args, "path", default=".")
    max_files = _integer(args, "max_files", default=1000, minimum=1, maximum=10_000)
    result = await workspace.list_files(path, max_files=max_files)
    lines = [f"{entry.path} ({entry.size} bytes)" for entry in result.files]
    text = "\n".join(lines) if lines else "Workspace contains no matching files."
    if result.truncated:
        text += "\n[results truncated]"
    return _success(tool_call_id, "coding_list_files", text, result.model_dump(mode="json"))


async def _read_file(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    path = _string(args, "path")
    max_bytes = _integer(
        args,
        "max_bytes",
        default=64 * 1024,
        minimum=1,
        maximum=5 * 1024 * 1024,
    )
    result = await workspace.read_file(path, max_bytes=max_bytes)
    return _success(
        tool_call_id,
        "coding_read_file",
        result.content,
        {"path": result.path, "size": result.size, "sha256": result.sha256},
    )


async def _search(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    query = _string(args, "query")
    path = _string(args, "path", default=".")
    case_sensitive = _boolean(args, "case_sensitive", default=True)
    max_matches = _integer(args, "max_matches", default=200, minimum=1, maximum=1000)
    result = await workspace.search(
        query,
        path=path,
        case_sensitive=case_sensitive,
        max_matches=max_matches,
    )
    lines = [f"{match.path}:{match.line}:{match.column}: {match.text}" for match in result.matches]
    text = "\n".join(lines) if lines else "No matches found."
    if result.truncated:
        text += "\n[results truncated]"
    return _success(tool_call_id, "coding_search", text, result.model_dump(mode="json"))


async def _write_file(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    path = _string(args, "path")
    content = _string(args, "content", allow_empty=True)
    overwrite = _boolean(args, "overwrite", default=True)
    result = await workspace.write_file(path, content, overwrite=overwrite)
    action = "Created" if result.created else "Updated"
    return _success(
        tool_call_id,
        "coding_write_file",
        f"{action} {result.path} ({result.size} bytes).",
        result.model_dump(mode="json"),
    )


async def _apply_patch(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    patch = _string(args, "patch")
    result = await workspace.apply_patch(patch)
    return _diff_result(tool_call_id, "coding_apply_patch", result)


async def _delete_file(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    result = await workspace.delete_file(_string(args, "path"))
    return _success(
        tool_call_id,
        "coding_delete_file",
        f"Deleted {result.path}.",
        result.model_dump(mode="json"),
    )


async def _run(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    signal: asyncio.Event | None,
    on_update: ToolUpdateCallback | None,
) -> ToolResult:
    raw_argv = args.get("argv")
    if (
        not isinstance(raw_argv, list)
        or not raw_argv
        or len(raw_argv) > 128
        or any(
            not isinstance(part, str) or not part or len(part.encode("utf-8")) > 8192
            for part in raw_argv
        )
    ):
        raise SandboxWorkspaceError("command_failed")
    argv = tuple(raw_argv)
    cwd = _string(args, "cwd", default=".")
    timeout = _optional_integer(args, "timeout_seconds", minimum=1, maximum=86_400)

    async def _output(chunk: SandboxOutputChunk) -> None:
        if on_update is None:
            return
        await on_update(
            ToolResult(
                tool_call_id=tool_call_id,
                name="coding_run",
                content=[TextContent(text=chunk.text)],
                details={
                    "partial": True,
                    "stream": chunk.stream,
                    "sequence": chunk.sequence,
                },
            )
        )

    result = await workspace.run(
        argv,
        cwd=cwd,
        timeout_seconds=timeout,
        on_output=_output if on_update is not None else None,
        signal=signal,
    )
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if not parts:
        parts.append("Command produced no output.")
    return ToolResult(
        tool_call_id=tool_call_id,
        name="coding_run",
        content=[TextContent(text="\n".join(parts))],
        is_error=not result.succeeded,
        details=result.model_dump(mode="json"),
    )


async def _diff(
    workspace: CodingWorkspace,
    tool_call_id: str,
    args: dict[str, Any],
    _signal: asyncio.Event | None,
    _on_update: ToolUpdateCallback | None,
) -> ToolResult:
    max_patch_bytes = _integer(
        args,
        "max_patch_bytes",
        default=256 * 1024,
        minimum=0,
        maximum=5 * 1024 * 1024,
    )
    return _diff_result(
        tool_call_id,
        "coding_diff",
        await workspace.diff(max_patch_bytes=max_patch_bytes),
    )


async def _validate_required_checks(
    workspace: CodingWorkspace,
    tool_call_id: str,
    _args: dict[str, Any],
    signal: asyncio.Event | None,
    on_update: ToolUpdateCallback | None,
) -> ToolResult:
    async def _output(chunk: SandboxOutputChunk) -> None:
        if on_update is None:
            return
        await on_update(
            ToolResult(
                tool_call_id=tool_call_id,
                name="coding_validate",
                content=[TextContent(text=chunk.text)],
                details={
                    "partial": True,
                    "command_id": chunk.command_id,
                    "stream": chunk.stream,
                    "sequence": chunk.sequence,
                },
            )
        )

    evidence = await workspace.validate_required_checks(
        on_output=_output if on_update is not None else None,
        signal=signal,
    )
    lines = [f"{check.check_id}: {check.status}" for check in evidence.checks]
    if evidence.passed:
        summary = f"Required validation passed at revision {evidence.workspace_revision}."
    else:
        summary = f"Required validation failed: {evidence.failure_code or 'validation_failed'}."
    if lines:
        summary += "\n" + "\n".join(lines)
    return ToolResult(
        tool_call_id=tool_call_id,
        name="coding_validate",
        content=[TextContent(text=summary)],
        is_error=not evidence.passed,
        details=evidence.model_dump(mode="json"),
    )


def _diff_result(tool_call_id: str, name: str, result: SandboxDiffResult) -> ToolResult:
    summary = [f"{entry.status}: {entry.path}" for entry in result.entries]
    text = "\n".join(summary) if summary else "Workspace has no changes."
    if result.patch:
        text += f"\n\n{result.patch}"
    if result.patch_truncated:
        text += "\n[patch truncated]"
    return _success(tool_call_id, name, text, result.model_dump(mode="json"))


def _success(
    tool_call_id: str,
    name: str,
    text: str,
    details: dict[str, Any],
) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        name=name,
        content=[TextContent(text=text)],
        details=details,
    )


def _error(
    tool_call_id: str,
    name: str,
    message: str,
    *,
    error_code: str,
    relative_path: str | None = None,
    error_type: str | None = None,
) -> ToolResult:
    details: dict[str, Any] = {"error_code": error_code}
    if relative_path is not None:
        details["relative_path"] = relative_path
    if error_type is not None:
        details["error_type"] = error_type
    return ToolResult(
        tool_call_id=tool_call_id,
        name=name,
        content=[TextContent(text=message)],
        is_error=True,
        details=details,
    )


def _string(
    args: dict[str, Any],
    name: str,
    *,
    default: str | None = None,
    allow_empty: bool = False,
) -> str:
    value = args.get(name, default)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SandboxWorkspaceError("protocol_error")
    return value


def _integer(
    args: dict[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SandboxWorkspaceError("resource_limit")
    return value


def _optional_integer(
    args: dict[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SandboxWorkspaceError("resource_limit")
    return value


def _boolean(args: dict[str, Any], name: str, *, default: bool) -> bool:
    value = args.get(name, default)
    if not isinstance(value, bool):
        raise SandboxWorkspaceError("protocol_error")
    return value


def create_coding_sandbox_tools(
    *,
    workspace_getter: WorkspaceGetter = get_current_coding_workspace,
) -> list[CodingSandboxTool]:
    """Create all eight tools without creating or owning a Sandbox."""
    specs: tuple[
        tuple[str, str, str, dict[str, Any], ToolHandler],
        ...,
    ] = (
        (
            "coding_list_files",
            "Coding: List Files",
            "List regular files inside the active coding sandbox workspace.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 10000},
                },
                "additionalProperties": False,
            },
            _list_files,
        ),
        (
            "coding_read_file",
            "Coding: Read File",
            "Read one UTF-8 file by a workspace-relative path.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            _read_file,
        ),
        (
            "coding_search",
            "Coding: Search",
            "Search UTF-8 workspace files for a literal string.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "case_sensitive": {"type": "boolean", "default": True},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            _search,
        ),
        (
            "coding_write_file",
            "Coding: Write File",
            "Create or atomically replace one UTF-8 workspace file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": True},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            _write_file,
        ),
        (
            "coding_apply_patch",
            "Coding: Apply Patch",
            "Apply an exact, bounded unified diff inside the workspace.",
            {
                "type": "object",
                "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
                "additionalProperties": False,
            },
            _apply_patch,
        ),
        (
            "coding_delete_file",
            "Coding: Delete File",
            "Delete one regular file by a workspace-relative path.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            _delete_file,
        ),
        (
            "coding_run",
            "Coding: Run",
            "Run an argv command in the isolated workspace without a shell input API.",
            {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "cwd": {"type": "string", "default": "."},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            _run,
        ),
        (
            "coding_diff",
            "Coding: Diff",
            "Show added, modified and deleted files versus the operation baseline.",
            {
                "type": "object",
                "properties": {
                    "max_patch_bytes": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
            _diff,
        ),
    )
    return [
        CodingSandboxTool(
            name=name,
            label=label,
            description=description,
            parameters=parameters,
            handler=handler,
            workspace_getter=workspace_getter,
        )
        for name, label, description, parameters, handler in specs
    ]


def create_coding_validation_tool(
    *,
    workspace_getter: WorkspaceGetter = get_current_coding_workspace,
) -> CodingSandboxTool:
    """Create the trigger for the immutable server-selected validation plan."""
    return CodingSandboxTool(
        name="coding_validate",
        label="Coding: Validate",
        description=(
            "Run every server-configured required check for the current workspace. "
            "The command list cannot be supplied or changed by the model."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_validate_required_checks,
        workspace_getter=workspace_getter,
    )


__all__ = [
    "CodingSandboxTool",
    "WorkspaceGetter",
    "create_coding_sandbox_tools",
    "create_coding_validation_tool",
]
