"""Paired baseline/candidate aggregation for local eval observations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .harness import EvalSuite, derive_group_key
from .models import EvalScoredObservation, JsonValue


@dataclass(frozen=True, slots=True)
class EvalMetricComparison:
    total_pairs: int
    eligible_pairs: int
    baseline_mean: float | None
    candidate_mean: float | None
    mean_delta: float | None

    def to_record(self) -> dict[str, JsonValue]:
        return {
            "total_pairs": self.total_pairs,
            "eligible_pairs": self.eligible_pairs,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "mean_delta": self.mean_delta,
        }


@dataclass(frozen=True, slots=True)
class EvalComparison:
    baseline: str
    candidate: str
    total_pairs: int
    eligible_pairs: int
    baseline_pass_rate: float | None
    candidate_pass_rate: float | None
    pass_rate_lift: float | None
    baseline_wins: int
    candidate_wins: int
    ties: int
    total_tokens: EvalMetricComparison
    total_ms: EvalMetricComparison
    estimated_cost: EvalMetricComparison

    def to_record(self) -> dict[str, JsonValue]:
        return {
            "baseline": self.baseline,
            "candidate": self.candidate,
            "total_pairs": self.total_pairs,
            "eligible_pairs": self.eligible_pairs,
            "baseline_pass_rate": self.baseline_pass_rate,
            "candidate_pass_rate": self.candidate_pass_rate,
            "pass_rate_lift": self.pass_rate_lift,
            "baseline_wins": self.baseline_wins,
            "candidate_wins": self.candidate_wins,
            "ties": self.ties,
            "total_tokens": self.total_tokens.to_record(),
            "total_ms": self.total_ms.to_record(),
            "estimated_cost": self.estimated_cost.to_record(),
        }


@dataclass(frozen=True, slots=True)
class EvalDiagnostic:
    eval_set: str
    group_key: str
    harness: str
    reason: str

    def to_record(self) -> dict[str, JsonValue]:
        return {
            "eval_set": self.eval_set,
            "group_key": self.group_key,
            "harness": self.harness,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class EvalSuiteReport:
    name: str
    comparisons: tuple[EvalComparison, ...]

    def to_record(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "comparisons": [comparison.to_record() for comparison in self.comparisons],
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    suites: tuple[EvalSuiteReport, ...]
    diagnostics: tuple[EvalDiagnostic, ...]
    candidate_gate_passed: bool

    def to_record(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "suites": [suite.to_record() for suite in self.suites],
            "diagnostics": [diagnostic.to_record() for diagnostic in self.diagnostics],
            "candidate_gate_passed": self.candidate_gate_passed,
        }


def summarize_results(
    suites: Sequence[EvalSuite],
    results: Sequence[EvalScoredObservation],
) -> EvalReport:
    by_suite: dict[str, list[EvalScoredObservation]] = defaultdict(list)
    for result in results:
        by_suite[result.observation.eval_set].append(result)

    reports: list[EvalSuiteReport] = []
    diagnostics: list[EvalDiagnostic] = []
    gate_passed = True
    for suite in suites:
        suite_results = by_suite.get(suite.name, [])
        by_group_harness: dict[
            tuple[str, str], list[EvalScoredObservation]
        ] = defaultdict(list)
        for result in suite_results:
            observation = result.observation
            by_group_harness[(observation.group_key, observation.harness)].append(result)

        expected_groups = tuple(
            derive_group_key(case, repetition)
            for repetition in range(1, suite.repetitions + 1)
            for case in suite.cases
        )
        harnesses = (suite.baseline, *suite.candidates)
        for group_key in expected_groups:
            for harness in harnesses:
                observations = by_group_harness.get((group_key, harness.name), [])
                reason = _diagnostic_reason(observations)
                if reason is not None:
                    diagnostics.append(
                        EvalDiagnostic(suite.name, group_key, harness.name, reason)
                    )

        comparisons = tuple(
            _compare_candidate(
                suite,
                candidate.name,
                expected_groups,
                by_group_harness,
            )
            for candidate in suite.candidates
        )
        reports.append(EvalSuiteReport(suite.name, comparisons))

        for candidate in suite.candidates:
            for group_key in expected_groups:
                observations = by_group_harness.get((group_key, candidate.name), [])
                if len(observations) != 1:
                    gate_passed = False
                    continue
                candidate_result = observations[0]
                score = candidate_result.score
                if (
                    not candidate_result.observation.infrastructure_ok
                    or score is None
                    or score < suite.minimum_candidate_score
                ):
                    gate_passed = False

    return EvalReport(
        suites=tuple(reports),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (
                    item.eval_set,
                    item.group_key,
                    item.harness,
                    item.reason,
                ),
            )
        ),
        candidate_gate_passed=gate_passed,
    )


def _diagnostic_reason(
    observations: Sequence[EvalScoredObservation],
) -> str | None:
    if not observations:
        return "missing-observation"
    if len(observations) > 1:
        return "duplicate-observation"
    observation = observations[0]
    if not observation.observation.infrastructure_ok:
        return "harness-error"
    if observation.score is None:
        return "missing-score"
    return None


def _compare_candidate(
    suite: EvalSuite,
    candidate: str,
    expected_groups: Sequence[str],
    observations: dict[tuple[str, str], list[EvalScoredObservation]],
) -> EvalComparison:
    pairs: list[tuple[EvalScoredObservation, EvalScoredObservation]] = []
    for group_key in expected_groups:
        baseline_items = observations.get((group_key, suite.baseline.name), [])
        candidate_items = observations.get((group_key, candidate), [])
        if len(baseline_items) == 1 and len(candidate_items) == 1:
            pairs.append((baseline_items[0], candidate_items[0]))

    scored_pairs = [
        pair
        for pair in pairs
        if pair[0].observation.infrastructure_ok
        and pair[1].observation.infrastructure_ok
        and pair[0].score is not None
        and pair[1].score is not None
    ]
    baseline_passes = [
        pair[0].score >= suite.minimum_candidate_score
        for pair in scored_pairs
        if pair[0].score is not None
    ]
    candidate_passes = [
        pair[1].score >= suite.minimum_candidate_score
        for pair in scored_pairs
        if pair[1].score is not None
    ]
    baseline_rate = _mean([float(value) for value in baseline_passes])
    candidate_rate = _mean([float(value) for value in candidate_passes])
    wins = sum(
        candidate_passed and not baseline_passed
        for baseline_passed, candidate_passed in zip(
            baseline_passes, candidate_passes, strict=True
        )
    )
    losses = sum(
        baseline_passed and not candidate_passed
        for baseline_passed, candidate_passed in zip(
            baseline_passes, candidate_passes, strict=True
        )
    )
    ties = len(scored_pairs) - wins - losses
    total_pairs = len(expected_groups)
    return EvalComparison(
        baseline=suite.baseline.name,
        candidate=candidate,
        total_pairs=total_pairs,
        eligible_pairs=len(scored_pairs),
        baseline_pass_rate=baseline_rate,
        candidate_pass_rate=candidate_rate,
        pass_rate_lift=_difference(candidate_rate, baseline_rate),
        baseline_wins=losses,
        candidate_wins=wins,
        ties=ties,
        total_tokens=_metric(
            scored_pairs,
            lambda result: float(result.observation.usage.total_tokens),
            total_pairs,
        ),
        total_ms=_metric(
            scored_pairs,
            lambda result: result.observation.total_ms,
            total_pairs,
        ),
        estimated_cost=_metric(
            scored_pairs,
            lambda result: result.observation.usage.estimated_cost,
            total_pairs,
        ),
    )


def _metric(
    pairs: Sequence[tuple[EvalScoredObservation, EvalScoredObservation]],
    select: Callable[[EvalScoredObservation], float | None],
    total_pairs: int,
) -> EvalMetricComparison:
    baseline_values: list[float] = []
    candidate_values: list[float] = []
    for baseline, candidate in pairs:
        baseline_value = select(baseline)
        candidate_value = select(candidate)
        if baseline_value is None or candidate_value is None:
            continue
        baseline_values.append(baseline_value)
        candidate_values.append(candidate_value)
    baseline_mean = _mean(baseline_values)
    candidate_mean = _mean(candidate_values)
    return EvalMetricComparison(
        total_pairs=total_pairs,
        eligible_pairs=len(baseline_values),
        baseline_mean=baseline_mean,
        candidate_mean=candidate_mean,
        mean_delta=_difference(candidate_mean, baseline_mean),
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return float(f"{left - right:.15g}")


__all__ = [
    "EvalComparison",
    "EvalDiagnostic",
    "EvalMetricComparison",
    "EvalReport",
    "EvalSuiteReport",
    "summarize_results",
]
