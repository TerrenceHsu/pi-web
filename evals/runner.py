"""Command-line orchestration for built-in local eval suites."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .artifacts import EvalArtifactWriter, default_artifact_directory
from .comparison import EvalReport, summarize_results
from .harness import EvalSuite, run_suite
from .models import EvalScoredObservation
from .reporter import format_report
from .suites import built_in_suites


@dataclass(frozen=True, slots=True)
class EvalRunOutcome:
    report: EvalReport
    results: tuple[EvalScoredObservation, ...]
    artifact_directory: Path


async def run_evaluations(
    suites: Sequence[EvalSuite],
    *,
    artifact_directory: Path,
    include_content: bool = False,
) -> EvalRunOutcome:
    selected = tuple(suites)
    writer = EvalArtifactWriter(artifact_directory, include_content=include_content)
    writer.initialize(selected)
    results: list[EvalScoredObservation] = []
    for suite in selected:
        suite_results = await run_suite(suite)
        for result in suite_results:
            writer.append(result)
        results.extend(suite_results)
    report = summarize_results(selected, results)
    writer.write_report(report)
    return EvalRunOutcome(report, tuple(results), artifact_directory)


def _parser(suite_names: Sequence[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run deterministic coding-agent evaluations without network access.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=tuple(suite_names),
        help="Run only the named built-in suite; may be repeated.",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        help="Write this run to an explicit local artifact directory.",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="Persist prompts, responses, tool payloads and Workspace text.",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Return a failure exit code when a candidate misses its suite threshold.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List built-in suites and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    suites = built_in_suites()
    by_name = {suite.name: suite for suite in suites}
    parser = _parser(tuple(by_name))
    args = parser.parse_args(argv)
    if args.list:
        for name in by_name:
            print(name)
        return 0
    selected_names = args.suite or list(by_name)
    selected = tuple(by_name[name] for name in selected_names)
    artifact_directory = args.artifacts_dir or default_artifact_directory()
    outcome = asyncio.run(
        run_evaluations(
            selected,
            artifact_directory=artifact_directory,
            include_content=bool(args.include_content),
        )
    )
    print(format_report(outcome.report))
    print(f"Artifacts: {outcome.artifact_directory.resolve()}")
    if args.gate and not outcome.report.candidate_gate_passed:
        return 1
    return 0


__all__ = ["EvalRunOutcome", "main", "run_evaluations"]
