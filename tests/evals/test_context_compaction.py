from __future__ import annotations

from pathlib import Path

from evals.harness import run_suite
from evals.runner import run_evaluations
from evals.suites import built_in_suites
from evals.suites.context_compaction import build_suite


async def test_context_compaction_suite_uses_standard_offline_gate(tmp_path: Path) -> None:
    suite = build_suite()
    results = await run_suite(suite, temp_root=tmp_path)
    assert len(results) == 10
    assert all(result.observation.infrastructure_ok for result in results)
    assert all(result.score == 1.0 for result in results)
    assert "context-compaction" in {item.name for item in built_in_suites()}


async def test_context_compaction_artifacts_exclude_facts_and_tool_bodies(tmp_path: Path) -> None:
    output = tmp_path / "artifacts"
    outcome = await run_evaluations((build_suite(),), artifact_directory=output)
    assert outcome.report.candidate_gate_passed
    raw = (output / "runs.jsonl").read_text(encoding="utf-8")
    assert "Execute Python only after" not in raw
    assert "UnicodeError" not in raw
    assert "Investigation detail" not in raw
    assert "D:\\" not in raw
    assert all(result.observation.usage.provider == "offline" for result in outcome.results)
