"""Completely offline MinerU parser fake for Contract v2.

The fake exercises the production preset mapping and job state machine without
importing a PDF runtime, launching a process, or opening a network connection.
It snapshots the verified source bytes at job creation so every simulated
attempt is tied to the same original PDF digest.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import tarfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contract_v2 import (
    ParserArtifactFileV2,
    ParserArtifactManifestV2,
    ParserArtifactReceiptV2,
    ParserAttemptEvidence,
    ParserAttemptStateV2,
    ParserCapabilitiesV2,
    ParserEngineName,
    ParserImageMimeTypeV2,
    ParserJobHandleV2,
    ParserJobPhaseV2,
    ParserJobSpecV2,
    ParserJobStateV2,
    ParserJobStatusV2,
    ParserParsedPage,
    ParserPreflightReport,
    ParserPresetName,
    ParserProbeV2,
    ParserQualityMetrics,
    ParserQualityReport,
    ParserRouteDecision,
    ParserRouteReason,
    ParserRoutingConfigIdentity,
    canonical_markdown_v2,
    canonical_preflight_sha256,
)
from .errors import ParserError, ParserErrorCode

_FAKE_WORKER_VERSION = "0.1.0"
_FAKE_MINERU_VERSION = "fake-mineru-3.4.5"
_FAKE_ROUTING_CONFIG = ParserRoutingConfigIdentity(
    revision="fake_routing_v1",
    sha256=hashlib.sha256(b"llm-wiki-fake-routing-v1").hexdigest(),
)
_IMAGE_SUFFIX: dict[ParserImageMimeTypeV2, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

FakePreflightProfileV2 = Literal["simple_digital", "scanned", "complex_table"]
FakeAttemptOutcomeV2 = Literal["succeeded", "quality_rejected", "failed"]


class FakeParserAssetV2(BaseModel):
    """One optional image emitted by the offline fake."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["embedded_image", "table_image"] = "embedded_image"
    mime_type: ParserImageMimeTypeV2 = "image/png"
    content: bytes = Field(repr=False, min_length=1)
    page_number: int = Field(default=1, ge=1)


class FakeParserDocumentV2(BaseModel):
    """Parser-neutral fake output; page text is deliberately hidden from repr."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: tuple[str, ...] = Field(default=("# Fake PDF page\n",), repr=False)
    assets: tuple[FakeParserAssetV2, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("pages")
    @classmethod
    def _validate_pages(cls, pages: tuple[str, ...]) -> tuple[str, ...]:
        if not pages:
            raise ValueError("fake document requires at least one page")
        for markdown in pages:
            if "\ufffd" in markdown or "\x00" in markdown:
                raise ValueError("fake page contains invalid characters")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in markdown):
                raise ValueError("fake page contains invalid control characters")
        return pages

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, warnings: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(warnings)) != len(warnings):
            raise ValueError("fake warnings must be unique")
        if any(
            not warning
            or len(warning) > 128
            or not all(char.isalnum() or char in "_-" for char in warning)
            for warning in warnings
        ):
            raise ValueError("fake warning has an invalid format")
        return warnings

    @model_validator(mode="after")
    def _validate_assets(self) -> FakeParserDocumentV2:
        if any(asset.page_number > len(self.pages) for asset in self.assets):
            raise ValueError("fake asset page exceeds document page count")
        return self


class FakeParserScenarioV2(BaseModel):
    """One deterministic MinerU parser-outcome scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preflight_profile: FakePreflightProfileV2 = "simple_digital"
    outcome: FakeAttemptOutcomeV2 = "succeeded"
    document: FakeParserDocumentV2 = Field(default_factory=FakeParserDocumentV2)


@dataclass(frozen=True)
class _NormalizedOutputV2:
    document: FakeParserDocumentV2 = field(repr=False)
    pages: tuple[ParserParsedPage, ...] = field(repr=False)
    files: tuple[tuple[ParserArtifactFileV2, bytes], ...] = field(repr=False)
    output_size_bytes: int
    output_sha256: str


