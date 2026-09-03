from __future__ import annotations

import json

import pytest

from evals.comparison import summarize_results
from evals.harness import run_suite
from evals.suites.resource_composition import build_suite as resource_suite
from evals.suites.session_reload import build_suite as reload_suite
from evals.suites.telemetry_safety import build_suite as telemetry_suite
from evals.suites.tool_lifecycle import build_suite as tool_suite


@pytest.mark.asyncio
async def test_tool_suite_captures_trace_usage_and_workspace(tmp_path) -> None:
    suite = tool_suite()
    results = await run_suite(suite, temp_root=tmp_path)

    candidate = results[1]
    observation = candidate.observation
    assert candidate.score == 1
    assert [call.name for call in observation.tool_calls] == ["write_artifact"]
    assert observation.workspace_text == {
        "artifacts/result.txt": "local artifact"
    }
    assert observation.usage.total_tokens == 30
    assert observation.usage.tool_calls == 1


@pytest.mark.asyncio
async def test_resource_suite_measures_product_composition_lift(tmp_path) -> None:
    suite = resource_suite()
    results = await run_suite(suite, temp_root=tmp_path)
    report = summarize_results((suite,), results)

    baseline, candidate = results
    assert baseline.score is not None and baseline.score < 1
    assert candidate.score == 1
    assert candidate.observation.resource_skills == ("local_guidance",)
    assert "LOCAL RESOURCE CONTEXT" in "\n".join(
        candidate.observation.system_prompts
    )
    assert report.suites[0].comparisons[0].pass_rate_lift == 1


@pytest.mark.asyncio
async def test_reload_suite_recreates_session_from_sqlite(tmp_path) -> None:
    suite = reload_suite()
    results = await run_suite(suite, temp_root=tmp_path)

    candidate = results[1]
    assert candidate.score == 1
    assert candidate.observation.response == "SECOND"
    assert [
        (item.before_message_count, item.after_message_count)
        for item in candidate.observation.reload_checks
    ] == [(2, 2)]
    assert candidate.observation.message_counts == (2, 2, 4)


@pytest.mark.asyncio
async def test_telemetry_suite_keeps_content_out_of_spans(tmp_path) -> None:
    suite = telemetry_suite()
    results = await run_suite(suite, temp_root=tmp_path)

    candidate = results[1]
    serialized = json.dumps(candidate.observation.telemetry, ensure_ascii=False)
    assert candidate.score == 1
    assert "eval-secret-7f6d0d8a" not in serialized
    assert {span["name"] for span in candidate.observation.telemetry} == {
        "coding_agent.request",
        "agent.run",
    }
