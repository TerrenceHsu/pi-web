"""Content-free Telemetry projection evaluation."""

from __future__ import annotations

from pi_agent_core_py.ai.messages import Usage
from pi_agent_core_py.ai.stream_events import DoneEvent, StreamEvent, TextDeltaEvent

from ..harness import EvalSuite, LocalCodingAgentHarness, LocalCodingAgentHarnessConfig
from ..judges import ExactTextJudge, StopReasonJudge, TelemetryPrivacyJudge
from ..models import EvalCase, PromptStep

_SECRET = "eval-secret-7f6d0d8a"


def _scripts(_case: EvalCase) -> tuple[tuple[StreamEvent, ...], ...]:
    return (
        (
            TextDeltaEvent(delta="SAFE"),
            DoneEvent(
                stop_reason="stop",
                usage=Usage(input=6, output=1, total_tokens=7),
            ),
        ),
    )


def build_suite() -> EvalSuite:
    case = EvalCase(
        id="telemetry-content-boundary",
        steps=(PromptStep(f"Do not persist this marker: {_SECRET}"),),
        system_prompt_suffix=f"private suffix {_SECRET}",
        tags=("telemetry", "privacy"),
    )
    baseline = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(name="telemetry-baseline", scripts=_scripts)
    )
    candidate = LocalCodingAgentHarness(
        LocalCodingAgentHarnessConfig(name="telemetry-candidate", scripts=_scripts)
    )
    return EvalSuite(
        name="telemetry-safety",
        cases=(case,),
        baseline=baseline,
        candidates=(candidate,),
        judges=(
            ExactTextJudge("SAFE"),
            StopReasonJudge(),
            TelemetryPrivacyJudge((_SECRET,)),
        ),
    )


__all__ = ["build_suite"]
