from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.runner import main, run_evaluations
from evals.suites.telemetry_safety import build_suite
from evals.suites.tool_lifecycle import build_suite as build_tool_suite


@pytest.mark.asyncio
async def test_artifacts_are_content_free_by_default(tmp_path: Path) -> None:
    suite = build_suite()
    output = tmp_path / "safe"

    outcome = await run_evaluations((suite,), artifact_directory=output)

    runs = (output / "runs.jsonl").read_text(encoding="utf-8")
    assert outcome.report.candidate_gate_passed is True
    assert "eval-secret-7f6d0d8a" not in runs
    assert '"response"' not in runs
    assert (output / "manifest.json").is_file()
    assert (output / "summary.json").is_file()
    assert (output / "summary.md").is_file()


@pytest.mark.asyncio
async def test_content_artifacts_require_explicit_opt_in(tmp_path: Path) -> None:
    output = tmp_path / "detailed"

    await run_evaluations(
        (build_suite(),),
        artifact_directory=output,
        include_content=True,
    )

    runs = (output / "runs.jsonl").read_text(encoding="utf-8")
    assert "eval-secret-7f6d0d8a" in runs
    assert '"response"' in runs


def test_cli_lists_built_in_suites(capsys) -> None:
    assert main(["--list"]) == 0

    output = capsys.readouterr().out
    assert "local-smoke" in output
    assert "session-reload" in output


@pytest.mark.asyncio
async def test_default_workspace_artifact_records_count_not_paths(tmp_path: Path) -> None:
    output = tmp_path / "workspace-safe"

    await run_evaluations((build_tool_suite(),), artifact_directory=output)

    records = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all("workspace_files" not in record for record in records)
    assert all(record["workspace_file_count"] == 1 for record in records)
    assert "artifacts/result.txt" not in str(records)


@pytest.mark.asyncio
async def test_artifact_directory_cannot_mix_multiple_runs(tmp_path: Path) -> None:
    output = tmp_path / "single-run"
    suite = build_suite()

    await run_evaluations((suite,), artifact_directory=output)

    with pytest.raises(FileExistsError, match="already contains a run"):
        await run_evaluations((suite,), artifact_directory=output)
