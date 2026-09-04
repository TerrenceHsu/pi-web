"""Semantic invariants for the MinerU parser Contract v2."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wiki_parser import (
    ParserCapabilitiesV2,
    ParserJobSpecV2,
    ParserPreflightReport,
    ParserProbeV2,
    ParserRouteDecision,
    ParserRoutingConfigIdentity,
    ParserSourceSpec,
    canonical_preflight_sha256,
)

_CONFIG = ParserRoutingConfigIdentity(revision="mineru_profiles_v1", sha256="a" * 64)
_SOURCE = ParserSourceSpec(
    source_id="source-1",
    display_name="paper.pdf",
    size_bytes=128,
    sha256="b" * 64,
)


def _preflight() -> ParserPreflightReport:
    return ParserPreflightReport(
        source_id=_SOURCE.source_id,
        source_sha256=_SOURCE.sha256,
        page_count=2,
        text_page_count=2,
        image_dominant_page_count=0,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        text_page_ratio=1.0,
        image_dominant_page_ratio=0.0,
        complexity_score=0.1,
        duration_ms=5,
        observed_at_ms=100,
    )


@pytest.mark.parametrize(
    ("mode", "preset", "reason"),
    [
        ("pipeline", "mineru_pipeline", "explicit_pipeline"),
        ("gpu-medium", "mineru_gpu_medium", "explicit_gpu_medium"),
        ("gpu-high", "mineru_gpu_high", "explicit_gpu_high"),
    ],
)
def test_each_product_profile_maps_to_one_fixed_mineru_route(
    mode: str,
    preset: str,
    reason: str,
) -> None:
    decision = ParserRouteDecision(
        requested_mode=mode,
        initial_parser="mineru",
        initial_preset=preset,
        reasons=(reason,),
        preflight_sha256=canonical_preflight_sha256(_preflight()),
    )

    assert decision.initial_parser == "mineru"
    assert decision.initial_preset == preset
    assert decision.fallback_parser is None
    assert decision.fallback_preset is None


def test_legacy_mode_and_cross_profile_route_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ParserJobSpecV2.model_validate(
            {
                "job_id": "job-1",
                "source": _SOURCE.model_dump(mode="json"),
                "requested_mode": "auto",
                "routing_config": _CONFIG.model_dump(mode="json"),
            }
        )
    with pytest.raises(ValidationError, match="requested preset"):
        ParserRouteDecision(
            requested_mode="pipeline",
            initial_parser="mineru",
            initial_preset="mineru_gpu_high",
            reasons=("explicit_pipeline",),
            preflight_sha256="c" * 64,
        )


def test_capabilities_expose_only_closed_product_profiles() -> None:
    capabilities = ParserCapabilitiesV2()

    assert capabilities.requested_modes == ("pipeline", "gpu-medium", "gpu-high")
    assert capabilities.parsers == ("mineru",)
    assert capabilities.pipeline_supports_cpu is True
    assert capabilities.gpu_presets_require_cuda is True
    assert capabilities.network_during_job is False


def test_real_probe_requires_mineru_license_declaration() -> None:
    payload = {
        "provider": "mineru",
        "available": True,
        "worker_version": "0.1.0",
        "license_mode": "not_required",
        "routing_config": _CONFIG.model_dump(mode="json"),
        "observed_at_ms": 1,
    }
    with pytest.raises(ValidationError, match="declared license"):
        ParserProbeV2.model_validate(payload)

    payload["license_mode"] = "mineru_open_source"
    assert ParserProbeV2.model_validate(payload).provider == "mineru"
