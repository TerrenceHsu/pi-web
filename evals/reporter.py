"""Human-readable local eval comparison reports."""

from __future__ import annotations

from .comparison import EvalMetricComparison, EvalReport


def format_report(report: EvalReport) -> str:
    lines = ["Local Eval Comparisons"]
    for suite in report.suites:
        lines.append(f"  {suite.name}")
        for comparison in suite.comparisons:
            coverage = f"{comparison.eligible_pairs}/{comparison.total_pairs} pairs"
            lines.append(f"    baseline   {comparison.baseline}")
            lines.append(f"    candidate  {comparison.candidate} ({coverage})")
            lines.append(
                "    pass rate  "
                f"{_percentage(comparison.pass_rate_lift, signed=True)} "
                f"(candidate {_percentage(comparison.candidate_pass_rate)}, "
                f"baseline {_percentage(comparison.baseline_pass_rate)})"
            )
            lines.append(_metric_line("tokens", comparison.total_tokens, ""))
            lines.append(_metric_line("latency", comparison.total_ms, "ms"))
            lines.append(_metric_line("est. cost", comparison.estimated_cost, ""))
    if report.diagnostics:
        lines.append("  Incomplete observations")
        for diagnostic in report.diagnostics:
            lines.append(
                f"    {diagnostic.reason}: {diagnostic.eval_set}/"
                f"{diagnostic.group_key}, harness {diagnostic.harness}"
            )
    lines.append(
        "  Candidate gate: " + ("PASS" if report.candidate_gate_passed else "FAIL")
    )
    return "\n".join(lines)


def format_markdown_report(report: EvalReport) -> str:
    lines = ["# Local Eval Report", ""]
    for suite in report.suites:
        lines.extend([f"## {suite.name}", ""])
        for comparison in suite.comparisons:
            lines.extend(
                [
                    f"- Baseline: `{comparison.baseline}`",
                    f"- Candidate: `{comparison.candidate}`",
                    (
                        "- Pass-rate lift: "
                        f"{_percentage(comparison.pass_rate_lift, signed=True)} "
                        f"(candidate {_percentage(comparison.candidate_pass_rate)}, "
                        f"baseline {_percentage(comparison.baseline_pass_rate)})"
                    ),
                    (
                        "- Eligible pairs: "
                        f"{comparison.eligible_pairs}/{comparison.total_pairs}"
                    ),
                    f"- Token delta: {_delta(comparison.total_tokens)}",
                    f"- Latency delta: {_delta(comparison.total_ms, suffix='ms')}",
                    "",
                ]
            )
    if report.diagnostics:
        lines.extend(["## Diagnostics", ""])
        lines.extend(
            f"- `{item.reason}`: {item.eval_set}/{item.group_key}, {item.harness}"
            for item in report.diagnostics
        )
        lines.append("")
    lines.extend(
        [
            "## Gate",
            "",
            "PASS" if report.candidate_gate_passed else "FAIL",
            "",
        ]
    )
    return "\n".join(lines)


def _percentage(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "unavailable"
    prefix = "+" if signed and value >= 0 else ""
    suffix = " pp" if signed else "%"
    multiplier = 100
    return f"{prefix}{value * multiplier:.1f}{suffix}"


def _metric_line(label: str, metric: EvalMetricComparison, suffix: str) -> str:
    if metric.mean_delta is None:
        return f"    {label:<10} unavailable"
    return (
        f"    {label:<10} {metric.mean_delta:+.1f}{suffix} "
        f"(candidate {metric.candidate_mean:.1f}{suffix}, "
        f"baseline {metric.baseline_mean:.1f}{suffix})"
    )


def _delta(metric: EvalMetricComparison, suffix: str = "") -> str:
    if metric.mean_delta is None:
        return "unavailable"
    return f"{metric.mean_delta:+.1f}{suffix}"


__all__ = ["format_markdown_report", "format_report"]
