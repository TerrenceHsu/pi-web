"""Durable Session reconstruction evaluation."""

from __future__ import annotations

from pi_agent_core_py.ai.messages import Usage
from pi_agent_core_py.ai.stream_events import DoneEvent, StreamEvent, TextDeltaEvent

from ..harness import EvalSuite, LocalCodingAgentHarness, LocalCodingAgentHarnessConfig
from ..judges import ExactTextJudge, SessionReloadJudge, StopReasonJudge
from ..models import EvalCase, PromptStep, ReloadStep


def _scripts(_case: EvalCase) -> tuple[tuple[StreamEvent, ...], ...]:
    return (
        (
            TextDeltaEvent(delta="FIRST"),
            DoneEvent(
                stop_reason="stop",
                usage=Usage(input=4, output=1, total_tokens=5),
            ),
        ),
        (
            TextDeltaEvent(delta="SECOND"),
            DoneEvent(
                stop_reason="stop",
                usage=Usage(input=8, output=1, total_tokens=9),
            ),
        ),
    )


def build_suite() -> EvalSuite:
    case = EvalCase(
        id="sqlite-session-reload",
        steps=(
            PromptStep("Store the first turn."),
            ReloadStep(),
            PromptStep("Continue after reload."),
        ),
        tags=("session", "sqlite", "reload"),
    )
    baseline = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(name="reload-baseline", scripts=_scripts)
    )
    candidate = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(name="reload-candidate", scripts=_scripts)
    )
    return EvalSuite(
        name="session-reload",
        cases=(case,),
        baseline=baseline,
        candidates=(candidate,),
        judges=(
            ExactTextJudge("SECOND"),
            StopReasonJudge(),
            SessionReloadJudge(),
        ),
    )


__all__ = ["build_suite"]
