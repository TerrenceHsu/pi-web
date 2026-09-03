from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from evals.comparison import summarize_results
from evals.harness import EvalRunContext, EvalSuite, derive_group_key
from evals.judges import ContainsTextJudge, ExactTextJudge, JsonSchemaJudge
from evals.models import (
    EvalCase,
    EvalJudgeResult,
    EvalObservation,
    EvalScoredObservation,
    EvalUsage,
    PromptStep,
    ReloadStep,
)


@dataclass
class _Harness:
    name: str

    async def run(
        self,
        case: EvalCase,
        context: EvalRunContext,
    ) -> EvalObservation:
        raise AssertionError((case, context))


def _observation(
    *,
    harness: str,
    score: float | None = 1.0,
) -> EvalScoredObservation:
    observation = EvalObservation(
        eval_set="suite",
        case_id="case",
        harness=harness,
        repetition=1,
        group_key='["case",1]',
        response="secret response",
        stop_reason="stop",
        messages=({"role": "user", "content": "secret prompt"},),
        tool_calls=(),
        tool_results=(),
        system_prompts=("secret system",),
        workspace_files=(),
        workspace_text={},
        resource_skills=(),
        resource_tools=(),
        resource_mcp_tools=(),
        context_fragments=("secret context",),
        workspace_bound=True,
        telemetry=(),
        reload_checks=(),
        message_counts=(2,),
        usage=EvalUsage(total_tokens=10),
        total_ms=20,
    )
    judgments = (
        ()
        if score is None
        else (EvalJudgeResult("judge", score, "deterministic result"),)
    )
    return EvalScoredObservation(observation, judgments)


def test_eval_case_requires_a_prompt() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        EvalCase(id="invalid", steps=())
    with pytest.raises(ValueError, match="prompt"):
        EvalCase(id="invalid", steps=(ReloadStep(),))


def test_eval_suite_requires_unique_case_ids() -> None:
    case = EvalCase(id="duplicate", steps=(PromptStep("go"),))

    with pytest.raises(ValueError, match="case ids must be unique"):
        EvalSuite(
            name="suite",
            cases=(case, case),
            baseline=_Harness("baseline"),
            candidates=(_Harness("candidate"),),
            judges=(ExactTextJudge("unused"),),
        )


def test_default_record_omits_evaluated_content() -> None:
    result = _observation(harness="candidate")

    safe = result.to_record()
    detailed = result.to_record(include_content=True)

    assert "response" not in safe
    assert "messages" not in safe
    assert "system_prompts" not in safe
    assert "context_fragments" not in safe
    assert "workspace_files" not in safe
    assert "rationale" not in str(safe["judgments"])
    assert detailed["response"] == "secret response"
    assert "secret prompt" in str(detailed["messages"])


def test_text_and_json_schema_judges_are_deterministic() -> None:
    case = EvalCase(id="case", steps=(PromptStep("go"),))
    base = _observation(harness="candidate").observation
    observation = replace(base, response='{"status":"ok"}')

    assert ContainsTextJudge("STATUS", case_sensitive=False).evaluate(
        case, observation
    ).score == 1
    assert JsonSchemaJudge(
        {
            "type": "object",
            "properties": {"status": {"const": "ok"}},
            "required": ["status"],
            "additionalProperties": False,
        }
    ).evaluate(case, observation).score == 1


def test_group_key_is_stable_and_json_safe() -> None:
    case = EvalCase(id='quote-"-safe', steps=(PromptStep("go"),))

    assert derive_group_key(case, 2) == '["quote-\\"-safe",2]'


def test_comparison_reports_candidate_lift_and_gate() -> None:
    suite = EvalSuite(
        name="suite",
        cases=(EvalCase(id="case", steps=(PromptStep("go"),)),),
        baseline=_Harness("baseline"),
        candidates=(_Harness("candidate"),),
        judges=(ExactTextJudge("unused"),),
    )

    report = summarize_results(
        (suite,),
        (
            _observation(harness="baseline", score=0),
            _observation(harness="candidate", score=1),
        ),
    )

    comparison = report.suites[0].comparisons[0]
    assert comparison.pass_rate_lift == 1
    assert comparison.candidate_wins == 1
    assert comparison.total_tokens.mean_delta == 0
    assert report.candidate_gate_passed is True


def test_missing_candidate_is_diagnostic_and_gate_failure() -> None:
    suite = EvalSuite(
        name="suite",
        cases=(EvalCase(id="case", steps=(PromptStep("go"),)),),
        baseline=_Harness("baseline"),
        candidates=(_Harness("candidate"),),
        judges=(ExactTextJudge("unused"),),
    )

    report = summarize_results((suite,), (_observation(harness="baseline"),))

    assert report.candidate_gate_passed is False
    assert any(
        item.harness == "candidate" and item.reason == "missing-observation"
        for item in report.diagnostics
    )
