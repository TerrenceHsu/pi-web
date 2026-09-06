"""Thin, optional Bash adapter; importing it does not register or enable it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from coding_agent_app.execution.models import ExecutionDenied
from coding_sandbox.bash import BashRequest, BashWorkspace
from coding_sandbox.operation import get_current_coding_workspace
from coding_sandbox.workspace_models import SandboxWorkspaceError

from ..agent.tooling import AgentTool, ToolResult, ToolUpdateCallback
from ..ai.messages import TextContent


class RunBashTool(AgentTool):
    name = "run_bash"
    label = "Bash · Local Docker"
    description = (
        "Run an exact Bash script in this approved local Docker task copy. "
        "Requires execution approval; never runs on the host or publishes Workspace changes."
    )
    parameters = BashRequest.model_json_schema()
    execution_mode = "sequential"

    def __init__(
        self, workspace_getter: Callable[[], object] = get_current_coding_workspace
    ) -> None:
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
            request = BashRequest.model_validate(args)
            workspace = self._workspace_getter()
            if not isinstance(workspace, BashWorkspace):
                raise SandboxWorkspaceError("bash_unavailable")
            result = await workspace.run_bash(request, signal=signal)
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(text=result.stdout + result.stderr)],
                details={**result.model_dump(mode="json"), "published": False},
                is_error=not result.succeeded,
            )
        except ValidationError:
            code = "invalid_bash_request"
        except (ExecutionDenied, SandboxWorkspaceError) as exc:
            code = exc.code
        except Exception:
            code = "bash_execution_failed"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            is_error=True,
            content=[TextContent(text=code)],
            details={"error_code": code},
        )
