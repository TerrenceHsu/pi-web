"""Provider, Skill, MCP, Workspace and Prompt composition comparison."""

from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent_app.core import CodingAgentResourceSnapshot
from pi_agent_core_py.ai.messages import TextContent, Usage
from pi_agent_core_py.ai.stream_events import DoneEvent, StreamEvent, TextDeltaEvent
from pi_agent_core_py.skills import Skill
from pi_agent_core_py.tools import AgentTool, ToolResult, ToolUpdateCallback

from ..harness import EvalSuite, LocalCodingAgentHarness, LocalCodingAgentHarnessConfig
from ..judges import ExactTextJudge, ResourceAssemblyJudge, StopReasonJudge
from ..models import EvalCase, PromptStep


class _LocalLookupTool(AgentTool):
    name = "mcp__local__lookup"
    label = "Local lookup"
    description = "Return a deterministic local lookup result."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, object],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        del args, signal, on_update
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="local")],
        )


def _baseline_resources(
    _workspace: Path,
    _case: EvalCase,
) -> CodingAgentResourceSnapshot:
    return CodingAgentResourceSnapshot()


def _candidate_resources(
    _workspace: Path,
    _case: EvalCase,
) -> CodingAgentResourceSnapshot:
    return CodingAgentResourceSnapshot(
        skills=(
            Skill(
                name="local_guidance",
                description="Keep all evaluation activity local.",
                prompt="USE LOCAL RESOURCES ONLY",
            ),
        ),
        tools=(_LocalLookupTool(),),
        mcp_tool_names=("mcp__local__lookup",),
        context_fragments=("LOCAL RESOURCE CONTEXT",),
    )


def _scripts(_case: EvalCase) -> tuple[tuple[StreamEvent, ...], ...]:
    return (
        (
            TextDeltaEvent(delta="READY"),
            DoneEvent(
                stop_reason="stop",
                usage=Usage(input=12, output=1, total_tokens=13),
            ),
        ),
    )


def build_suite() -> EvalSuite:
    case = EvalCase(
        id="unified-resource-composition",
        steps=(PromptStep("Confirm that product resources are ready."),),
        system_prompt_suffix="CASE PROMPT SUFFIX",
        tags=("provider", "skills", "mcp", "workspace", "prompt"),
    )
    baseline = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(
            name="without-product-resources",
            scripts=_scripts,
            resources=_baseline_resources,
        )
    )
    candidate = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(
            name="with-product-resources",
            scripts=_scripts,
            resources=_candidate_resources,
        )
    )
    return EvalSuite(
        name="resource-composition",
        cases=(case,),
        baseline=baseline,
        candidates=(candidate,),
        judges=(
            ExactTextJudge("READY"),
            StopReasonJudge(),
            ResourceAssemblyJudge(
                required_skills=("local_guidance",),
                required_tools=("mcp__local__lookup",),
                required_mcp_tools=("mcp__local__lookup",),
                required_prompt_fragments=(
                    "USE LOCAL RESOURCES ONLY",
                    "LOCAL RESOURCE CONTEXT",
                    "CASE PROMPT SUFFIX",
                ),
            ),
        ),
        minimum_candidate_score=1.0,
    )


__all__ = ["build_suite"]
