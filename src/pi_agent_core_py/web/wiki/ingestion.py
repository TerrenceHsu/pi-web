"""Provider-neutral PDF/HTML raw-ingestion orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from wiki_parser import (
    ParserArtifactManifestV2,
    ParserAttemptEvidence,
    ParserError,
    ParserJobHandle,
    ParserJobHandleV2,
    ParserJobSpec,
    ParserJobSpecV2,
    ParserJobStatusV2,
    ParserLimits,
    ParserProvider,
    ParserProviderV2,
    ParserRequestedMode,
    ParserSourceSpec,
)

from .artifact_import import import_parser_artifact, import_parser_artifact_v2
from .errors import WikiStoreError
from .html_parser import (
    HTML_PARSER_VERSION,
    HtmlParseResult,
    HtmlParserLimits,
    parse_single_html,
)
from .models import (
    WikiArtifact,
    WikiJob,
    WikiParseAttempt,
    WikiParseMode,
    WikiSource,
    WikiSourceMimeType,
)
from .store import WikiStore

HTML_ARTIFACT_SCHEMA = "llm-wiki-html-artifact/v1"
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WikiIngestionLimits:
    pdf: ParserLimits = ParserLimits()
    html: HtmlParserLimits = HtmlParserLimits()


@dataclass(frozen=True, slots=True)
class WikiParseOutcome:
    source: WikiSource
    job: WikiJob


class WikiIngestionService:
    """Persist raw sources and parse them without trusting provider output."""

    def __init__(
        self,
        store: WikiStore,
        *,
        pdf_provider: ParserProvider | None = None,
        pdf_provider_v2: ParserProviderV2 | None = None,
        limits: WikiIngestionLimits | None = None,
    ) -> None:
        if pdf_provider is not None and pdf_provider_v2 is not None:
            raise ValueError("configure exactly one Wiki PDF provider contract")
        self._store = store
        self._pdf_provider = pdf_provider
        self._pdf_provider_v2 = pdf_provider_v2
        self._limits = limits or WikiIngestionLimits()

    def can_parse(self, source: WikiSource) -> bool:
        return source.mime_type == "text/html" or (
            self._pdf_provider is not None or self._pdf_provider_v2 is not None
        )

    def resolve_parse_mode(
        self,
        source: WikiSource,
        requested_mode: WikiParseMode | None = None,
    ) -> WikiParseMode:
        """Resolve and validate a mode before work is admitted to the queue."""

        return self.resolve_parse_mode_for_mime(source.mime_type, requested_mode)

    def resolve_parse_mode_for_mime(
        self,
        mime_type: WikiSourceMimeType,
        requested_mode: WikiParseMode | None = None,
    ) -> WikiParseMode:
        """Validate upload-time mode semantics before persisting source bytes."""

        if mime_type == "text/html":
            if requested_mode not in {None, "builtin"}:
                raise WikiStoreError("unsupported_parse_mode")
            return "builtin"
        if self._pdf_provider_v2 is not None:
            mode = "auto" if requested_mode is None else requested_mode
            if mode not in {"auto", "fast", "accurate"}:
                raise WikiStoreError("unsupported_parse_mode")
            return mode
        if self._pdf_provider is not None:
            mode = "fast" if requested_mode is None else requested_mode
            if mode != "fast":
                raise WikiStoreError("unsupported_parse_mode")
            return mode
        mode = "fast" if requested_mode is None else requested_mode
        if mode not in {"auto", "fast", "accurate"}:
            raise WikiStoreError("unsupported_parse_mode")
        return mode

    def max_source_bytes(self, mime_type: WikiSourceMimeType) -> int:
        return (
            self._limits.pdf.max_source_bytes
            if mime_type == "application/pdf"
            else self._limits.html.max_source_bytes
        )

    async def upload_source(
        self,
        space_id: str,
        *,
        display_name: str,
        mime_type: WikiSourceMimeType,
        content: bytes,
    ) -> WikiSource:
        self._validate_upload(display_name, mime_type, content)
        max_bytes = (
            self._limits.pdf.max_source_bytes
            if mime_type == "application/pdf"
            else self._limits.html.max_source_bytes
        )
        return await self._store.upload_source(
            space_id,
            display_name=display_name,
            mime_type=mime_type,
            content=content,
            max_bytes=max_bytes,
        )

    async def parse_source(
        self,
        source_id: str,
        *,
        requested_mode: WikiParseMode | None = None,
        signal: asyncio.Event | None = None,
    ) -> WikiParseOutcome:
        pending = await self._store.get_source(source_id)
        resolved_mode = self.resolve_parse_mode(pending, requested_mode)
        source, job = await self._store.begin_parse_job(
            source_id,
            requested_mode=resolved_mode,
        )
        job = await self._store.set_job_status(job.id, "running")
        try:
            if signal is not None and signal.is_set():
                raise asyncio.CancelledError
            if source.mime_type == "text/html":
                completed = await self._parse_html(source, job)
            else:
                completed = await self._parse_pdf(source, job, signal=signal)
        except asyncio.CancelledError:
            await self._mark_failed(source, job, "cancelled", cancelled=True)
            raise
        except ParserError as exc:
            return await self._mark_failed(source, job, exc.code)
        except WikiStoreError as exc:
            mapped_codes = {
                "file_too_large": "resource_limit",
                "invalid_source": "invalid_source",
                "invalid_artifact": "artifact_invalid",
                "file_exists": "artifact_invalid",
                "path_unsafe": "artifact_invalid",
            }
            code = mapped_codes.get(exc.code, "unknown_error")
            if code == "unknown_error":
                _logger.exception(
                    "Wiki parse hit an unclassified store error "
                    "(source_id=%s, job_id=%s, error_code=%s)",
                    source.id,
                    job.id,
                    exc.code,
                )
            return await self._mark_failed(source, job, code)
        except Exception as exc:
            _logger.exception(
                "Wiki parse failed unexpectedly (source_id=%s, job_id=%s, error_type=%s)",
                source.id,
                job.id,
                type(exc).__name__,
            )
            return await self._mark_failed(source, job, "unknown_error")
        job = await self._store.set_job_status(job.id, "succeeded")
        return WikiParseOutcome(source=completed, job=job)

    async def _parse_html(self, source: WikiSource, job: WikiJob) -> WikiSource:
        content = await asyncio.to_thread(
            self._store.file_store.read_owned_file,
            source.space_id,
            source.source_relpath,
            max_bytes=self._limits.html.max_source_bytes,
        )
        if (
            len(content) != source.size_bytes
            or hashlib.sha256(content).hexdigest() != source.source_sha256
        ):
            raise WikiStoreError("invalid_source")
        result = await asyncio.to_thread(
            parse_single_html,
            content,
            limits=self._limits.html,
        )
        revision_id = self._store.new_parse_revision_id()
        artifacts = await asyncio.to_thread(
            self._materialize_html,
            source,
            revision_id,
            result,
        )
        return await self._complete_legacy_revision(
            source,
            job,
            revision_id=revision_id,
            artifacts=artifacts,
            contract_version=1,
            artifact_schema=HTML_ARTIFACT_SCHEMA,
            parser="html_builtin",
            parser_version=HTML_PARSER_VERSION,
            preset="html_single_file",
            provider_attempt_id=job.id,
            route_reasons=("builtin_html",),
            page_count=1,
        )

    async def _parse_pdf(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        signal: asyncio.Event | None,
    ) -> WikiSource:
        if self._pdf_provider_v2 is not None:
            return await self._parse_pdf_v2(source, job, signal=signal)
        return await self._parse_pdf_v1(source, job, signal=signal)

    async def _parse_pdf_v2(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        signal: asyncio.Event | None,
    ) -> WikiSource:
        provider = self._pdf_provider_v2
        if provider is None or job.requested_mode not in {"auto", "fast", "accurate"}:
            raise ParserError("invalid_configuration")
        probe = await provider.probe()
        if not probe.available:
            raise ParserError(probe.error_code or "provider_unavailable", provider=probe.provider)
        if provider.provider_name() != probe.provider:
            raise ParserError("protocol_error", provider=probe.provider)
        source_path = await asyncio.to_thread(
            self._store.file_store.resolve_owned_regular_file,
            source.space_id,
            source.source_relpath,
        )
        requested_mode = cast(ParserRequestedMode, job.requested_mode)
        spec = ParserJobSpecV2(
            job_id=job.id,
            source=ParserSourceSpec(
                source_id=source.id,
                display_name=source.display_name,
                mime_type="application/pdf",
                size_bytes=source.size_bytes,
                sha256=source.source_sha256,
            ),
            requested_mode=requested_mode,
            routing_config=probe.routing_config,
            limits=self._limits.pdf,
        )
        handle: ParserJobHandleV2 | None = None
        status: ParserJobStatusV2 | None = None
        wiki_attempts: tuple[WikiParseAttempt, ...] = ()
        try:
            handle = await provider.create_job(spec, source_path=source_path)
            status = await provider.wait(handle, signal=signal)
            self._validate_v2_status_identity(status, handle, source, job)
            wiki_attempts = self._map_v2_attempts(source, job, status.attempts)
            if status.state == "cancelled":
                if wiki_attempts:
                    await self._store.record_terminal_parse_attempts(job.id, wiki_attempts)
                raise asyncio.CancelledError
            if status.state != "succeeded" or status.artifact_sha256 is None:
                if wiki_attempts:
                    await self._store.record_terminal_parse_attempts(job.id, wiki_attempts)
                raise ParserError(
                    status.safe_error_code or "parsing_failed",
                    provider=probe.provider,
                )
            try:
                with tempfile.TemporaryDirectory(prefix="llm-wiki-parser-v2-") as staging:
                    archive_path = Path(staging) / "artifact.tar"
                    receipt = await provider.download_artifact(
                        handle,
                        local_path=archive_path,
                        expected_sha256=status.artifact_sha256,
                    )
                    revision_id = self._store.new_parse_revision_id()
                    imported = await asyncio.to_thread(
                        import_parser_artifact_v2,
                        self._store,
                        source,
                        job_id=job.id,
                        requested_mode=requested_mode,
                        archive_path=archive_path,
                        receipt=receipt,
                        status=status,
                        limits=self._limits.pdf,
                        expected_provider=probe.provider,
                        expected_routing_config=probe.routing_config,
                        parse_revision_id=revision_id,
                    )
            except BaseException:
                if wiki_attempts:
                    await self._store.record_terminal_parse_attempts(job.id, wiki_attempts)
                raise
            try:
                return await self._complete_v2_revision(
                    source,
                    job,
                    revision_id=revision_id,
                    artifacts=imported.artifacts,
                    manifest=imported.manifest,
                    attempts=wiki_attempts,
                )
            except BaseException:
                try:
                    if wiki_attempts:
                        await self._store.record_terminal_parse_attempts(job.id, wiki_attempts)
                except WikiStoreError:
                    pass
                raise
        finally:
            if handle is not None:
                try:
                    await provider.destroy(handle)
                except Exception:
                    pass

    async def _parse_pdf_v1(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        signal: asyncio.Event | None,
    ) -> WikiSource:
        provider = self._pdf_provider
        if provider is None:
            raise ParserError("invalid_configuration")
        probe = await provider.probe()
        if not probe.available:
            raise ParserError(probe.error_code or "provider_unavailable", provider=probe.provider)
        if provider.provider_name() != probe.provider:
            raise ParserError("protocol_error", provider=probe.provider)
        source_path = await asyncio.to_thread(
            self._store.file_store.resolve_owned_regular_file,
            source.space_id,
            source.source_relpath,
        )
        spec = ParserJobSpec(
            job_id=job.id,
            source=ParserSourceSpec(
                source_id=source.id,
                display_name=source.display_name,
                mime_type="application/pdf",
                size_bytes=source.size_bytes,
                sha256=source.source_sha256,
            ),
            limits=self._limits.pdf,
        )
        handle: ParserJobHandle | None = None
        try:
            handle = await provider.create_job(spec, source_path=source_path)
            status = await provider.wait(handle, signal=signal)
            if status.state == "cancelled":
                await self._record_legacy_pdf_failure(
                    source,
                    job,
                    provider_attempt_id=handle.provider_job_id,
                    parser=probe.provider,
                    parser_version=probe.provider_version,
                    state="cancelled",
                    safe_error_code="cancelled",
                    started_at_ms=status.started_at_ms,
                    finished_at_ms=status.finished_at_ms,
                )
                raise asyncio.CancelledError
            if status.state != "succeeded" or status.artifact_sha256 is None:
                error_code = status.safe_error_code or "parsing_failed"
                await self._record_legacy_pdf_failure(
                    source,
                    job,
                    provider_attempt_id=handle.provider_job_id,
                    parser=probe.provider,
                    parser_version=probe.provider_version,
                    state="failed",
                    safe_error_code=error_code,
                    started_at_ms=status.started_at_ms,
                    finished_at_ms=status.finished_at_ms,
                )
                raise ParserError(
                    error_code,
                    provider=probe.provider,
                )
            with tempfile.TemporaryDirectory(prefix="llm-wiki-parser-") as staging:
                archive_path = Path(staging) / "artifact.tar"
                receipt = await provider.download_artifact(
                    handle,
                    local_path=archive_path,
                    expected_sha256=status.artifact_sha256,
                )
                revision_id = self._store.new_parse_revision_id()
                imported = await asyncio.to_thread(
                    import_parser_artifact,
                    self._store,
                    source,
                    job_id=job.id,
                    archive_path=archive_path,
                    receipt=receipt,
                    limits=self._limits.pdf,
                    expected_provider=probe.provider,
                    expected_provider_version=probe.provider_version,
                    parse_revision_id=revision_id,
                )
            return await self._complete_legacy_revision(
                source,
                job,
                revision_id=revision_id,
                artifacts=imported.artifacts,
                contract_version=1,
                artifact_schema=imported.manifest.schema_id,
                parser=imported.manifest.provider,
                parser_version=imported.manifest.provider_version,
                preset=imported.manifest.mode,
                provider_attempt_id=handle.provider_job_id,
                route_reasons=("legacy_contract_v1",),
                page_count=imported.page_count,
            )
        finally:
            if handle is not None:
                try:
                    await provider.destroy(handle)
                except Exception:
                    pass

    @staticmethod
    def _validate_v2_status_identity(
        status: ParserJobStatusV2,
        handle: ParserJobHandleV2,
        source: WikiSource,
        job: WikiJob,
    ) -> None:
        if (
            status.handle != handle
            or handle.job_id != job.id
            or handle.source_id != source.id
            or status.state not in {"succeeded", "failed", "cancelled"}
            or any(
                attempt.source_sha256 != source.source_sha256
                or attempt.routing_config.revision == ""
                or attempt.state not in {"succeeded", "failed", "quality_rejected", "cancelled"}
                for attempt in status.attempts
            )
            or (
                status.route_decision is not None
                and status.route_decision.requested_mode != job.requested_mode
            )
            or (status.attempts and status.route_decision is None)
        ):
            raise ParserError("protocol_error", provider=handle.provider)

    def _map_v2_attempts(
        self,
        source: WikiSource,
        job: WikiJob,
        evidence: tuple[ParserAttemptEvidence, ...],
    ) -> tuple[WikiParseAttempt, ...]:
        mapped: list[WikiParseAttempt] = []
        internal_id_by_provider_id: dict[str, str] = {}
        for item in evidence:
            fallback_id = None
            if item.fallback_from_attempt_id is not None:
                fallback_id = internal_id_by_provider_id.get(item.fallback_from_attempt_id)
                if fallback_id is None:
                    raise ParserError("protocol_error", provider="wiki_ingestion")
            quality_json = "{}"
            if item.quality_report is not None:
                quality_json = json.dumps(
                    item.quality_report.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            attempt = self._store.new_parse_attempt(
                source,
                job,
                provider_attempt_id=item.attempt_id,
                ordinal=item.ordinal,
                parser=item.parser,
                parser_version=item.parser_version,
                preset=item.preset,
                route_reasons=item.route_reasons,
                state=item.state,
                safe_error_code=item.safe_error_code or "",
                quality_report_json=quality_json,
                output_size_bytes=item.output_size_bytes,
                output_sha256=item.output_sha256,
                fallback_from_attempt_id=fallback_id,
                routing_config_revision=item.routing_config.revision,
                routing_config_sha256=item.routing_config.sha256,
                started_at_ms=item.started_at_ms,
                finished_at_ms=item.finished_at_ms,
            )
            internal_id_by_provider_id[item.attempt_id] = attempt.id
            mapped.append(attempt)
        return tuple(mapped)

    async def _complete_v2_revision(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        revision_id: str,
        artifacts: tuple[WikiArtifact, ...],
        manifest: ParserArtifactManifestV2,
        attempts: tuple[WikiParseAttempt, ...],
    ) -> WikiSource:
        markdown = next(
            (artifact for artifact in artifacts if artifact.kind == "parsed_markdown"),
            None,
        )
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "manifest"),
            None,
        )
        selected_attempt = next(
            (
                attempt
                for attempt in attempts
                if attempt.provider_attempt_id == manifest.selected_attempt_id
            ),
            None,
        )
        if markdown is None or manifest_artifact is None or selected_attempt is None:
            raise WikiStoreError("invalid_artifact")
        revision = self._store.new_parse_revision(
            source,
            job,
            selected_attempt,
            revision_id=revision_id,
            contract_version=2,
            artifact_schema=manifest.schema_id,
            parsed_markdown_relpath=markdown.relpath,
            parsed_markdown_sha256=markdown.sha256,
            manifest_relpath=manifest_artifact.relpath,
            manifest_sha256=manifest_artifact.sha256,
            page_count=manifest.page_count,
        )
        return await self._store.complete_source_parse(
            source.id,
            job_id=job.id,
            attempts=attempts,
            revision=revision,
            artifacts=artifacts,
        )

    def _materialize_html(
        self,
        source: WikiSource,
        revision_id: str,
        result: HtmlParseResult,
    ) -> tuple[WikiArtifact, ...]:
        bundle_dir = PurePosixPath(
            self._store.file_store.parse_revision_relative_path(source, revision_id)
        )
        markdown = result.markdown.encode("utf-8")
        markdown_sha = hashlib.sha256(markdown).hexdigest()
        parsed_relpath = str(bundle_dir / "parsed.md")
        image_manifest: list[dict[str, object]] = []
        artifacts: list[WikiArtifact] = []
        self._store.file_store.write_owned_file_once_or_verify(
            source.space_id,
            parsed_relpath,
            markdown,
            max_bytes=self._limits.html.max_markdown_bytes,
        )
        artifacts.append(
            self._store.new_artifact(
                source.id,
                parse_revision_id=revision_id,
                kind="parsed_markdown",
                relpath=parsed_relpath,
                mime_type="text/markdown",
                size_bytes=len(markdown),
                sha256=markdown_sha,
            )
        )
        for image in result.images:
            relpath = str(bundle_dir / image.path)
            self._store.file_store.write_owned_file_once_or_verify(
                source.space_id,
                relpath,
                image.content,
                max_bytes=self._limits.html.max_image_bytes,
            )
            artifacts.append(
                self._store.new_artifact(
                    source.id,
                    parse_revision_id=revision_id,
                    kind="embedded_image",
                    relpath=relpath,
                    mime_type=image.mime_type,
                    size_bytes=len(image.content),
                    sha256=image.sha256,
                )
            )
            image_manifest.append(
                {
                    "path": image.path,
                    "mime_type": image.mime_type,
                    "size_bytes": len(image.content),
                    "sha256": image.sha256,
                }
            )
        page_relpath = str(bundle_dir / "pages/000001.md")
        self._store.file_store.write_owned_file_once_or_verify(
            source.space_id,
            page_relpath,
            markdown,
            max_bytes=self._limits.html.max_markdown_bytes,
        )
        artifacts.append(
            self._store.new_artifact(
                source.id,
                parse_revision_id=revision_id,
                kind="page_markdown",
                relpath=page_relpath,
                mime_type="text/markdown",
                size_bytes=len(markdown),
                sha256=markdown_sha,
                source_locator_json='{"page_number":1}',
            )
        )
        manifest = {
            "schema": HTML_ARTIFACT_SCHEMA,
            "source_id": source.id,
            "source_sha256": source.source_sha256,
            "parser": {"provider": "html_builtin", "version": HTML_PARSER_VERSION},
            "markdown": {
                "path": "parsed.md",
                "mime_type": "text/markdown",
                "size_bytes": len(markdown),
                "sha256": markdown_sha,
            },
            "images": image_manifest,
            "warnings": list(result.warnings),
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        manifest_relpath = str(bundle_dir / "manifest.json")
        self._store.file_store.write_owned_file_once_or_verify(
            source.space_id,
            manifest_relpath,
            manifest_bytes,
            max_bytes=1024 * 1024,
        )
        artifacts.append(
            self._store.new_artifact(
                source.id,
                parse_revision_id=revision_id,
                kind="manifest",
                relpath=manifest_relpath,
                mime_type="application/json",
                size_bytes=len(manifest_bytes),
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        )
        return tuple(artifacts)

    async def _complete_legacy_revision(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        revision_id: str,
        artifacts: tuple[WikiArtifact, ...],
        contract_version: int,
        artifact_schema: str,
        parser: str,
        parser_version: str,
        preset: str,
        provider_attempt_id: str,
        route_reasons: tuple[str, ...],
        page_count: int,
    ) -> WikiSource:
        markdown = next(
            (artifact for artifact in artifacts if artifact.kind == "parsed_markdown"),
            None,
        )
        manifest = next(
            (artifact for artifact in artifacts if artifact.kind == "manifest"),
            None,
        )
        if markdown is None or manifest is None or job.started_at_ms is None:
            raise WikiStoreError("invalid_artifact")
        evidence = json.dumps(
            [
                {
                    "kind": artifact.kind,
                    "path": artifact.relpath,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in sorted(artifacts, key=lambda item: item.relpath)
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        finished_at = max(
            job.started_at_ms,
            *(artifact.created_at_ms for artifact in artifacts),
        )
        attempt = self._store.new_parse_attempt(
            source,
            job,
            provider_attempt_id=provider_attempt_id,
            ordinal=1,
            parser=parser,
            parser_version=parser_version,
            preset=preset,
            route_reasons=route_reasons,
            state="succeeded",
            output_size_bytes=sum(artifact.size_bytes for artifact in artifacts),
            output_sha256=hashlib.sha256(evidence).hexdigest(),
            started_at_ms=job.started_at_ms,
            finished_at_ms=finished_at,
        )
        revision = self._store.new_parse_revision(
            source,
            job,
            attempt,
            revision_id=revision_id,
            contract_version=contract_version,
            artifact_schema=artifact_schema,
            parsed_markdown_relpath=markdown.relpath,
            parsed_markdown_sha256=markdown.sha256,
            manifest_relpath=manifest.relpath,
            manifest_sha256=manifest.sha256,
            page_count=page_count,
        )
        return await self._store.complete_source_parse(
            source.id,
            job_id=job.id,
            attempts=(attempt,),
            revision=revision,
            artifacts=artifacts,
        )

    async def _record_legacy_pdf_failure(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        provider_attempt_id: str,
        parser: str,
        parser_version: str,
        state: Literal["failed", "cancelled"],
        safe_error_code: str,
        started_at_ms: int | None,
        finished_at_ms: int | None,
    ) -> None:
        started = started_at_ms or job.started_at_ms
        if started is None:
            raise WikiStoreError("invalid_artifact")
        finished = max(finished_at_ms or started, started)
        attempt = self._store.new_parse_attempt(
            source,
            job,
            provider_attempt_id=provider_attempt_id,
            ordinal=1,
            parser=parser,
            parser_version=parser_version,
            preset="fast_no_ocr",
            route_reasons=("legacy_contract_v1",),
            state=state,
            safe_error_code=safe_error_code,
            started_at_ms=started,
            finished_at_ms=finished,
        )
        await self._store.record_unsuccessful_parse_attempts(job.id, (attempt,))

    async def _mark_failed(
        self,
        source: WikiSource,
        job: WikiJob,
        safe_error_code: str,
        *,
        cancelled: bool = False,
    ) -> WikiParseOutcome:
        try:
            failed_source = await self._store.set_source_status(
                source.id,
                "failed",
                safe_error_code=safe_error_code,
            )
        except WikiStoreError:
            failed_source = await self._store.get_source(source.id)
        try:
            failed_job = await self._store.set_job_status(
                job.id,
                "cancelled" if cancelled else "failed",
                safe_error_code=safe_error_code,
            )
        except WikiStoreError:
            failed_job = await self._store.get_job(job.id)
        return WikiParseOutcome(source=failed_source, job=failed_job)

    @staticmethod
    def _validate_upload(
        display_name: str,
        mime_type: WikiSourceMimeType,
        content: bytes,
    ) -> None:
        lowered = display_name.casefold()
        if mime_type == "application/pdf":
            if not lowered.endswith(".pdf") or not content.startswith(b"%PDF-"):
                raise WikiStoreError("invalid_source")
            return
        if mime_type == "text/html":
            if not lowered.endswith(".html"):
                raise WikiStoreError("invalid_source")
            try:
                content.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as exc:
                raise WikiStoreError("invalid_source") from exc
            return
        raise WikiStoreError("invalid_source")


__all__ = [
    "HTML_ARTIFACT_SCHEMA",
    "WikiIngestionLimits",
    "WikiIngestionService",
    "WikiParseOutcome",
]