@dataclass
class _FakeParserStateV2:
    handle: ParserJobHandleV2
    spec: ParserJobSpecV2
    scenario: FakeParserScenarioV2
    source_bytes: bytes = field(repr=False)
    state: ParserJobStateV2 = "queued"
    phase: ParserJobPhaseV2 = "queued"
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    route_decision: ParserRouteDecision | None = None
    attempts: list[ParserAttemptEvidence] = field(default_factory=list)
    safe_error_code: ParserErrorCode | None = None
    artifact: bytes | None = field(default=None, repr=False)
    artifact_sha256: str | None = None
    manifest_sha256: str | None = None
    file_count: int | None = None


class FakeMineruParserProvider:
    """In-memory Contract v2 provider with real hashing and tar artifacts."""

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        scenarios: Iterable[FakeParserScenarioV2] | None = None,
        routing_config: ParserRoutingConfigIdentity = _FAKE_ROUTING_CONFIG,
        completion_gate: asyncio.Event | None = None,
        wait_timeout_seconds: float | None = None,
    ) -> None:
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._id_factory = id_factory or (lambda: f"fakev2-{uuid4().hex}")
        self._scenarios = deque(scenarios or ())
        self._routing_config = routing_config
        self._completion_gate = completion_gate
        self._wait_timeout_seconds = wait_timeout_seconds
        self._states: dict[str, _FakeParserStateV2] = {}
        self.created_specs: list[ParserJobSpecV2] = []
        self.destroyed_provider_job_ids: list[str] = []

    def provider_name(self) -> Literal["fake_mineru"]:
        return "fake_mineru"

    async def probe(self) -> ParserProbeV2:
        return ParserProbeV2(
            provider="fake_mineru",
            available=True,
            worker_version=_FAKE_WORKER_VERSION,
            license_mode="not_required",
            routing_config=self._routing_config,
            capabilities=ParserCapabilitiesV2(),
            observed_at_ms=self._clock_ms(),
        )

    async def create_job(
        self,
        spec: ParserJobSpecV2,
        *,
        source_path: Path,
    ) -> ParserJobHandleV2:
        if spec.routing_config != self._routing_config:
            raise ParserError("invalid_configuration", provider=self.provider_name())
        source_bytes = await asyncio.to_thread(self._read_verified_source, source_path, spec)
        handle = ParserJobHandleV2(
            provider="fake_mineru",
            provider_job_id=self._id_factory(),
            job_id=spec.job_id,
            source_id=spec.source.source_id,
            created_at_ms=self._clock_ms(),
        )
        if handle.provider_job_id in self._states:
            raise ParserError("protocol_error", provider=self.provider_name())
        scenario = self._scenarios.popleft() if self._scenarios else FakeParserScenarioV2()
        self._states[handle.provider_job_id] = _FakeParserStateV2(
            handle=handle,
            spec=spec,
            scenario=scenario,
            source_bytes=source_bytes,
        )
        self.created_specs.append(spec)
        return handle

    async def status(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        return self._snapshot(self._state_for_status(handle))

    async def wait(
        self,
        handle: ParserJobHandleV2,
        *,
        signal: asyncio.Event | None = None,
    ) -> ParserJobStatusV2:
        state = self._live_state(handle)
        if state.state in {"succeeded", "failed", "cancelled"}:
            return self._snapshot(state)
        if signal is not None and signal.is_set():
            return self._mark_cancelled(state)

        if state.state == "queued":
            state.state = "running"
            state.phase = "preflight"
            state.started_at_ms = self._clock_ms()

        if self._completion_gate is not None and not self._completion_gate.is_set():
            outcome = await self._wait_for_gate_or_cancel(
                signal,
                timeout_seconds=(
                    self._wait_timeout_seconds
                    if self._wait_timeout_seconds is not None
                    else state.spec.limits.timeout_seconds
                ),
            )
            if outcome == "cancelled":
                return self._mark_cancelled(state)
            if outcome == "request_timeout":
                return self._mark_failed(state, "request_timeout")
        if signal is not None and signal.is_set():
            return self._mark_cancelled(state)

        try:
            self._verify_source_snapshot(state)
            state.phase = "routing"
            preflight = self._build_preflight(state)
            route = self._route(state.spec, preflight, state.scenario.preflight_profile)
            state.route_decision = route

            state.phase = self._phase_for_parser(route.initial_parser)
            selected_output = self._run_attempt(
                state,
                parser=route.initial_parser,
                preset=route.initial_preset,
                reasons=route.reasons,
                fallback_from_attempt_id=None,
            )
            selected_attempt = state.attempts[-1]
            if selected_attempt.state != "succeeded" or selected_output is None:
                return self._mark_failed(
                    state,
                    selected_attempt.safe_error_code or "parsing_failed",
                )

            state.phase = "packaging"
            artifact, archive_sha, manifest_sha, file_count = self._build_artifact(
                state,
                preflight=preflight,
                output=selected_output,
            )
        except ParserError as exc:
            return self._mark_failed(state, exc.code)
        except Exception:
            return self._mark_failed(state, "protocol_error")

        state.artifact = artifact
        state.artifact_sha256 = archive_sha
        state.manifest_sha256 = manifest_sha
        state.file_count = file_count
        state.state = "succeeded"
        state.phase = "terminal"
        state.finished_at_ms = self._clock_ms()
        state.safe_error_code = None
        return self._snapshot(state)

    async def download_artifact(
        self,
        handle: ParserJobHandleV2,
        *,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> ParserArtifactReceiptV2:
        state = self._live_state(handle)
        if state.state != "succeeded" or state.artifact is None:
            raise ParserError("artifact_unavailable", provider=self.provider_name())
        digest = hashlib.sha256(state.artifact).hexdigest()
        if digest != state.artifact_sha256 or (
            expected_sha256 is not None and expected_sha256 != digest
        ):
            raise ParserError("artifact_invalid", provider=self.provider_name())
        try:
            await asyncio.to_thread(self._atomic_write, local_path, state.artifact)
        except OSError as exc:
            raise ParserError("artifact_unavailable", provider=self.provider_name()) from exc
        if state.manifest_sha256 is None or state.file_count is None:
            raise ParserError("protocol_error", provider=self.provider_name())
        return ParserArtifactReceiptV2(
            job_id=handle.job_id,
            source_id=handle.source_id,
            size_bytes=len(state.artifact),
            sha256=digest,
            manifest_sha256=state.manifest_sha256,
            file_count=state.file_count,
        )

    async def cancel(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        state = self._live_state(handle)
        if state.state in {"queued", "running"}:
            return self._mark_cancelled(state)
        return self._snapshot(state)

    async def destroy(self, handle: ParserJobHandleV2) -> None:
        state = self._states.get(handle.provider_job_id)
        if state is None:
            if handle.provider != "fake_mineru":
                raise ParserError("invalid_configuration", provider=self.provider_name())
            return
        if state.handle != handle:
            raise ParserError("job_not_found", provider=self.provider_name())
        if state.state == "destroyed":
            return
        state.state = "destroyed"
        state.phase = "terminal"
        state.safe_error_code = None
        state.artifact = None
        state.artifact_sha256 = None
        state.manifest_sha256 = None
        state.file_count = None
        if state.finished_at_ms is None:
            state.finished_at_ms = self._clock_ms()
        self.destroyed_provider_job_ids.append(handle.provider_job_id)

    def _build_preflight(self, state: _FakeParserStateV2) -> ParserPreflightReport:
        page_count = len(state.scenario.document.pages)
        profile = state.scenario.preflight_profile
        text_pages = 0 if profile == "scanned" else page_count
        image_pages = page_count if profile == "scanned" else 0
        table_pages = page_count if profile == "complex_table" else 0
        complexity = 0.9 if profile == "scanned" else 0.85 if profile == "complex_table" else 0.1
        return ParserPreflightReport(
            source_id=state.spec.source.source_id,
            source_sha256=state.spec.source.sha256,
            page_count=page_count,
            text_page_count=text_pages,
            image_dominant_page_count=image_pages,
            multicolumn_page_count=0,
            table_candidate_page_count=table_pages,
            text_page_ratio=text_pages / page_count,
            image_dominant_page_ratio=image_pages / page_count,
            complexity_score=complexity,
            duration_ms=0,
            observed_at_ms=self._clock_ms(),
        )

    @staticmethod
    def _route(
        spec: ParserJobSpecV2,
        preflight: ParserPreflightReport,
        profile: FakePreflightProfileV2,
    ) -> ParserRouteDecision:
        del profile
        digest = canonical_preflight_sha256(preflight)
        preset, reason = {
            "pipeline": ("mineru_pipeline", "explicit_pipeline"),
            "gpu-medium": ("mineru_gpu_medium", "explicit_gpu_medium"),
            "gpu-high": ("mineru_gpu_high", "explicit_gpu_high"),
        }[spec.requested_mode]
        return ParserRouteDecision(
            requested_mode=spec.requested_mode,
            initial_parser="mineru",
            initial_preset=cast(ParserPresetName, preset),
            reasons=(cast(ParserRouteReason, reason),),
            preflight_sha256=digest,
        )

    def _run_attempt(
        self,
        state: _FakeParserStateV2,
        *,
        parser: ParserEngineName,
        preset: ParserPresetName,
        reasons: tuple[ParserRouteReason, ...],
        fallback_from_attempt_id: str | None,
    ) -> _NormalizedOutputV2 | None:
        self._verify_source_snapshot(state)
        ordinal = len(state.attempts) + 1
        attempt_id = self._attempt_id(state.handle.provider_job_id, ordinal)
        started_at = self._clock_ms()
        outcome = state.scenario.outcome
        document = state.scenario.document
        parser_version = self._parser_version(parser)
        if outcome == "failed":
            finished_at = self._clock_ms()
            state.attempts.append(
                ParserAttemptEvidence(
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    source_sha256=state.spec.source.sha256,
                    parser=parser,
                    parser_version=parser_version,
                    preset=preset,
                    routing_config=state.spec.routing_config,
                    route_reasons=reasons,
                    state="failed",
                    started_at_ms=started_at,
                    finished_at_ms=finished_at,
                    duration_ms=finished_at - started_at,
                    safe_error_code="parsing_failed",
                    fallback_from_attempt_id=fallback_from_attempt_id,
                )
            )
            return None

        try:
            normalized = self._normalize_output(state, document)
        except ParserError as exc:
            finished_at = self._clock_ms()
            state.attempts.append(
                ParserAttemptEvidence(
                    attempt_id=attempt_id,
                    ordinal=ordinal,
                    source_sha256=state.spec.source.sha256,
                    parser=parser,
                    parser_version=parser_version,
                    preset=preset,
                    routing_config=state.spec.routing_config,
                    route_reasons=reasons,
                    state="failed",
                    started_at_ms=started_at,
                    finished_at_ms=finished_at,
                    duration_ms=finished_at - started_at,
                    safe_error_code=exc.code,
                    fallback_from_attempt_id=fallback_from_attempt_id,
                )
            )
            return None

        state.phase = "quality_check"
        quality = self._quality_report(
            state,
            parser=parser,
            preset=preset,
            output=normalized,
            passed=outcome == "succeeded",
        )
        finished_at = self._clock_ms()
        attempt_state: ParserAttemptStateV2 = (
            "succeeded" if outcome == "succeeded" else "quality_rejected"
        )
        state.attempts.append(
            ParserAttemptEvidence(
                attempt_id=attempt_id,
                ordinal=ordinal,
                source_sha256=state.spec.source.sha256,
                parser=parser,
                parser_version=parser_version,
                preset=preset,
                routing_config=state.spec.routing_config,
                route_reasons=reasons,
                state=attempt_state,
                started_at_ms=started_at,
                finished_at_ms=finished_at,
                duration_ms=finished_at - started_at,
                safe_error_code=None if outcome == "succeeded" else "quality_rejected",
                quality_report=quality,
                output_size_bytes=(
                    normalized.output_size_bytes if outcome == "succeeded" else None
                ),
                output_sha256=normalized.output_sha256 if outcome == "succeeded" else None,
                fallback_from_attempt_id=fallback_from_attempt_id,
            )
        )
        return normalized if outcome == "succeeded" else None

    def _normalize_output(
        self,
        state: _FakeParserStateV2,
        document: FakeParserDocumentV2,
    ) -> _NormalizedOutputV2:
        pages = tuple(
            self._parsed_page(page_number, markdown)
            for page_number, markdown in enumerate(document.pages, start=1)
        )
        markdown = canonical_markdown_v2(pages)
        markdown_entry = self._artifact_file(
            path="parsed.md",
            kind="document_markdown",
            mime_type="text/markdown",
            payload=markdown,
        )
        files: list[tuple[ParserArtifactFileV2, bytes]] = [(markdown_entry, markdown)]
        files.extend(
            (
                self._artifact_file(
                    path=f"pages/{page.page_number:06d}.md",
                    kind="page_markdown",
                    mime_type="text/markdown",
                    payload=page.markdown.encode("utf-8"),
                    page_number=page.page_number,
                ),
                page.markdown.encode("utf-8"),
            )
            for page in pages
        )
        if len(document.assets) > state.spec.limits.max_image_count:
            raise ParserError("resource_limit", provider=self.provider_name())
        for index, asset in enumerate(document.assets, start=1):
            payload = asset.content
            if len(payload) > state.spec.limits.max_image_bytes:
                raise ParserError("resource_limit", provider=self.provider_name())
            digest = hashlib.sha256(payload).hexdigest()
            suffix = _IMAGE_SUFFIX[asset.mime_type]
            path = f"images/{asset.kind}_{index:04d}_{digest[:16]}{suffix}"
            files.append(
                (
                    self._artifact_file(
                        path=path,
                        kind=asset.kind,
                        mime_type=asset.mime_type,
                        payload=payload,
                        page_number=asset.page_number,
                    ),
                    payload,
                )
            )
        if len(files) + 1 > state.spec.limits.max_artifact_files:
            raise ParserError("resource_limit", provider=self.provider_name())
        output_size = sum(len(payload) for _, payload in files)
        tree_evidence = [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
            }
            for entry, _ in files
        ]
        tree_bytes = json.dumps(
            tree_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _NormalizedOutputV2(
            document=document,
            pages=pages,
            files=tuple(files),
            output_size_bytes=output_size,
            output_sha256=hashlib.sha256(tree_bytes).hexdigest(),
        )

    def _quality_report(
        self,
        state: _FakeParserStateV2,
        *,
        parser: ParserEngineName,
        preset: ParserPresetName,
        output: _NormalizedOutputV2,
        passed: bool,
    ) -> ParserQualityReport:
        page_count = len(output.pages)
        pages_with_text = sum(bool(page.plain_text.strip()) for page in output.pages)
        return ParserQualityReport(
            parser=parser,
            parser_version=self._parser_version(parser),
            preset=preset,
            routing_config=state.spec.routing_config,
            metrics=ParserQualityMetrics(
                input_page_count=page_count,
                output_page_count=page_count,
                pages_with_text=pages_with_text,
                output_character_count=sum(len(page.plain_text) for page in output.pages),
                text_page_coverage=pages_with_text / page_count,
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
            evaluated_at_ms=self._clock_ms(),
        )

    def _build_artifact(
        self,
        state: _FakeParserStateV2,
        *,
        preflight: ParserPreflightReport,
        output: _NormalizedOutputV2,
    ) -> tuple[bytes, str, str, int]:
        if state.route_decision is None or not state.attempts:
            raise ParserError("protocol_error", provider=self.provider_name())
        selected = state.attempts[-1]
        file_map = {entry.path: (entry, payload) for entry, payload in output.files}
        markdown = file_map.get("parsed.md")
        if markdown is None:
            raise ParserError("protocol_error", provider=self.provider_name())
        pages = tuple(
            entry
            for entry, _ in output.files
            if entry.kind == "page_markdown"
        )
        assets = tuple(
            entry
            for entry, _ in output.files
            if entry.kind in {"embedded_image", "table_image"}
        )
        manifest = ParserArtifactManifestV2(
            job_id=state.spec.job_id,
            source_id=state.spec.source.source_id,
            source_sha256=state.spec.source.sha256,
            requested_mode=state.spec.requested_mode,
            selected_attempt_id=selected.attempt_id,
            parser=selected.parser,
            parser_version=selected.parser_version,
            preset=selected.preset,
            routing_config=state.spec.routing_config,
            preflight=preflight,
            route_decision=state.route_decision,
            page_count=len(output.pages),
            quality_report=cast(ParserQualityReport, selected.quality_report),
            attempts=tuple(state.attempts),
            markdown=markdown[0],
            pages=pages,
            assets=assets,
            warnings=output.document.warnings,
        )
        manifest_bytes = (
            json.dumps(
                manifest.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            self._add_tar_file(archive, "manifest.json", manifest_bytes)
            for entry, payload in output.files:
                self._add_tar_file(archive, entry.path, payload)
        artifact = buffer.getvalue()
        if len(artifact) > state.spec.limits.max_artifact_bytes:
            raise ParserError("resource_limit", provider=self.provider_name())
        return (
            artifact,
            hashlib.sha256(artifact).hexdigest(),
            hashlib.sha256(manifest_bytes).hexdigest(),
            len(output.files) + 1,
        )

    def _snapshot(self, state: _FakeParserStateV2) -> ParserJobStatusV2:
        succeeded = state.state == "succeeded"
        return ParserJobStatusV2(
            handle=state.handle,
            state=state.state,
            phase=state.phase,
            observed_at_ms=self._clock_ms(),
            started_at_ms=state.started_at_ms,
            finished_at_ms=state.finished_at_ms,
            current_attempt_ordinal=len(state.attempts) or None,
            route_decision=state.route_decision,
            attempts=tuple(state.attempts),
            safe_error_code=state.safe_error_code,
            artifact_size_bytes=len(state.artifact) if succeeded and state.artifact else None,
            artifact_sha256=state.artifact_sha256 if succeeded else None,
        )

    def _mark_cancelled(self, state: _FakeParserStateV2) -> ParserJobStatusV2:
        state.state = "cancelled"
        state.phase = "terminal"
        state.safe_error_code = "cancelled"
        state.finished_at_ms = self._clock_ms()
        return self._snapshot(state)

    def _mark_failed(
        self,
        state: _FakeParserStateV2,
        code: ParserErrorCode,
    ) -> ParserJobStatusV2:
        state.state = "failed"
        state.phase = "terminal"
        state.safe_error_code = code
        state.finished_at_ms = self._clock_ms()
        state.artifact = None
        state.artifact_sha256 = None
        return self._snapshot(state)

    async def _wait_for_gate_or_cancel(
        self,
        signal: asyncio.Event | None,
        *,
        timeout_seconds: float,
    ) -> ParserErrorCode | None:
        assert self._completion_gate is not None
        if timeout_seconds <= 0:
            return "request_timeout"
        gate_task = asyncio.create_task(self._completion_gate.wait())
        signal_task = asyncio.create_task(signal.wait()) if signal is not None else None
        tasks = {gate_task}
        if signal_task is not None:
            tasks.add(signal_task)
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return "request_timeout"
        if signal_task is not None and signal_task in done and signal is not None:
            if signal.is_set():
                return "cancelled"
        return None

    def _state_for_status(self, handle: ParserJobHandleV2) -> _FakeParserStateV2:
        if handle.provider != "fake_mineru":
            raise ParserError("invalid_configuration", provider=self.provider_name())
        state = self._states.get(handle.provider_job_id)
        if state is None or state.handle != handle:
            raise ParserError("job_not_found", provider=self.provider_name())
        return state

    def _live_state(self, handle: ParserJobHandleV2) -> _FakeParserStateV2:
        state = self._state_for_status(handle)
        if state.state == "destroyed":
            raise ParserError("job_not_found", provider=self.provider_name())
        return state

    @staticmethod
    def _read_verified_source(path: Path, spec: ParserJobSpecV2) -> bytes:
        if not path.is_absolute():
            raise ParserError("invalid_source", provider="fake_mineru")
        try:
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                raise ParserError("invalid_source", provider="fake_mineru")
            if before.st_size > spec.limits.max_source_bytes:
                raise ParserError("source_too_large", provider="fake_mineru")
            source_bytes = path.read_bytes()
            after = path.stat(follow_symlinks=False)
        except ParserError:
            raise
        except OSError as exc:
            raise ParserError("invalid_source", provider="fake_mineru") from exc
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            raise ParserError("invalid_source", provider="fake_mineru")
        if (
            len(source_bytes) != spec.source.size_bytes
            or hashlib.sha256(source_bytes).hexdigest() != spec.source.sha256
        ):
            raise ParserError("invalid_source", provider="fake_mineru")
        return source_bytes

    @staticmethod
    def _verify_source_snapshot(state: _FakeParserStateV2) -> None:
        if (
            len(state.source_bytes) != state.spec.source.size_bytes
            or hashlib.sha256(state.source_bytes).hexdigest() != state.spec.source.sha256
        ):
            raise ParserError("invalid_source", provider="fake_mineru")

    @staticmethod
    def _parsed_page(page_number: int, markdown: str) -> ParserParsedPage:
        markdown_bytes = markdown.encode("utf-8")
        plain_text = markdown
        plain_bytes = plain_text.encode("utf-8")
        return ParserParsedPage(
            page_number=page_number,
            markdown=markdown,
            markdown_sha256=hashlib.sha256(markdown_bytes).hexdigest(),
            plain_text=plain_text,
            plain_text_sha256=hashlib.sha256(plain_bytes).hexdigest(),
        )

    @staticmethod
    def _artifact_file(
        *,
        path: str,
        kind: Literal[
            "document_markdown",
            "page_markdown",
            "embedded_image",
            "table_image",
        ],
        mime_type: str,
        payload: bytes,
        page_number: int | None = None,
    ) -> ParserArtifactFileV2:
        return ParserArtifactFileV2(
            path=path,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            page_number=page_number,
        )

    @staticmethod
    def _parser_version(parser: ParserEngineName) -> str:
        del parser
        return _FAKE_MINERU_VERSION

    @staticmethod
    def _phase_for_parser(parser: ParserEngineName) -> ParserJobPhaseV2:
        del parser
        return "mineru_parse"

    @staticmethod
    def _attempt_id(provider_job_id: str, ordinal: int) -> str:
        digest = hashlib.sha256(provider_job_id.encode("utf-8")).hexdigest()[:24]
        return f"attempt-{ordinal}-{digest}"

    @staticmethod
    def _add_tar_file(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = 0
        info.mode = 0o600
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        archive.addfile(info, io.BytesIO(payload))

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def __repr__(self) -> str:
        active = sum(state.state != "destroyed" for state in self._states.values())
        return (
            "FakeMineruParserProvider("
            f"active={active}, queued_scenarios={len(self._scenarios)})"
        )


__all__ = [
    "FakeAttemptOutcomeV2",
    "FakeMineruParserProvider",
    "FakeParserAssetV2",
    "FakeParserDocumentV2",
    "FakeParserScenarioV2",
    "FakePreflightProfileV2",
]
