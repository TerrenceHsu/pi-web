"""Contract tests for the eight thin AgentTool adapters."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coding_sandbox import (
    SandboxCommandResult,
    SandboxDeleteResult,
    SandboxDiffEntry,
    SandboxDiffResult,
    SandboxFileEntry,
    SandboxFileList,
    SandboxFileRead,
    SandboxOutputCallback,
    SandboxOutputChunk,
    SandboxSearchMatch,
    SandboxSearchResult,
    SandboxWriteResult,
)
from pi_agent_core_py.tools import ToolResult, create_coding_sandbox_tools


class StubWorkspace:
    operation_id = "stub-operation"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def list_files(
        self,
        path: str = ".",
        *,
        max_files: int = 1000,
    ) -> SandboxFileList:
        self.calls.append(("list", (path, max_files)))
        return SandboxFileList(
            root=path,
            files=(SandboxFileEntry(path="a.py", size=1, sha256="a" * 64),),
        )

    async def read_file(
        self,
        path: str,
        *,
        max_bytes: int = 64 * 1024,
    ) -> SandboxFileRead:
        self.calls.append(("read", (path, max_bytes)))
        return SandboxFileRead(
            path=path,
            content="x",
            size=1,
            sha256="a" * 64,
        )

    async def search(
        self,
        query: str,
        *,
        path: str = ".",
        case_sensitive: bool = True,
        max_matches: int = 200,
    ) -> SandboxSearchResult:
        self.calls.append(
            ("search", (query, path, case_sensitive, max_matches))
        )
        return SandboxSearchResult(
            query=query,
            root=path,
            matches=(
                SandboxSearchMatch(
                    path="a.py",
                    line=1,
                    column=1,
                    text="x",
                ),
            ),
        )

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = True,
    ) -> SandboxWriteResult:
        self.calls.append(("write", (path, content, overwrite)))
        return SandboxWriteResult(
            path=path,
            size=len(content.encode()),
            sha256="b" * 64,
            created=True,
        )

    async def apply_patch(self, patch: str) -> SandboxDiffResult:
        self.calls.append(("patch", patch))
        return self._diff_result()

    async def delete_file(self, path: str) -> SandboxDeleteResult:
        self.calls.append(("delete", path))
        return SandboxDeleteResult(path=path)

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: int | None = None,
        on_output: SandboxOutputCallback | None = None,
        signal: asyncio.Event | None = None,
    ) -> SandboxCommandResult:
        self.calls.append(("run", (argv, cwd, timeout_seconds, signal)))
        if on_output is not None:
            await on_output(
                SandboxOutputChunk(
                    command_id="run",
                    sequence=0,
                    stream="stdout",
                    text="partial",
                )
            )
        return SandboxCommandResult(
            command_id="run",
            exit_code=2,
            stdout="terminal",
            stderr="failure",
            started_at_ms=1,
            finished_at_ms=2,
        )

    async def diff(self, *, max_patch_bytes: int = 256 * 1024) -> SandboxDiffResult:
        self.calls.append(("diff", max_patch_bytes))
        return self._diff_result()

    @staticmethod
    def _diff_result() -> SandboxDiffResult:
        return SandboxDiffResult(
            entries=(
                SandboxDiffEntry(
                    path="a.py",
                    status="modified",
                    before_sha256="a" * 64,
                    after_sha256="b" * 64,
                ),
            ),
            patch="--- a/a.py\n+++ b/a.py\n",
        )


def _tools(workspace: StubWorkspace) -> dict[str, Any]:
    return {
        tool.name: tool
        for tool in create_coding_sandbox_tools(workspace_getter=lambda: workspace)
    }


def test_factory_defines_exact_sequential_tool_surface() -> None:
    tools = create_coding_sandbox_tools(workspace_getter=StubWorkspace)

    assert [tool.name for tool in tools] == [
        "coding_list_files",
        "coding_read_file",
        "coding_search",
        "coding_write_file",
        "coding_apply_patch",
        "coding_delete_file",
        "coding_run",
        "coding_diff",
    ]
    assert all(tool.execution_mode == "sequential" for tool in tools)
    assert all(tool.parameters["additionalProperties"] is False for tool in tools)


@pytest.mark.asyncio
async def test_tools_map_all_workspace_operations() -> None:
    workspace = StubWorkspace()
    tools = _tools(workspace)

    results = [
        await tools["coding_list_files"].execute("1", {"path": "src", "max_files": 5}),
        await tools["coding_read_file"].execute("2", {"path": "a.py"}),
        await tools["coding_search"].execute("3", {"query": "x"}),
        await tools["coding_write_file"].execute(
            "4", {"path": "b.py", "content": "y", "overwrite": False}
        ),
        await tools["coding_apply_patch"].execute("5", {"patch": "patch"}),
        await tools["coding_delete_file"].execute("6", {"path": "b.py"}),
        await tools["coding_diff"].execute("7", {"max_patch_bytes": 123}),
    ]

    assert all(not result.is_error for result in results)
    assert [call[0] for call in workspace.calls] == [
        "list",
        "read",
        "search",
        "write",
        "patch",
        "delete",
        "diff",
    ]
    assert results[0].tool_call_id == "1"
    assert results[0].name == "coding_list_files"


@pytest.mark.asyncio
async def test_run_emits_partial_update_and_returns_nonzero_as_tool_error() -> None:
    workspace = StubWorkspace()
    run_tool = _tools(workspace)["coding_run"]
    updates: list[ToolResult] = []
    signal = asyncio.Event()

    async def on_update(result: ToolResult) -> None:
        updates.append(result)

    result = await run_tool.execute(
        "run-call",
        {"argv": ["python3", "-V"], "cwd": "src", "timeout_seconds": 10},
        signal=signal,
        on_update=on_update,
    )

    assert len(updates) == 1
    assert updates[0].details == {
        "partial": True,
        "stream": "stdout",
        "sequence": 0,
    }
    assert updates[0].content[0].text == "partial"
    assert result.is_error is True
    assert "terminal" in result.content[0].text
    assert "failure" in result.content[0].text
    assert workspace.calls[0] == (
        "run",
        (("python3", "-V"), "src", 10, signal),
    )


@pytest.mark.asyncio
async def test_default_getter_requires_active_request_scope() -> None:
    tool = create_coding_sandbox_tools()[0]

    result = await tool.execute("call", {})

    assert result.is_error is True
    assert result.details["error_code"] == "no_active_operation"
    assert result.content[0].text == "No coding sandbox operation is active."


@pytest.mark.asyncio
async def test_invalid_arguments_are_safe_terminal_errors() -> None:
    workspace = StubWorkspace()
    run_tool = _tools(workspace)["coding_run"]

    result = await run_tool.execute("call", {"argv": "python3 -V"})

    assert result.is_error is True
    assert result.details["error_code"] == "command_failed"
    assert workspace.calls == []


@pytest.mark.asyncio
async def test_unexpected_exception_does_not_leak_provider_text() -> None:
    def broken_getter() -> StubWorkspace:
        raise RuntimeError("secret provider response")

    tool = create_coding_sandbox_tools(workspace_getter=broken_getter)[0]

    result = await tool.execute("call", {})

    assert result.is_error is True
    assert result.content[0].text == "Coding sandbox tool failed."
    assert "secret provider response" not in repr(result)
    assert result.details == {
        "error_code": "unexpected_error",
        "error_type": "RuntimeError",
    }
