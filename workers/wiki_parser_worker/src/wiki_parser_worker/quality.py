"""Explainable, versioned fast-path quality evaluation.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable

from .config import WorkerRoutingConfig
from .models import (
    QualityFailure,
    WorkerParsedDocument,
    WorkerPreflightReport,
    WorkerQualityMetrics,
    WorkerQualityReport,
)

_IMAGE_REFERENCE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _markdown_health(markdown: str) -> float:
    penalties = 0.0
    if markdown.count("```") % 2:
        penalties += 0.4
    if markdown.count("[") != markdown.count("]"):
        penalties += 0.2
    if markdown.count("(") != markdown.count(")"):
        penalties += 0.2
    table_lines = [line for line in markdown.splitlines() if line.strip().startswith("|")]
    if table_lines and not any(re.search(r"\|\s*:?-{3,}", line) for line in table_lines):
        penalties += 0.2
    return max(0.0, 1.0 - penalties)


def _asset_reference_health(document: WorkerParsedDocument) -> float:
    known = {asset.path for asset in document.assets}
    references = [
        match.group(1)
        for page in document.pages
        for match in _IMAGE_REFERENCE.finditer(page.markdown)
    ]
    if not references:
        return 1.0
    valid = 0
    for reference in references:
        path = reference.split("#", maxsplit=1)[0].split("?", maxsplit=1)[0]
        if (
            path in known
            and not path.startswith("/")
            and "\\" not in path
            and ".." not in path.split("/")
        ):
            valid += 1
    return valid / len(references)


def _repetition_ratio(document: WorkerParsedDocument) -> float:
    lines = [
        " ".join(line.casefold().split())
        for page in document.pages
        for line in page.plain_text.splitlines()
        if len("".join(line.split())) >= 16
    ]
    total = sum(len(line) for line in lines)
    if total == 0:
        return 0.0
    counts = Counter(lines)
    repeated = sum(len(line) * (count - 1) for line, count in counts.items())
    return min(repeated / total, 1.0)


class QualityEvaluator:
    """Compute stable metrics and a fail-closed decision from fixed config."""

    def __init__(
        self,
        config: WorkerRoutingConfig,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._config = config
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def evaluate(
        self,
        document: WorkerParsedDocument,
        preflight: WorkerPreflightReport,
    ) -> WorkerQualityReport:
        combined_markdown = "\n".join(page.markdown for page in document.pages)
        combined_text = "\n".join(page.plain_text for page in document.pages)
        output_characters = len("".join(combined_text.split()))
        pages_with_text = sum(
            len("".join(page.plain_text.split()))
            >= self._config.preflight.min_text_characters_per_page
            for page in document.pages
        )
        evaluated_text = combined_markdown + "\n" + combined_text
        total_chars = max(len(evaluated_text), 1)
        replacement_ratio = evaluated_text.count("\ufffd") / total_chars
        control_count = sum(
            ord(character) < 32 and character not in "\n\r\t"
            for character in evaluated_text
        )
        control_ratio = control_count / total_chars
        native_characters = max(preflight.native_text_character_count, 1)
        completeness = min(output_characters / native_characters, 1.0)
        repetition = _repetition_ratio(document)
        markdown_health = _markdown_health(combined_markdown)
        asset_health = _asset_reference_health(document)
        metrics = WorkerQualityMetrics(
            input_page_count=preflight.page_count,
            output_page_count=len(document.pages),
            pages_with_text=pages_with_text,
            output_character_count=output_characters,
            text_page_coverage=min(_ratio(pages_with_text, preflight.page_count), 1.0),
            replacement_char_ratio=replacement_ratio,
            control_char_ratio=control_ratio,
            content_completeness=completeness,
            repetition_ratio=repetition,
            page_count_match=len(document.pages) == preflight.page_count,
            markdown_health=markdown_health,
            asset_reference_health=asset_health,
        )
        thresholds = self._config.quality
        failures: list[QualityFailure] = []
        if not metrics.page_count_match:
            failures.append("missing_pages")
        if metrics.output_character_count == 0:
            failures.append("empty_content")
        if metrics.replacement_char_ratio > thresholds.maximum_replacement_character_ratio:
            failures.append("replacement_characters")
        if metrics.control_char_ratio > thresholds.maximum_control_character_ratio:
            failures.append("excessive_controls")
        if (
            metrics.text_page_coverage < thresholds.minimum_text_page_coverage
            or metrics.content_completeness < thresholds.minimum_content_completeness
        ):
            failures.append("incomplete_content")
        if metrics.repetition_ratio > thresholds.maximum_repetition_ratio:
            failures.append("excessive_repetition")
        if metrics.markdown_health < thresholds.minimum_markdown_health:
            failures.append("markdown_invalid")
        if metrics.asset_reference_health < thresholds.minimum_asset_reference_health:
            failures.append("asset_reference_invalid")

        score = (
            metrics.text_page_coverage * thresholds.text_page_coverage_weight
            + metrics.content_completeness * thresholds.content_completeness_weight
            + (1.0 - metrics.repetition_ratio) * thresholds.repetition_weight
            + metrics.markdown_health * thresholds.markdown_health_weight
            + metrics.asset_reference_health * thresholds.asset_reference_health_weight
        )
        score = min(max(score, 0.0), 1.0)
        critical = tuple(failures)
        return WorkerQualityReport(
            metrics=metrics,
            score=score,
            pass_threshold=thresholds.pass_threshold,
            passed=score >= thresholds.pass_threshold and not critical,
            critical_failures=critical,
            evaluated_at_ms=self._clock_ms(),
        )


__all__ = ["QualityEvaluator"]
