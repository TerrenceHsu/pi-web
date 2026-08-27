"""Local-only REST API for Wiki Spaces, immutable Sources and Raw artifacts."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ..credentials.api import (
    SafeValidationErrorResponse,
    require_allowed_origin_dep,
    require_ui_header_dep,
)
from ..local_web_security import WebSecurityConfig
from .changes import WikiChangeSetService
from .conversations import WikiConversationService
from .errors import WikiStoreError
from .ingestion import WikiIngestionService
from .models import (
    WikiArtifact,
    WikiChangeSet,
    WikiChangeSetItem,
    WikiConversation,
    WikiGraphSnapshot,
    WikiJob,
    WikiPage,
    WikiPageProposal,
    WikiPageRevision,
    WikiPageSearchResult,
    WikiParseAttempt,
    WikiParseRevision,
    WikiSource,
    WikiSourceMimeType,
    WikiSourceSummaryDraft,
    WikiSpace,
)
from .pages import WikiEntryPageService
from .store import WikiStore
from .summary import WikiSummaryService
from .worker import WikiIngestionWorkerManager

_READ_CHUNK_BYTES = 1024 * 1024


class WikiSpaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


class WikiSpacePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)


class WikiSpaceStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "archived"]


class WikiSourceUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: WikiSource
    parse_queued: bool


class WikiParseQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    queued: bool


class WikiParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parse_mode: Literal["auto", "fast", "accurate"] = "auto"


class WikiChangeSetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_set: WikiChangeSet
    items: tuple[WikiChangeSetItem, ...]


class WikiChangeSetDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class WikiChangeSetDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_set: WikiChangeSet
    pages: tuple[WikiPage, ...]


class WikiConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="New Knowledge conversation", min_length=1, max_length=160)


class WikiConversationStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "archived"]


async def _get_store(request: Request) -> WikiStore:
    store = getattr(request.app.state.web, "wiki_store", None)
    if not isinstance(store, WikiStore) or store.closed:
        raise _safe_error(503, "invalid_configuration")
    return store


async def _get_ingestion(request: Request) -> WikiIngestionService:
    service = getattr(request.app.state.web, "wiki_ingestion_service", None)
    if not isinstance(service, WikiIngestionService):
        raise _safe_error(503, "invalid_configuration")
    return service


async def _get_worker(request: Request) -> WikiIngestionWorkerManager:
    worker = getattr(request.app.state.web, "wiki_ingestion_worker", None)
    if not isinstance(worker, WikiIngestionWorkerManager) or not worker.running:
        raise _safe_error(503, "invalid_configuration")
    return worker


async def _get_summary_service(request: Request) -> WikiSummaryService:
    service = getattr(request.app.state.web, "wiki_summary_service", None)
    if not isinstance(service, WikiSummaryService):
        raise _safe_error(503, "invalid_configuration")
    return service


async def _get_entry_page_service(request: Request) -> WikiEntryPageService:
    service = getattr(request.app.state.web, "wiki_entry_page_service", None)
    if not isinstance(service, WikiEntryPageService):
        raise _safe_error(503, "invalid_configuration")
    return service


async def _get_change_set_service(request: Request) -> WikiChangeSetService:
    service = getattr(request.app.state.web, "wiki_change_set_service", None)
    if not isinstance(service, WikiChangeSetService):
        raise _safe_error(503, "invalid_configuration")
    return service


async def _get_conversation_service(request: Request) -> WikiConversationService:
    service = getattr(request.app.state.web, "wiki_conversation_service", None)
    if not isinstance(service, WikiConversationService):
        raise _safe_error(503, "invalid_configuration")
    return service


def _safe_error(status_code: int, code: str) -> HTTPException:
    messages = {
        "invalid_configuration": "Wiki ingestion is not available.",
        "invalid_identifier": "Wiki identifier is invalid.",
        "invalid_space": "Wiki space data is invalid.",
        "space_not_found": "Wiki space does not exist.",
        "space_conflict": "Wiki space conflicts with another operation.",
        "space_in_use": "Wiki space still contains active content or work.",
        "space_read_only": "Wiki space is archived and read-only.",
        "invalid_source": "Wiki source is invalid.",
        "unsupported_parse_mode": "Wiki parser does not support the requested mode.",
        "source_not_found": "Wiki source does not exist.",
        "source_conflict": "Wiki source conflicts with another operation.",
        "source_in_use": "Wiki source is still referenced by an active Wiki page.",
        "source_not_available": "Wiki source Raw content is no longer available.",
        "artifact_not_found": "Wiki artifact does not exist.",
        "summary_not_found": "Wiki source summary does not exist.",
        "invalid_summary": "Wiki source summary is invalid.",
        "summary_generation_failed": "Wiki source summary generation failed.",
        "summary_too_large": "Wiki source is too large for summarization.",
        "page_proposal_not_found": "Wiki page proposal does not exist.",
        "invalid_page_proposal": "Wiki page proposal is invalid.",
        "page_proposal_conflict": "Wiki page proposal conflicts with another proposal.",
        "change_set_not_found": "Wiki change set does not exist.",
        "invalid_change_set": "Wiki change set is invalid.",
        "change_set_conflict": "Wiki change set conflicts with another operation.",
        "change_set_stale": "Wiki change set is stale and was not published.",
        "invalid_conversation": "Wiki conversation is invalid.",
        "conversation_not_found": "Wiki conversation does not exist.",
        "conversation_conflict": "Wiki conversation conflicts with another operation.",
        "page_not_found": "Wiki page does not exist.",
        "page_revision_not_found": "Wiki page revision does not exist.",
        "invalid_search_query": "Wiki page search query is invalid.",
        "job_not_found": "Wiki job does not exist.",
        "file_too_large": "Wiki source exceeds the configured limit.",
    }
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": messages.get(code, "Wiki operation failed.")},
    )


def _translate_error(exc: WikiStoreError) -> HTTPException:
    if exc.code in {
        "space_not_found",
        "source_not_found",
        "artifact_not_found",
        "summary_not_found",
        "page_proposal_not_found",
        "change_set_not_found",
        "conversation_not_found",
        "page_not_found",
        "page_revision_not_found",
        "job_not_found",
    }:
        return _safe_error(404, exc.code)
    if exc.code in {
        "space_conflict",
        "space_in_use",
        "space_read_only",
        "source_conflict",
        "source_in_use",
        "job_conflict",
    }:
        return _safe_error(409, exc.code)
    if exc.code == "source_not_available":
        return _safe_error(410, exc.code)
    if exc.code in {
        "page_proposal_conflict",
        "change_set_conflict",
        "change_set_stale",
        "conversation_conflict",
    }:
        return _safe_error(409, exc.code)
    if exc.code == "file_too_large":
        return _safe_error(413, exc.code)
    if exc.code == "summary_too_large":
        return _safe_error(413, exc.code)
    if exc.code in {"invalid_summary", "summary_generation_failed"}:
        return _safe_error(502, exc.code)
    if exc.code == "invalid_configuration":
        return _safe_error(503, exc.code)
    return _safe_error(400, exc.code)


def _infer_mime_type(filename: str) -> WikiSourceMimeType:
    lowered = filename.casefold()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".html"):
        return "text/html"
    raise WikiStoreError("invalid_source")


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    content = bytearray()
    try:
        while True:
            chunk = await upload.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            if len(content) + len(chunk) > max_bytes:
                raise WikiStoreError("file_too_large")
            content.extend(chunk)
    finally:
        await upload.close()
    if not content:
        raise WikiStoreError("invalid_source")
    return bytes(content)


def _content_response(content: bytes, *, media_type: str, filename: str) -> Response:
    encoded = quote(filename, safe="")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{encoded}",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def build_wiki_router(config: WebSecurityConfig) -> APIRouter:
    router = APIRouter(
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
        responses={
            422: {
                "model": SafeValidationErrorResponse,
                "description": "Request validation failed.",
            }
        },
    )

    @router.get("/spaces")
    async def list_spaces(
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiSpace]:
        return list(await store.list_spaces())

    @router.post("/spaces", status_code=status.HTTP_201_CREATED)
    async def create_space(
        body: WikiSpaceCreateRequest,
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSpace:
        try:
            return await store.create_space(name=body.name, description=body.description)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}")
    async def get_space(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSpace:
        try:
            return await store.get_space(space_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/spaces/{space_id}/conversations",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation(
        space_id: Annotated[str, Path()],
        body: WikiConversationCreateRequest,
        service: Annotated[
            WikiConversationService,
            Depends(_get_conversation_service),
        ],
    ) -> WikiConversation:
        try:
            return await service.create(space_id, title=body.title)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/conversations")
    async def list_conversations(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiConversation]:
        try:
            return list(await store.list_conversations(space_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiConversation:
        try:
            return await store.get_conversation(conversation_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.patch("/conversations/{conversation_id}/status")
    async def set_conversation_status(
        conversation_id: Annotated[str, Path()],
        body: WikiConversationStatusRequest,
        service: Annotated[
            WikiConversationService,
            Depends(_get_conversation_service),
        ],
    ) -> WikiConversation:
        try:
            return await service.set_status(
                conversation_id,
                body.status,
            )
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.patch("/spaces/{space_id}")
    async def patch_space(
        space_id: Annotated[str, Path()],
        body: WikiSpacePatchRequest,
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSpace:
        try:
            return await store.update_space(
                space_id,
                name=body.name,
                description=body.description,
            )
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.patch("/spaces/{space_id}/status")
    async def set_space_status(
        space_id: Annotated[str, Path()],
        body: WikiSpaceStatusRequest,
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSpace:
        try:
            return await store.set_space_status(space_id, body.status)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.delete("/spaces/{space_id}")
    async def delete_space(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSpace:
        try:
            return await store.delete_space(space_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/sources")
    async def list_sources(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiSource]:
        try:
            await store.get_space(space_id)
            return list(await store.list_sources(space_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/spaces/{space_id}/sources",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_source(
        space_id: Annotated[str, Path()],
        file: Annotated[UploadFile, File(...)],
        service: Annotated[WikiIngestionService, Depends(_get_ingestion)],
        worker: Annotated[WikiIngestionWorkerManager, Depends(_get_worker)],
        parse_mode: Annotated[Literal["auto", "fast", "accurate"] | None, Form()] = None,
    ) -> WikiSourceUploadResponse:
        filename = file.filename or ""
        try:
            mime_type = _infer_mime_type(filename)
            resolved_mode = service.resolve_parse_mode_for_mime(mime_type, parse_mode)
            max_bytes = service.max_source_bytes(mime_type)
            content = await _read_upload(file, max_bytes)
            source = await service.upload_source(
                space_id,
                display_name=filename,
                mime_type=mime_type,
                content=content,
            )
            queued = False
            if service.can_parse(source):
                queued = await worker.enqueue(source.id, requested_mode=resolved_mode)
            return WikiSourceUploadResponse(source=source, parse_queued=queued)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}")
    async def get_source(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSource:
        try:
            return await store.get_source(source_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.delete("/sources/{source_id}")
    async def delete_source(
        source_id: Annotated[str, Path()],
        request: Request,
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSource:
        try:
            source = await store.request_source_deletion(source_id)
            retention_ms = int(request.app.state.web.wiki_source_retention_ms)
            await store.purge_due_source_files(retention_ms=retention_ms, limit=1)
            return source
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post("/sources/{source_id}/parse", status_code=status.HTTP_202_ACCEPTED)
    async def enqueue_parse(
        source_id: Annotated[str, Path()],
        worker: Annotated[WikiIngestionWorkerManager, Depends(_get_worker)],
        body: WikiParseRequest | None = None,
    ) -> WikiParseQueuedResponse:
        try:
            queued = await worker.enqueue(
                source_id,
                requested_mode=(None if body is None else body.parse_mode),
            )
            return WikiParseQueuedResponse(source_id=source_id, queued=queued)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}/artifacts")
    async def list_artifacts(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
        parse_revision_id: Annotated[str | None, Query()] = None,
    ) -> list[WikiArtifact]:
        try:
            return list(
                await store.list_artifacts(
                    source_id,
                    parse_revision_id=parse_revision_id,
                )
            )
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}/parse-revisions")
    async def list_parse_revisions(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiParseRevision]:
        try:
            return list(await store.list_parse_revisions(source_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/sources/{source_id}/summaries",
        status_code=status.HTTP_201_CREATED,
    )
    async def summarize_source(
        source_id: Annotated[str, Path()],
        service: Annotated[WikiSummaryService, Depends(_get_summary_service)],
    ) -> WikiSourceSummaryDraft:
        try:
            return await service.summarize_source(source_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}/summaries")
    async def list_source_summaries(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiSourceSummaryDraft]:
        try:
            return list(await store.list_source_summaries(source_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/summaries/{summary_id}")
    async def get_source_summary(
        summary_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiSourceSummaryDraft:
        try:
            return await store.get_source_summary(summary_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/summaries/{summary_id}/entry-page-proposal",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_entry_page_proposal(
        summary_id: Annotated[str, Path()],
        service: Annotated[WikiEntryPageService, Depends(_get_entry_page_service)],
    ) -> WikiPageProposal:
        try:
            return await service.create_entry_page(summary_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/summaries/{summary_id}/topic-page-proposals",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_topic_page_proposals(
        summary_id: Annotated[str, Path()],
        service: Annotated[WikiEntryPageService, Depends(_get_entry_page_service)],
    ) -> list[WikiPageProposal]:
        try:
            return list(await service.create_topic_pages(summary_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}/page-proposals")
    async def list_page_proposals(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiPageProposal]:
        try:
            return list(await store.list_page_proposals(source_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/page-proposals/{proposal_id}")
    async def get_page_proposal(
        proposal_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiPageProposal:
        try:
            return await store.get_page_proposal(proposal_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post(
        "/summaries/{summary_id}/change-set",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_change_set(
        summary_id: Annotated[str, Path()],
        service: Annotated[WikiChangeSetService, Depends(_get_change_set_service)],
    ) -> WikiChangeSetResponse:
        try:
            change_set, items = await service.create_from_summary(summary_id)
            return WikiChangeSetResponse(change_set=change_set, items=items)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/change-sets")
    async def list_change_sets(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiChangeSet]:
        try:
            return list(await store.list_change_sets(space_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/change-sets/{change_set_id}")
    async def get_change_set(
        change_set_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiChangeSet:
        try:
            return await store.get_change_set(change_set_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/change-sets/{change_set_id}/items")
    async def list_change_set_items(
        change_set_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiChangeSetItem]:
        try:
            return list(await store.list_change_set_items(change_set_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.post("/change-sets/{change_set_id}/decision")
    async def decide_change_set(
        change_set_id: Annotated[str, Path()],
        body: WikiChangeSetDecisionRequest,
        service: Annotated[WikiChangeSetService, Depends(_get_change_set_service)],
    ) -> WikiChangeSetDecisionResponse:
        try:
            change_set, pages = await service.decide(
                change_set_id,
                approve=body.decision == "approve",
            )
            return WikiChangeSetDecisionResponse(change_set=change_set, pages=pages)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/pages")
    async def list_pages(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiPage]:
        try:
            return list(await store.list_pages(space_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/search")
    async def search_pages(
        space_id: Annotated[str, Path()],
        query: Annotated[str, Query(alias="q", min_length=1, max_length=200)],
        store: Annotated[WikiStore, Depends(_get_store)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> list[WikiPageSearchResult]:
        try:
            return list(await store.search_pages(space_id, query, limit=limit))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/graph")
    async def get_graph_snapshot(
        space_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiGraphSnapshot:
        try:
            return await store.get_graph_snapshot(space_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/spaces/{space_id}/graph/pages/{page_id}/neighbors")
    async def get_page_graph_neighborhood(
        space_id: Annotated[str, Path()],
        page_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiGraphSnapshot:
        try:
            return await store.get_page_graph_neighborhood(space_id, page_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/pages/{page_id}")
    async def get_page(
        page_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiPage:
        try:
            return await store.get_page(page_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/pages/{page_id}/revisions")
    async def list_page_revisions(
        page_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiPageRevision]:
        try:
            return list(await store.list_page_revisions(page_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/page-revisions/{revision_id}")
    async def get_page_revision(
        revision_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiPageRevision:
        try:
            return await store.get_page_revision(revision_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}/jobs")
    async def list_parse_jobs(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiJob]:
        try:
            return list(await store.list_parse_jobs(source_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/sources/{source_id}/content", response_class=Response)
    async def read_source_content(
        source_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> Response:
        try:
            source = await store.get_source(source_id)
            content = await store.read_source_content(source)
            return _content_response(
                content,
                media_type=source.mime_type,
                filename=source.display_name,
            )
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/artifacts/{artifact_id}/content", response_class=Response)
    async def read_artifact_content(
        artifact_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> Response:
        try:
            artifact = await store.get_artifact(artifact_id)
            source = await store.get_source(artifact.source_id)
            content = await store.read_artifact_content(source, artifact)
            return _content_response(
                content,
                media_type=artifact.mime_type,
                filename=_pure_path_name(artifact.relpath),
            )
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/jobs/{job_id}")
    async def get_job(
        job_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> WikiJob:
        try:
            return await store.get_job(job_id)
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    @router.get("/jobs/{job_id}/attempts")
    async def list_parse_attempts(
        job_id: Annotated[str, Path()],
        store: Annotated[WikiStore, Depends(_get_store)],
    ) -> list[WikiParseAttempt]:
        try:
            return list(await store.list_parse_attempts(job_id))
        except WikiStoreError as exc:
            raise _translate_error(exc) from exc

    return router


def _pure_path_name(relative_path: str) -> str:
    return relative_path.rsplit("/", 1)[-1]


__all__ = ["build_wiki_router"]
