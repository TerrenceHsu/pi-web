"""Semantic and security invariants for the dual-PDF parser Contract v2."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from wiki_parser import (
    PARSER_ARTIFACT_SCHEMA_V2,
    ParserArtifactFileV2,
    ParserArtifactManifestV2,
    ParserArtifactReceiptV2,
    ParserAttemptEvidence,
    ParserCapabilitiesV2,
    ParserJobHandleV2,
    ParserJobSpecV2,
    ParserJobStatusV2,
    ParserParsedDocument,
    ParserParsedPage,
    ParserPreflightReport,
    ParserProbeV2,
    ParserQualityMetrics,
    ParserQualityReport,
    ParserRouteDecision,
    ParserRoutingConfigIdentity,
    ParserSourceSpec,
    canonical_markdown_v2,
    canonical_preflight_sha256,
)

_SOURCE_SHA = "a" * 64
_OUTPUT_SHA = "b" * 64
_ARCHIVE_SHA = "c" * 64
_MANIFEST_SHA = "d" * 64
_FILE_SHA = "e" * 64
_CONFIG = ParserRoutingConfigIdentity(
    revision="routing_v1",
    sha256="f" * 64,
)
_SOURCE = ParserSourceSpec(
    source_id="source-1",
    display_name="paper.pdf",
    size_bytes=1024,
    sha256=_SOURCE_SHA,
)


def _preflight(*, scanned: bool = False) -> ParserPreflightReport:
    return ParserPreflightReport(
        source_id=_SOURCE.source_id,
        source_sha256=_SOURCE.sha256,
        page_count=2,
        text_page_count=0 if scanned else 2,
        image_dominant_page_count=2 if scanned else 0,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        text_page_ratio=0.0 if scanned else 1.0,
        image_dominant_page_ratio=1.0 if scanned else 0.0,
        complexity_score=0.9 if scanned else 0.1,
        duration_ms=8,
        observed_at_ms=100,
    )


def _route(
    requested_mode: str,
    *,
    preflight: ParserPreflightReport,
) -> ParserRouteDecision:
    preflight_sha = canonical_preflight_sha256(preflight)
    if requested_mode == "fast":
        return ParserRouteDecision(
            requested_mode="fast",
            initial_parser="pymupdf4llm",
            initial_preset="pymupdf4llm_fast_no_ocr",
            reasons=("explicit_fast",),
            preflight_sha256=preflight_sha,
        )
    if requested_mode == "accurate":
        return ParserRouteDecision(
            requested_mode="accurate",
            initial_parser="docling",
            initial_preset="docling_ocr" if preflight.text_page_count == 0 else "docling_standard",
            reasons=("explicit_accurate",),
            preflight_sha256=preflight_sha,
        )
    if preflight.text_page_count == 0:
        return ParserRouteDecision(
            requested_mode="auto",
            initial_parser="docling",
            initial_preset="docling_ocr",
            reasons=("scan_text_layer_missing",),
            preflight_sha256=preflight_sha,
        )
    return ParserRouteDecision(
        requested_mode="auto",
        initial_parser="pymupdf4llm",
        initial_preset="pymupdf4llm_fast_no_ocr",
        reasons=("simple_digital",),
        preflight_sha256=preflight_sha,
        fallback_parser="docling",
        fallback_preset="docling_standard",
    )


def _quality(
    parser: str,
    preset: str,
    *,
    passed: bool = True,
) -> ParserQualityReport:
    return ParserQualityReport(
        parser=parser,
        parser_version="1.2.3",
        preset=preset,
        routing_config=_CONFIG,
        metrics=ParserQualityMetrics(
            input_page_count=2,
            output_page_count=2,
            pages_with_text=2,
            output_character_count=42,
            text_page_coverage=1.0,
            replacement_char_ratio=0.0,
            control_char_ratio=0.0,
            content_completeness=1.0 if passed else 0.2,
            repetition_ratio=0.0,
            page_count_match=True,
            markdown_health=1.0,
            asset_reference_health=1.0,
        ),
        score=0.95 if passed else 0.2,
        pass_threshold=0.8,
        passed=passed,
        critical_failures=() if passed else ("incomplete_content",),
        evaluated_at_ms=115,
    )


def _attempt(
    *,
    attempt_id: str,
    ordinal: int,
    parser: str,
    preset: str,
    reasons: tuple[str, ...],
    passed: bool,
    fallback_from: str | None = None,
) -> ParserAttemptEvidence:
    return ParserAttemptEvidence(
        attempt_id=attempt_id,
        ordinal=ordinal,
        source_sha256=_SOURCE_SHA,
        parser=parser,
        parser_version="1.2.3",
        preset=preset,
        routing_config=_CONFIG,
        route_reasons=reasons,
        state="succeeded" if passed else "quality_rejected",
        started_at_ms=110,
        finished_at_ms=120,
        duration_ms=10,
        safe_error_code=None if passed else "quality_rejected",
        quality_report=_quality(parser, preset, passed=passed),
        output_size_bytes=128 if passed else None,
        output_sha256=_OUTPUT_SHA if passed else None,
        fallback_from_attempt_id=fallback_from,
    )


def _page(page_number: int, markdown: str) -> ParserParsedPage:
    plain_text = markdown.removeprefix("# ").strip()
    return ParserParsedPage(
        page_number=page_number,
        markdown=markdown,
        markdown_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        plain_text=plain_text,
        plain_text_sha256=hashlib.sha256(plain_text.encode()).hexdigest(),
    )


def _fast_document() -> ParserParsedDocument:
    preflight = _preflight()
    pages = (_page(1, "# One"), _page(2, "Two\n"))
    attempt = _attempt(
        attempt_id="attempt-1",
        ordinal=1,
        parser="pymupdf4llm",
        preset="pymupdf4llm_fast_no_ocr",
        reasons=("explicit_fast",),
        passed=True,
    )
    return ParserParsedDocument(
        source_id=_SOURCE.source_id,
        source_sha256=_SOURCE_SHA,
        requested_mode="fast",
        selected_attempt_id=attempt.attempt_id,
        parser=attempt.parser,
        parser_version=attempt.parser_version,
        preset=attempt.preset,
        routing_config=_CONFIG,
        preflight=preflight,
        route_decision=_route("fast", preflight=preflight),
        canonical_markdown_sha256=hashlib.sha256(canonical_markdown_v2(pages)).hexdigest(),
        page_count=2,
        pages=pages,
        quality_report=attempt.quality_report,
        attempts=(attempt,),
    )


def _fallback_document() -> ParserParsedDocument:
    preflight = _preflight()
    route = _route("auto", preflight=preflight)
    first = _attempt(
        attempt_id="attempt-fast",
        ordinal=1,
        parser="pymupdf4llm",
        preset="pymupdf4llm_fast_no_ocr",
        reasons=route.reasons,
        passed=False,
    )
    selected = _attempt(
        attempt_id="attempt-docling",
        ordinal=2,
        parser="docling",
        preset="docling_standard",
        reasons=("fast_quality_fallback",),
        passed=True,
        fallback_from=first.attempt_id,
    )
    pages = (_page(1, "# One"), _page(2, "Two"))
    return ParserParsedDocument(
        source_id=_SOURCE.source_id,
        source_sha256=_SOURCE_SHA,
        requested_mode="auto",
        selected_attempt_id=selected.attempt_id,
        parser=selected.parser,
        parser_version=selected.parser_version,
        preset=selected.preset,
        routing_config=_CONFIG,
        preflight=preflight,
        route_decision=route,
        canonical_markdown_sha256=hashlib.sha256(canonical_markdown_v2(pages)).hexdigest(),
        page_count=2,
        pages=pages,
        quality_report=selected.quality_report,
        attempts=(first, selected),
    )


def _revalidate(model: object, model_type: type[object], **updates: object) -> object:
    payload = model.model_dump(mode="python")  # type: ignore[attr-defined]
    payload.update(updates)
    return model_type.model_validate(payload)  # type: ignore[attr-defined]


def test_job_spec_is_hash_pinned_and_rejects_runtime_arguments() -> None:
    spec = ParserJobSpecV2(
        job_id="job-1",
        source=_SOURCE,
        routing_config=_CONFIG,
    )

    assert spec.contract_version == 2
    assert spec.requested_mode == "auto"
    assert spec.output_schema == PARSER_ARTIFACT_SCHEMA_V2
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ParserJobSpecV2.model_validate(
            {
                **spec.model_dump(),
                "quality_threshold": 0.7,
                "parser_kwargs": {"use_ocr": True},
            }
        )


def test_preflight_counts_and_stable_digest_are_explainable() -> None:
    report = _preflight()
    assert canonical_preflight_sha256(report) == canonical_preflight_sha256(
        ParserPreflightReport.model_validate_json(report.model_dump_json())
    )

    payload = report.model_dump()
    payload["text_page_ratio"] = 0.5
    with pytest.raises(ValidationError, match="text_page_ratio"):
        ParserPreflightReport.model_validate(payload)
    payload = report.model_dump()
    payload["table_candidate_page_count"] = 3
    with pytest.raises(ValidationError, match="exceeds page_count"):
        ParserPreflightReport.model_validate(payload)


def test_route_modes_freeze_fallback_semantics() -> None:
    simple = _preflight()
    scanned = _preflight(scanned=True)

    assert _route("fast", preflight=simple).fallback_parser is None
    assert _route("accurate", preflight=scanned).initial_preset == "docling_ocr"
    assert _route("auto", preflight=simple).fallback_parser == "docling"
    assert _route("auto", preflight=scanned).initial_parser == "docling"

    with pytest.raises(ValidationError, match="fast mode"):
        ParserRouteDecision(
            requested_mode="fast",
            initial_parser="pymupdf4llm",
            initial_preset="pymupdf4llm_fast_no_ocr",
            reasons=("simple_digital",),
            preflight_sha256="0" * 64,
        )
    with pytest.raises(ValidationError, match="auto fast route"):
        ParserRouteDecision(
            requested_mode="auto",
            initial_parser="pymupdf4llm",
            initial_preset="pymupdf4llm_fast_no_ocr",
            reasons=("simple_digital",),
            preflight_sha256="0" * 64,
        )
    with pytest.raises(ValidationError, match="auto route"):
        ParserRouteDecision(
            requested_mode="auto",
            initial_parser="docling",
            initial_preset="docling_standard",
            reasons=("fast_quality_fallback",),
            preflight_sha256="0" * 64,
        )


def test_quality_report_cannot_disagree_with_metrics_or_decision() -> None:
    metrics = _quality(
        "pymupdf4llm", "pymupdf4llm_fast_no_ocr"
    ).metrics.model_dump()
    metrics["text_page_coverage"] = 0.5
    with pytest.raises(ValidationError, match="text_page_coverage"):
        ParserQualityMetrics.model_validate(metrics)

    report = _quality("pymupdf4llm", "pymupdf4llm_fast_no_ocr")
    with pytest.raises(ValidationError, match="passed flag"):
        _revalidate(report, ParserQualityReport, passed=False)
    with pytest.raises(ValidationError, match="unique"):
        _revalidate(
            report,
            ParserQualityReport,
            passed=False,
            critical_failures=("missing_pages", "missing_pages"),
        )


def test_page_content_is_hashed_hidden_from_repr_and_utf8_safe() -> None:
    page = _page(1, "PRIVATE MARKDOWN")
    assert "PRIVATE MARKDOWN" not in repr(page)
    assert canonical_markdown_v2((page,)).decode() == (
        "<!-- llm-wiki-pdf-page:1 -->\nPRIVATE MARKDOWN\n"
    )

    payload = page.model_dump()
    payload["markdown"] = "damaged \ufffd"
    payload["markdown_sha256"] = hashlib.sha256("damaged \ufffd".encode()).hexdigest()
    with pytest.raises(ValidationError, match="replacement"):
        ParserParsedPage.model_validate(payload)
    with pytest.raises(ValueError, match="reserved boundary"):
        canonical_markdown_v2((_page(1, "<!-- llm-wiki-pdf-page:99 -->"),))


def test_attempt_evidence_requires_terminal_quality_and_same_config() -> None:
    attempt = _fast_document().attempts[0]
    with pytest.raises(ValidationError, match="output evidence"):
        _revalidate(attempt, ParserAttemptEvidence, output_sha256=None)
    with pytest.raises(ValidationError, match="duration"):
        _revalidate(attempt, ParserAttemptEvidence, duration_ms=9)
    with pytest.raises(ValidationError, match="quality report does not match"):
        _revalidate(
            attempt,
            ParserAttemptEvidence,
            routing_config=ParserRoutingConfigIdentity(
                revision="routing_v2",
                sha256="0" * 64,
            ),
        )


def test_parsed_document_accepts_exact_fast_and_auto_fallback_routes() -> None:
    fast = _fast_document()
    fallback = _fallback_document()

    assert fast.parser == "pymupdf4llm"
    assert [attempt.state for attempt in fallback.attempts] == [
        "quality_rejected",
        "succeeded",
    ]
    assert fallback.attempts[1].source_sha256 == fallback.attempts[0].source_sha256
    assert ParserParsedDocument.model_validate_json(fallback.model_dump_json()) == fallback


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_sha256", "0" * 64, "preflight identity"),
        ("selected_attempt_id", "attempt-fast", "selected attempt"),
        ("requested_mode", "fast", "route decision"),
        ("canonical_markdown_sha256", "0" * 64, "canonical Markdown"),
    ],
)
def test_parsed_document_rejects_cross_evidence_mismatches(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _revalidate(_fallback_document(), ParserParsedDocument, **{field: value})


def test_parsed_document_rejects_forged_fallback_chain() -> None:
    document = _fallback_document()
    second = document.attempts[1].model_dump()
    second["fallback_from_attempt_id"] = "unrelated-attempt"
    with pytest.raises(ValidationError, match="fallback must reference"):
        _revalidate(
            document,
            ParserParsedDocument,
            attempts=(document.attempts[0].model_dump(), second),
        )


def test_probe_exposes_fixed_offline_capabilities_and_agpl_mode() -> None:
    probe = ParserProbeV2(
        provider="dual_pdf",
        available=True,
        worker_version="0.1.0",
        license_mode="agpl_3_0",
        routing_config=_CONFIG,
        observed_at_ms=1,
    )
    assert probe.capabilities == ParserCapabilitiesV2()
    assert probe.capabilities.fast_uses_ocr is False
    assert probe.capabilities.auto_fallback_uses_original_pdf is True
    assert probe.capabilities.network_during_job is False

    with pytest.raises(ValidationError, match="AGPL"):
        _revalidate(probe, ParserProbeV2, license_mode="not_required")
    fake_payload = probe.model_dump()
    fake_payload.update(provider="fake_dual_pdf", license_mode="agpl_3_0")
    with pytest.raises(ValidationError, match="must not claim"):
        ParserProbeV2.model_validate(fake_payload)


def test_artifact_v2_allows_blank_page_but_not_empty_image() -> None:
    blank_page = ParserArtifactFileV2(
        path="pages/000002.md",
        kind="page_markdown",
        mime_type="text/markdown",
        size_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
        page_number=2,
    )
    assert blank_page.size_bytes == 0
    with pytest.raises(ValidationError, match="cannot be empty"):
        ParserArtifactFileV2(
            path="images/a.png",
            kind="embedded_image",
            mime_type="image/png",
            size_bytes=0,
            sha256=_FILE_SHA,
            page_number=1,
        )


def test_manifest_and_job_status_bind_route_attempts_and_archive_evidence() -> None:
    document = _fast_document()
    page_files = tuple(
        ParserArtifactFileV2(
            path=f"pages/{page.page_number:06d}.md",
            kind="page_markdown",
            mime_type="text/markdown",
            size_bytes=len(page.markdown.encode()),
            sha256=page.markdown_sha256,
            page_number=page.page_number,
        )
        for page in document.pages
    )
    manifest = ParserArtifactManifestV2(
        job_id="job-1",
        source_id=document.source_id,
        source_sha256=document.source_sha256,
        requested_mode=document.requested_mode,
        selected_attempt_id=document.selected_attempt_id,
        parser=document.parser,
        parser_version=document.parser_version,
        preset=document.preset,
        routing_config=document.routing_config,
        preflight=document.preflight,
        route_decision=document.route_decision,
        page_count=document.page_count,
        quality_report=document.quality_report,
        attempts=document.attempts,
        markdown=ParserArtifactFileV2(
            path="parsed.md",
            kind="document_markdown",
            mime_type="text/markdown",
            size_bytes=len(canonical_markdown_v2(document.pages)),
            sha256=document.canonical_markdown_sha256,
        ),
        pages=page_files,
    )
    assert manifest.schema_id == PARSER_ARTIFACT_SCHEMA_V2

    handle = ParserJobHandleV2(
        provider="dual_pdf",
        provider_job_id="provider-job-1",
        job_id="job-1",
        source_id=document.source_id,
        created_at_ms=90,
    )
    status = ParserJobStatusV2(
        handle=handle,
        state="succeeded",
        phase="terminal",
        observed_at_ms=130,
        started_at_ms=100,
        finished_at_ms=125,
        current_attempt_ordinal=1,
        route_decision=document.route_decision,
        attempts=document.attempts,
        artifact_size_bytes=2048,
        artifact_sha256=_ARCHIVE_SHA,
    )
    receipt = ParserArtifactReceiptV2(
        job_id=handle.job_id,
        source_id=handle.source_id,
        size_bytes=status.artifact_size_bytes,
        sha256=status.artifact_sha256,
        manifest_sha256=_MANIFEST_SHA,
        file_count=4,
    )
    assert receipt.contract_version == 2

    with pytest.raises(ValidationError, match="terminal artifact and attempt evidence"):
        _revalidate(status, ParserJobStatusV2, route_decision=None)
    with pytest.raises(ValidationError, match="preflight identity"):
        _revalidate(
            manifest,
            ParserArtifactManifestV2,
            source_sha256="0" * 64,
        )

    fallback = _fallback_document()
    fallback_status = ParserJobStatusV2(
        handle=handle,
        state="succeeded",
        phase="terminal",
        observed_at_ms=130,
        started_at_ms=100,
        finished_at_ms=125,
        current_attempt_ordinal=2,
        route_decision=fallback.route_decision,
        attempts=fallback.attempts,
        artifact_size_bytes=2048,
        artifact_sha256=_ARCHIVE_SHA,
    )
    second_attempt = fallback.attempts[1].model_dump()
    second_attempt["source_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="one original PDF SHA"):
        _revalidate(
            fallback_status,
            ParserJobStatusV2,
            attempts=(fallback.attempts[0].model_dump(), second_attempt),
        )


def test_job_state_and_phase_cannot_contradict_each_other() -> None:
    handle = ParserJobHandleV2(
        provider="dual_pdf",
        provider_job_id="provider-job-1",
        job_id="job-1",
        source_id=_SOURCE.source_id,
        created_at_ms=1,
    )
    with pytest.raises(ValidationError, match="queued phase"):
        ParserJobStatusV2(
            handle=handle,
            state="queued",
            phase="preflight",
            observed_at_ms=2,
        )
    with pytest.raises(ValidationError, match="active phase"):
        ParserJobStatusV2(
            handle=handle,
            state="running",
            phase="terminal",
            observed_at_ms=2,
            started_at_ms=2,
        )
