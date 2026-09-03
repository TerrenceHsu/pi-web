"""Basic end-to-end local Agent response evaluation."""

from __future__ import annotations

from pi_agent_core_py.ai.messages import Usage
from pi_agent_core_py.ai.stream_events import DoneEvent, StreamEvent, TextDeltaEvent

from ..harness import EvalSuite, LocalCodingAgentHarness, LocalCodingAgentHarnessConfig
from ..judges import ExactTextJudge, StopReasonJudge
from ..models import EvalCase, PromptStep


def _scripts(_case: EvalCase) -> tuple[tuple[StreamEvent, ...], ...]:
    return (
        (
            TextDeltaEvent(delta="Paris"),
            DoneEvent(
                stop_reason="stop",
                usage=Usage(input=8, output=1, total_tokens=9),
            ),
        ),
    )


def build_suite() -> EvalSuite:
    case = EvalCase(
        id="capital-smoke",
        steps=(PromptStep("Name the capital of France. Return only the city."),),
        tags=("smoke",),
    )
    baseline = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(name="minimal-session", scripts=_scripts)
    )
    candidate = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(name="product-session", scripts=_scripts)
    )
    return EvalSuite(
        name="local-smoke",
        cases=(case,),
        baseline=baseline,
        candidates=(candidate,),
        judges=(ExactTextJudge("Paris"), StopReasonJudge()),
    )


__all__ = ["build_suite"]
