"""Tool execution and temporary Workspace artifact evaluation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent_app.core import CodingAgentResourceSnapshot
from pi_agent_core_py.ai.messages import TextContent, ToolCall, Usage
from pi_agent_core_py.ai.stream_events import (
    DoneEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolCallEvent,
)
from pi_agent_core_py.tools import AgentTool, ToolResult, ToolUpdateCallback

from ..harness import EvalSuite, LocalCodingAgentHarness, LocalCodingAgentHarnessConfig
from ..judges import (
    ExactTextJudge,
    NoToolErrorsJudge,
    StopReasonJudge,
    ToolSequenceJudge,
    WorkspaceFilesJudge,
)
from ..models import EvalCase, PromptStep


class _WriteArtifactTool(AgentTool):
    name = "write_artifact"
    label = "Write local artifact"
    description = "Write deterministic text into the isolated eval Workspace."
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, object],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        del signal, on_update
        directory = self._workspace / "artifacts"
        directory.mkdir()
        content = str(args["content"])
        (directory / "result.txt").write_text(content, encoding="utf-8")
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="written")],
        )


def _resources(workspace: Path, _case: EvalCase) -> CodingAgentResourceSnapshot:
    return CodingAgentResourceSnapshot(tools=(_WriteArtifactTool(workspace),))


def _scripts(_case: EvalCase) -> tuple[tuple[StreamEvent, ...], ...]:
    return (
        (
            ToolCallEvent(
                tool_call=ToolCall(
                    id="write-1",
                    name="write_artifact",
                    arguments={"content": "local artifact"},
                )
            ),
            DoneEvent(
                stop_reason="tool_use",
                usage=Usage(input=10, output=4, total_tokens=14),
            ),
        ),
        (
            TextDeltaEvent(delta="WRITTEN"),
            DoneEvent(
                stop_reason="stop",
                usage=Usage(input=15, output=1, total_tokens=16),
            ),
        ),
    )


def build_suite() -> EvalSuite:
    case = EvalCase(
        id="write-workspace-artifact",
        steps=(PromptStep("Write the requested local artifact."),),
        tags=("tools", "workspace"),
    )
    baseline = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(
            name="tool-baseline",
            scripts=_scripts,
            resources=_resources,
        )
    )
    candidate = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(
            name="tool-candidate",
            scripts=_scripts,
            resources=_resources,
        )
    )
    return EvalSuite(
        name="tool-lifecycle",
        cases=(case,),
        baseline=baseline,
        candidates=(candidate,),
        judges=(
            ExactTextJudge("WRITTEN"),
            StopReasonJudge(),
            ToolSequenceJudge(("write_artifact",)),
            NoToolErrorsJudge(),
            WorkspaceFilesJudge(("artifacts/result.txt",)),
        ),
    )


__all__ = ["build_suite"]
