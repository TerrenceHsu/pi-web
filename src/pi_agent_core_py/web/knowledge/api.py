"""Knowledge Library REST API (P2-R1 R1-C).

Library CRUD + Session Binding API. Reuses existing web security envelope
(``X-PI-Agent-UI`` header + Origin check) from ``credentials_api`` — no
second middleware stack.

R1 endpoints (per P2-R0 §17 + amendment-1):

Library management::

    GET    /api/knowledge/libraries
    POST   /api/knowledge/libraries
    GET    /api/knowledge/libraries/{library_id}
    PATCH  /api/knowledge/libraries/{library_id}
    DELETE /api/knowledge/libraries/{library_id}

Document metadata (read-only + delete; upload / retry / markdown arrive R2)::

    GET    /api/knowledge/libraries/{library_id}/documents
    GET    /api/knowledge/documents/{document_id}
    DELETE /api/knowledge/documents/{document_id}

Session Library Binding::

    GET    /api/sessions/{session_id}/knowledge-libraries
    PUT    /api/sessions/{session_id}/knowledge-libraries

**NOT in R1**: POST PDF upload, POST retry ingestion, GET markdown
content, GET search — those arrive in R2-R5.

Response contract:
- All responses return relative paths / ids only — never absolute paths.
- Error responses use ``{"code": <safe_code>, "message": <safe_msg>}``
  shape — never raw OSError / traceback / SQLite errors.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Annotated, Any, cast

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Path,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..credentials.api import (
    SafeValidationErrorResponse,
    require_allowed_origin_dep,
    require_ui_header_dep,
)
from ..local_web_security import WebSecurityConfig
from .chunk_store import ChunkSearchHit, ChunkStore
from .ingestion_store import (
    IngestionAlreadyActiveError,
    IngestionStore,
    RetryLimitReachedError,
    RetryNotAllowedError,
)
from .ingestion_worker import IngestionWorkerManager
from .models import (
    MAX_LIBRARY_DESCRIPTION_LENGTH,
    MAX_LIBRARY_NAME_LENGTH,
    Document,
    LibraryView,
    is_valid_document_id,
    is_valid_library_description,
    is_valid_library_id,
    is_valid_library_name,
    validate_library_id_or_raise,
)
from .service import (
    DocumentNotFound,
    DuplicateDocument,
    FileStoreFailure,
    KnowledgeService,
    KnowledgeServiceError,
    LibraryNotFound,
    LibraryNotReady,
    ServiceValidationError,
    SessionNotFound,
)
from .store import KnowledgeStore
from .upload_service import (
    MAX_UPLOAD_BODY_BYTES,
    DuplicateDocumentExistsError,
    InvalidFilenameError,
    InvalidPdfSignatureError,
    InvalidUploadError,
    LibraryNotMutableError,
    SourceFinalizeFailedError,
    UploadResult,
    UploadService,
    UploadServiceError,
    UploadServiceInternalError,
    UploadStagingFailedError,
    UploadTooLargeError,
    WorkerUnavailableError,
)

# ============================================================================
# Pydantic request / response DTOs
# ============================================================================


class LibraryCreateRequest(BaseModel):
    """POST /api/knowledge/libraries body."""

    name: str = Field(
        ..., min_length=1, max_length=MAX_LIBRARY_NAME_LENGTH
    )
    description: str = Field(
        default="", max_length=MAX_LIBRARY_DESCRIPTION_LENGTH
    )


class LibraryPatchRequest(BaseModel):
    """PATCH /api/knowledge/libraries/{library_id} body."""

    name: str | None = Field(default=None, min_length=1,
                              max_length=MAX_LIBRARY_NAME_LENGTH)
    description: str | None = Field(
        default=None, max_length=MAX_LIBRARY_DESCRIPTION_LENGTH
    )


class LibraryResponse(BaseModel):
    id: str
    name: str
    description: str
    status: str
    created_at: int
    updated_at: int
    document_count: int = 0
    binding_count: int = 0


class DocumentResponse(BaseModel):
    id: str
    library_id: str
    source_name: str
    source_sha256: str
    source_relpath: str
    markdown_relpath: str
    mime_type: str
    size_bytes: int
    page_count: int
    status: str
    parser_version: str
    error_code: str
    created_at: int
    updated_at: int


class SessionBindingResponse(BaseModel):
    session_id: str
    library_ids: list[str]


class SessionBindingPutRequest(BaseModel):
    """PUT /api/sessions/{session_id}/knowledge-libraries body.

    Replace-all semantics — server dedupes + validates each id.
    """

    library_ids: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: dict[str, Any]


# ============================================================================
# P2-R5-B2 DTOs (Library-scoped Search REST)
# ============================================================================

#: Default / max result limits for the Web Knowledge Manager search. Per
#: P2-R5-A §5.2 — REST default 10, cap = ``ChunkStore.MAX_FTS_LIMIT`` (50).
#: These differ from the Agent Tool (default 5 / max 10) because the Web
#: UI surfaces more hits than the LLM evidence budget.
DEFAULT_SEARCH_LIMIT: int = 10


class LibrarySearchRequest(BaseModel):
    """POST /api/knowledge/libraries/{library_id}/search body."""

    query: str = Field(..., min_length=1)
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=50)


class LibrarySearchResultItem(BaseModel):
    """Single search hit in the Web response.

    ``source_name`` is enriched from ``Document.source_name`` (never from
    filesystem path). ``rank`` is the FTS5 bm25 score (lower = better;
    same convention as ``ChunkSearchHit.rank``). No ``evidence_id`` —
    Evidence IDs are R4 Agent-only (per R5-A §39).
    """

    document_id: str
    source_name: str
    chunk_id: str
    heading_path: list[str]
    page_start: int
    page_end: int
    content: str
    rank: float


class LibrarySearchResponse(BaseModel):
    """``POST .../search`` response."""

    library_id: str
    query: str
    results: list[LibrarySearchResultItem]


# ============================================================================
# P2-R2-C3 DTOs (Upload / Status / Retry / Markdown)
# ============================================================================


class UploadDocumentPart(BaseModel):
    """Nested ``document`` field of the upload response (per directive §十二)."""

    id: str
    library_id: str
    source_name: str
    source_sha256: str
    size_bytes: int
    status: str
    created_at: int
    updated_at: int


class UploadJobPart(BaseModel):
    """Nested ``job`` field — ``null`` on upload (Job created by worker claim)."""

    id: str | None = None
    document_id: str | None = None
    status: str | None = None  # 'pending' (semantically; no Job row yet)
    attempt: int | None = None


class UploadResponse(BaseModel):
    """``POST /libraries/{lib}/documents/upload`` success response."""

    document: UploadDocumentPart
    job: UploadJobPart | None = None


class JobSummary(BaseModel):
    """Nested ``latest_job`` field of the status response."""

    id: str
    document_id: str
    stage: str
    status: str  # 'running' / 'completed' / 'failed'
    attempt: int
    started_at: int
    finished_at: int | None = None
    safe_error_code: str | None = None  # '' → null on success path


class IngestionStatusResponse(BaseModel):
    """``GET /documents/{doc}/ingestion`` response."""

    document_id: str
    document_status: str
    latest_job: JobSummary | None = None


class RetryResponse(BaseModel):
    """``POST /documents/{doc}/retry`` success response."""

    document_id: str
    job: JobSummary


# ============================================================================
# Service dependency
# ============================================================================


async def get_knowledge_service(request: Request) -> KnowledgeService:
    """Read KnowledgeService from app.state.web (WebAppState).

    Raises 503 if R1 not enabled at app composition time.
    """
    web_state = getattr(request.app.state, "web", None)
    svc = getattr(web_state, "knowledge_service", None) if web_state else None
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "knowledge_service_unavailable",
                "message": "Knowledge subsystem is not initialized.",
            },
        )
    return cast(KnowledgeService, svc)


# ============================================================================
# P2-R2-C3 dependencies (Upload / IngestionStore / WorkerManager)
# ============================================================================


async def get_knowledge_store(request: Request) -> KnowledgeStore:
    """Read KnowledgeStore; raise 503 if subsystem disabled."""
    web_state = getattr(request.app.state, "web", None)
    store = getattr(web_state, "knowledge_store", None) if web_state else None
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "knowledge_service_unavailable",
                "message": "Knowledge subsystem is not initialized.",
            },
        )
    return cast(KnowledgeStore, store)


async def get_ingestion_store(request: Request) -> IngestionStore:
    """Read IngestionStore; raise 503 if subsystem disabled."""
    web_state = getattr(request.app.state, "web", None)
    store = getattr(web_state, "knowledge_store", None) if web_state else None
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "knowledge_service_unavailable",
                "message": "Knowledge subsystem is not initialized.",
            },
        )
    # IngestionStore is a thin wrapper over KnowledgeStore — construct on demand.
    return IngestionStore(store)


async def get_chunk_store(request: Request) -> ChunkStore:
    """Read ChunkStore; raise 503 if subsystem disabled.

    Per P2-R5-A §10 — ChunkStore is sole owner of FTS SQL + BM25 ranking.
    Constructed lazily on each request (same pattern as ``get_ingestion_store``);
    cheap (only holds a back-ref to KnowledgeStore).
    """
    web_state = getattr(request.app.state, "web", None)
    store = getattr(web_state, "knowledge_store", None) if web_state else None
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "knowledge_service_unavailable",
                "message": "Knowledge subsystem is not initialized.",
            },
        )
    return ChunkStore(cast(KnowledgeStore, store))


async def get_worker_manager(request: Request) -> IngestionWorkerManager:
    """Read IngestionWorkerManager; raise 503 if not running."""
    web_state = getattr(request.app.state, "web", None)
    mgr = (
        getattr(web_state, "ingestion_worker_manager", None)
        if web_state
        else None
    )
    if mgr is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "worker_unavailable",
                "message": "Ingestion worker is not initialized.",
            },
        )
    if mgr.state != "running":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "worker_unavailable",
                "message": f"Ingestion worker state is {mgr.state!r}.",
            },
        )
    return cast(IngestionWorkerManager, mgr)


async def get_upload_service(request: Request) -> UploadService:
    """Construct UploadService from app.state.web dependencies.

    Per directive §三十六 — endpoints reuse app state; no module globals.
    """
    web_state = getattr(request.app.state, "web", None)
    if web_state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "knowledge_service_unavailable",
                "message": "Knowledge subsystem is not initialized.",
            },
        )
    store = web_state.knowledge_store
    file_store = web_state.knowledge_file_store
    mgr = web_state.ingestion_worker_manager
    if store is None or file_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "knowledge_service_unavailable",
                "message": "Knowledge subsystem is not initialized.",
            },
        )
    if mgr is None or mgr.state != "running":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "worker_unavailable",
                "message": "Ingestion worker is not running.",
            },
        )
    return UploadService(
        store=store,
        file_store=file_store,
        ingestion_store=IngestionStore(store),
        worker_manager=mgr,
    )


async def _library_has_active_job(
    store: KnowledgeStore, library_id: str
) -> bool:
    """Check if any Document in ``library_id`` has a running Job.

    Per C0 §17.3 — JOIN query via friend access (no new C1 method needed).
    """
    db = store._require_db()
    async with db.execute(
        "SELECT 1 FROM knowledge_ingestion_jobs j "
        "JOIN knowledge_documents d ON j.document_id = d.id "
        "WHERE d.library_id = ? AND j.status = 'running' LIMIT 1",
        (library_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


async def _document_is_indexing(
    store: KnowledgeStore, document_id: str
) -> bool:
    """Check if ``document_id`` is in an active indexing state.

    Per P2-R3-D2 §37 — block delete while status is ``chunking`` or
    ``indexing`` (R3-C runtime is mid-flight). ``normalizing`` is NOT
    blocked (delete-vs-claim race resolves atomically via R3-C
    conditional claim).
    """
    db = store._require_db()
    async with db.execute(
        "SELECT 1 FROM knowledge_documents "
        "WHERE id = ? AND status IN ('chunking', 'indexing') LIMIT 1",
        (document_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


async def _library_has_indexing_doc(
    store: KnowledgeStore, library_id: str
) -> bool:
    """Check if any Document in ``library_id`` is in active indexing state."""
    db = store._require_db()
    async with db.execute(
        "SELECT 1 FROM knowledge_documents "
        "WHERE library_id = ? AND status IN ('chunking', 'indexing') LIMIT 1",
        (library_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


# ============================================================================
# Safe error → HTTPException translation
# ============================================================================


def _safe_error(
    status_code: int,
    code: str,
    message: str,
) -> HTTPException:
    """Construct an HTTPException with safe error envelope.

    ``message`` must NOT contain absolute paths / OSError text / secret.
    Service layer already sanitizes; we only emit the code + brief message.
    """
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _translate_service_error(exc: KnowledgeServiceError) -> HTTPException:
    if isinstance(exc, ServiceValidationError):
        return _safe_error(400, "validation_error", str(exc))
    if isinstance(exc, LibraryNotFound):
        return _safe_error(404, "library_not_found", "Library not found.")
    if isinstance(exc, LibraryNotReady):
        return _safe_error(409, "library_not_ready",
                           "Library is not active; operation rejected.")
    if isinstance(exc, DocumentNotFound):
        return _safe_error(404, "document_not_found", "Document not found.")
    if isinstance(exc, DuplicateDocument):
        return _safe_error(409, "duplicate_document",
                           "Document with same SHA-256 already exists.")
    if isinstance(exc, SessionNotFound):
        return _safe_error(404, "session_not_found", "Session not found.")
    if isinstance(exc, FileStoreFailure):
        return _safe_error(500, "file_store_failure",
                           "File operation failed; retry may be possible.")
    return _safe_error(500, "knowledge_error",
                       f"Unexpected error: {type(exc).__name__}")


# ============================================================================
# P2-R2-C3 error translation (Upload / Retry / Markdown)
# ============================================================================


def _translate_upload_error(exc: UploadServiceError) -> HTTPException:
    """Map :class:`UploadServiceError` subclasses to safe HTTPException."""
    if isinstance(exc, WorkerUnavailableError):
        return _safe_error(503, "worker_unavailable",
                           "Ingestion worker is not running.")
    if isinstance(exc, LibraryNotMutableError):
        # Library not found OR status != active — both 409 with library_not_mutable
        return _safe_error(409, "library_not_mutable",
                           "Library not found or not active; mutation rejected.")
    if isinstance(exc, InvalidFilenameError):
        return _safe_error(400, "invalid_filename",
                           "Filename failed safety validation.")
    if isinstance(exc, InvalidUploadError):
        return _safe_error(400, "invalid_upload", "Malformed upload request.")
    if isinstance(exc, UploadTooLargeError):
        return _safe_error(413, "upload_too_large",
                           "Upload exceeds maximum allowed size.")
    if isinstance(exc, InvalidPdfSignatureError):
        return _safe_error(415, "invalid_pdf_signature",
                           "Upload is not a valid PDF (missing %PDF- magic).")
    if isinstance(exc, DuplicateDocumentExistsError):
        # Per C0 §11.2 — single 409 code with status-specific reason
        reason = _duplicate_reason_code(exc.existing_status)
        return HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_document",
                "reason": reason,
                "existing_document_id": exc.existing_document_id,
                "existing_status": exc.existing_status,
                "message": (
                    f"Document with same SHA-256 already exists "
                    f"(status: {exc.existing_status!r})."
                ),
            },
        )
    if isinstance(exc, UploadStagingFailedError):
        return _safe_error(500, "upload_staging_failed",
                           "Staging write failed; retry may succeed.")
    if isinstance(exc, SourceFinalizeFailedError):
        return _safe_error(500, "source_finalize_failed",
                           "Atomic source finalize failed; retry may succeed.")
    if isinstance(exc, UploadServiceInternalError):
        return _safe_error(500, "internal_knowledge_error",
                           "Internal upload error; safe to retry.")
    return _safe_error(500, "internal_knowledge_error",
                       f"Unexpected upload error: {type(exc).__name__}")


def _duplicate_reason_code(existing_status: str) -> str:
    """Per C0 §11.2 — single 409 + status-specific reason code."""
    if existing_status == "ready":
        return "duplicate_document_ready"
    if existing_status == "needs_ocr":
        return "duplicate_document_needs_ocr"
    if existing_status == "failed":
        return "duplicate_document_failed"
    if existing_status in ("uploaded", "extracting", "normalizing",
                            "chunking", "indexing"):
        return "duplicate_document_in_progress"
    return "duplicate_document"


def _translate_ingestion_store_error(exc: Exception) -> HTTPException:
    """Map IngestionStore errors (retry path) to HTTPException."""
    if isinstance(exc, RetryNotAllowedError):
        # Map per C0 §8.5
        if exc.current_status == "uploaded":
            return _safe_error(409, "retry_not_required",
                               "Document is already pending; retry not needed.")
        return _safe_error(409, "retry_not_allowed",
                           f"Retry not allowed in status {exc.current_status!r}.")
    if isinstance(exc, IngestionAlreadyActiveError):
        return _safe_error(409, "ingestion_already_active",
                           "Document already has a running ingestion job.")
    if isinstance(exc, RetryLimitReachedError):
        return _safe_error(409, "retry_limit_reached",
                           f"Retry limit reached ({exc.attempt_count}/5).")
    return _safe_error(500, "internal_knowledge_error",
                       f"Unexpected retry error: {type(exc).__name__}")


# ============================================================================
# Router factories
# ============================================================================


def build_knowledge_router(
    config: WebSecurityConfig,
    *,
    prefix: str = "",
) -> APIRouter:
    """Build Knowledge router with X-PI-Agent-UI + Origin enforcement."""
    router = APIRouter(
        prefix=prefix,
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
        responses={
            422: {
                "model": SafeValidationErrorResponse,
                "description": "Request validation failed.",
            },
        },
    )
    register_knowledge_endpoints(router)
    return router


def build_session_knowledge_router(
    config: WebSecurityConfig,
    *,
    prefix: str = "",
) -> APIRouter:
    """Build Session-Knowledge binding router.

    Separate router so composition can mount on a different prefix
    (``/api/sessions/``) if desired. Shares the same security envelope.
    """
    router = APIRouter(
        prefix=prefix,
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
        responses={
            422: {
                "model": SafeValidationErrorResponse,
                "description": "Request validation failed.",
            },
        },
    )
    register_session_knowledge_endpoints(router)
    return router


# ============================================================================
# Endpoint registration
# ============================================================================


def register_knowledge_endpoints(router: APIRouter) -> None:
    """Library + Document metadata endpoints (R1 scope)."""


    @router.get("/libraries")
    async def list_libraries(
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> list[LibraryResponse]:
        views = await service.list_libraries()
        return [_library_view_to_response(v) for v in views]

    @router.post("/libraries", status_code=status.HTTP_201_CREATED)
    async def create_library(
        body: LibraryCreateRequest,
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> LibraryResponse:
        if not is_valid_library_name(body.name):
            raise _safe_error(400, "validation_error", "Invalid library name.") from None
        if not is_valid_library_description(body.description):
            raise _safe_error(400, "validation_error",
                              "Invalid library description.") from None
        try:
            lib = await service.create_library(
                name=body.name, description=body.description
            )
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        view = await service.get_library(lib.id)
        return _library_view_to_response(view)

    @router.get("/libraries/{library_id}")
    async def get_library(
        library_id: Annotated[str, Path()],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> LibraryResponse:
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise _safe_error(400, "validation_error", "Invalid library id.") from None
        try:
            view = await service.get_library(library_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        return _library_view_to_response(view)

    @router.patch("/libraries/{library_id}")
    async def update_library(
        library_id: Annotated[str, Path()],
        body: LibraryPatchRequest,
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> LibraryResponse:
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise _safe_error(400, "validation_error", "Invalid library id.") from None
        if body.name is None and body.description is None:
            raise _safe_error(
                400, "validation_error",
                "At least one of name / description required.",
            )
        if body.name is not None and not is_valid_library_name(body.name):
            raise _safe_error(400, "validation_error", "Invalid library name.")
        if body.description is not None and not is_valid_library_description(
            body.description
        ):
            raise _safe_error(400, "validation_error",
                              "Invalid library description.") from None
        try:
            await service.update_library(
                library_id, name=body.name, description=body.description
            )
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        view = await service.get_library(library_id)
        return _library_view_to_response(view)

    @router.delete("/libraries/{library_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_library(
        library_id: Annotated[str, Path()],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
        store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    ) -> None:
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise _safe_error(400, "validation_error", "Invalid library id.") from None
        # C3 active-Job guard: prevent cascade delete while Worker is
        # reading any source.pdf in this Library (per C0 §17.3).
        if await _library_has_active_job(store, library_id):
            raise _safe_error(
                409, "library_ingestion_active",
                "Library has documents with active ingestion jobs; cannot delete.",
            ) from None
        # R3-D2: active-indexing guard — block library delete while any
        # Document in the Library is in ``chunking`` or ``indexing``.
        if await _library_has_indexing_doc(store, library_id):
            raise _safe_error(
                409, "library_indexing_active",
                "Library has documents being indexed; cannot delete.",
            ) from None
        try:
            await service.delete_library(library_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc

    @router.get("/libraries/{library_id}/documents")
    async def list_documents(
        library_id: Annotated[str, Path()],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> list[DocumentResponse]:
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise _safe_error(400, "validation_error", "Invalid library id.") from None
        try:
            # Ensure library exists (404 if not)
            await service.get_library(library_id)
            docs = await service.list_documents(library_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        return [_document_to_response(d) for d in docs]

    @router.get("/documents/{document_id}")
    async def get_document(
        document_id: Annotated[str, Path()],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> DocumentResponse:
        if not is_valid_document_id(document_id):
            raise _safe_error(400, "validation_error", "Invalid document id.") from None
        try:
            doc = await service.get_document(document_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        return _document_to_response(doc)

    @router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_document(
        document_id: Annotated[str, Path()],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
        store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
        ingestion_store: Annotated[IngestionStore, Depends(get_ingestion_store)],
    ) -> None:
        if not is_valid_document_id(document_id):
            raise _safe_error(400, "validation_error", "Invalid document id.") from None
        # C3 active-Job guard: prevent delete while Worker is reading source.pdf
        if await ingestion_store.has_active_job(document_id):
            raise _safe_error(
                409, "document_ingestion_active",
                "Document has an active ingestion job; cannot delete.",
            ) from None
        # R3-D2: active-indexing guard — block delete while Document is
        # in ``chunking`` or ``indexing`` (R3-C runtime mid-flight).
        # ``normalizing`` is allowed (delete-vs-claim race resolves
        # atomically via R3-C conditional claim).
        if await _document_is_indexing(store, document_id):
            raise _safe_error(
                409, "document_indexing_active",
                "Document is being indexed; cannot delete.",
            ) from None
        try:
            await service.delete_document(document_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc

    # ========================================================================
    # P2-R2-C3: Upload / Status / Retry / Markdown endpoints
    # ========================================================================

    @router.post(
        "/libraries/{library_id}/documents/upload",
        status_code=status.HTTP_201_CREATED,
        response_model=UploadResponse,
    )
    async def upload_pdf(
        library_id: Annotated[str, Path()],
        file: Annotated[UploadFile, File(description="PDF file part")],
        upload_service: Annotated[UploadService, Depends(get_upload_service)],
        request: Request,
    ) -> UploadResponse:
        """Upload a single PDF to a Library; creates Document + queues ingestion.

        Multipart form-data with a single ``file`` part. Streams to staging,
        computes SHA-256, validates %PDF- magic, dedup-checks, persists
        source.pdf atomically, notifies Worker. Returns immediately with
        ``document.status='uploaded'``; ingestion runs asynchronously.

        Per directive §三十二 — does NOT call Parser / Builder / Orchestrator;
        does NOT wait for ingestion completion.
        """
        # Content-Length pre-check hint (authoritative enforcement via streaming)
        content_length_hint = None
        cl_header = request.headers.get("content-length")
        if cl_header is not None:
            try:
                content_length_hint = int(cl_header)
            except ValueError:
                content_length_hint = None
            else:
                if content_length_hint > MAX_UPLOAD_BODY_BYTES:
                    raise _safe_error(
                        413, "upload_too_large",
                        f"Content-Length {content_length_hint} exceeds body limit.",
                    ) from None

        # Run the streaming upload pipeline
        try:
            result: UploadResult = await upload_service.upload_stream(
                library_id=library_id,
                source_name=file.filename or "",
                chunk_source=file,
                content_length_hint=content_length_hint,
            )
        except UploadServiceError as exc:
            raise _translate_upload_error(exc) from exc
        except Exception as exc:
            # Last-resort: catch any unclassified exception
            raise _safe_error(
                500, "internal_knowledge_error",
                f"Upload failed unexpectedly: {type(exc).__name__}",
            ) from None

        # Re-read Document to populate created_at / updated_at
        try:
            store = upload_service._store
            doc = await store.get_document(result.document_id)
        except Exception:
            # Don't fail upload on read-back; return the safe fields we have
            return UploadResponse(
                document=UploadDocumentPart(
                    id=result.document_id,
                    library_id=result.library_id,
                    source_name=result.source_name,
                    source_sha256=result.source_sha256,
                    size_bytes=result.size_bytes,
                    status=result.document_status,
                    created_at=0,
                    updated_at=0,
                ),
                job=None,
            )

        return UploadResponse(
            document=UploadDocumentPart(
                id=doc.id,
                library_id=doc.library_id,
                source_name=doc.source_name,
                source_sha256=doc.source_sha256,
                size_bytes=doc.size_bytes,
                status=doc.status,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
            ),
            # Per C0 §21.2 — Job is created by worker claim, not at upload time
            job=None,
        )

    # ========================================================================
    # P2-R2-C3-B: Status / Retry / Markdown endpoints
    # ========================================================================

    @router.get(
        "/documents/{document_id}/ingestion",
        response_model=IngestionStatusResponse,
    )
    async def get_ingestion_status(
        document_id: Annotated[str, Path()],
        store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
        ingestion_store: Annotated[IngestionStore, Depends(get_ingestion_store)],
    ) -> IngestionStatusResponse:
        """Return Document status + latest extract-stage Job.

        Per directive §二十 — only safe fields (no paths / body / traceback).
        ``latest_job`` is null when no Job has been created yet (Document
        in 'uploaded' state, not yet claimed by Worker).
        """
        if not is_valid_document_id(document_id):
            raise _safe_error(400, "validation_error", "Invalid document id.") from None
        try:
            doc = await store.get_document(document_id)
        except Exception as exc:
            # KnowledgeStore raises KnowledgeStoreError; map to 404 if not found
            from .store import DocumentNotFoundError
            if isinstance(exc, DocumentNotFoundError):
                raise _safe_error(404, "document_not_found", "Document not found.") from exc
            raise _safe_error(
                500, "internal_knowledge_error",
                f"document lookup failed: {type(exc).__name__}",
            ) from exc

        latest_job = await ingestion_store.get_latest_extract_job_for_document(document_id)
        job_summary: JobSummary | None = None
        if latest_job is not None:
            job_summary = JobSummary(
                id=latest_job.id,
                document_id=latest_job.document_id,
                stage=latest_job.stage,
                status=latest_job.status,
                attempt=latest_job.attempt,
                started_at=latest_job.started_at,
                finished_at=latest_job.finished_at,
                safe_error_code=(latest_job.safe_error_code or None),
            )
        return IngestionStatusResponse(
            document_id=doc.id,
            document_status=doc.status,
            latest_job=job_summary,
        )

    @router.post(
        "/documents/{document_id}/retry",
        status_code=status.HTTP_201_CREATED,
        response_model=RetryResponse,
    )
    async def retry_ingestion(
        document_id: Annotated[str, Path()],
        store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
        ingestion_store: Annotated[IngestionStore, Depends(get_ingestion_store)],
        worker_manager: Annotated[IngestionWorkerManager, Depends(get_worker_manager)],
    ) -> RetryResponse:
        """Create a new ingestion Job for a failed Document.

        Per C0 §12 + §13.1:
        - Only ``failed`` Documents can be retried (other states → 409)
        - Atomic active Job uniqueness check via BEGIN IMMEDIATE
        - Creates new Job (attempt +1); old Job immutable history
        - Reuses source.pdf + SHA + Document identity
        - Notifies Worker (queue full is OK; polling fallback)
        """
        if not is_valid_document_id(document_id):
            raise _safe_error(400, "validation_error", "Invalid document id.") from None

        # Atomic retry Job creation (handles active Job + retry limit + state machine)
        try:
            new_job = await ingestion_store.create_retry_job(document_id)
        except RetryNotAllowedError as exc:
            raise _translate_ingestion_store_error(exc) from exc
        except IngestionAlreadyActiveError as exc:
            raise _translate_ingestion_store_error(exc) from exc
        except RetryLimitReachedError as exc:
            raise _translate_ingestion_store_error(exc) from exc
        except Exception as exc:
            # Document not found OR Store error
            from .store import DocumentNotFoundError
            if isinstance(exc, DocumentNotFoundError):
                raise _safe_error(404, "document_not_found", "Document not found.") from exc
            raise _safe_error(
                500, "internal_knowledge_error",
                f"retry failed: {type(exc).__name__}",
            ) from exc

        # Notify Worker (best-effort; queue full → polling fallback)
        try:
            worker_manager.notify_pending_job()
        except RuntimeError:
            # Manager transitioned out of running during this call.
            # Job is durable; polling will pick it up if/when manager returns.
            pass

        return RetryResponse(
            document_id=document_id,
            job=JobSummary(
                id=new_job.id,
                document_id=new_job.document_id,
                stage=new_job.stage,
                status=new_job.status,
                attempt=new_job.attempt,
                started_at=new_job.started_at,
                finished_at=new_job.finished_at,
                safe_error_code=(new_job.safe_error_code or None),
            ),
        )

    @router.get(
        "/documents/{document_id}/markdown",
        response_class=Response,
    )
    async def get_markdown(
        document_id: Annotated[str, Path()],
        store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
        request: Request,
    ) -> Response:
        """Return raw Canonical Markdown bytes for a Document.

        Per C0 §21.5 (corrected) + §27-§29:
        - Allowed statuses: ``normalizing`` / ``chunking`` / ``indexing`` / ``ready``
          (Markdown readable != searchable; R2-C terminal is ``normalizing``)
        - Content-Type: ``text/markdown; charset=utf-8``
        - Content-Disposition: ``inline; filename="document.md"``
        - No HTML conversion / no remote image loading / no Markdown renderer
        - Page markers preserved byte-identical
        """
        if not is_valid_document_id(document_id):
            raise _safe_error(400, "validation_error", "Invalid document id.") from None

        # Load Document + check status
        try:
            doc = await store.get_document(document_id)
        except Exception as exc:
            from .store import DocumentNotFoundError
            if isinstance(exc, DocumentNotFoundError):
                raise _safe_error(404, "document_not_found", "Document not found.") from exc
            raise _safe_error(
                500, "internal_knowledge_error",
                f"document lookup failed: {type(exc).__name__}",
            ) from exc

        # Per C0 §21.5 corrected — Markdown readable when MD has been built
        _MARKDOWN_READABLE_STATUSES = frozenset(
            {"normalizing", "chunking", "indexing", "ready"}
        )
        if doc.status not in _MARKDOWN_READABLE_STATUSES:
            raise _safe_error(
                409, "markdown_not_available",
                f"Markdown not available in status {doc.status!r}.",
            )

        # Read raw document.md via R1 KnowledgeFileStore (containment + symlink-safe)
        web_state = getattr(request.app.state, "web", None)
        file_store = web_state.knowledge_file_store if web_state else None
        if file_store is None:
            raise _safe_error(
                503, "knowledge_service_unavailable",
                "Knowledge subsystem is not initialized.",
            )

        from .markdown_persistence import (
            CanonicalMarkdownPersistence,
        )
        persistence = CanonicalMarkdownPersistence(file_store)
        try:
            md_text = await asyncio.to_thread(
                persistence.read,
                library_id=doc.library_id,
                document_id=doc.id,
            )
        except Exception as exc:
            # File missing OR path safety OR IO
            from .markdown_persistence import CanonicalMarkdownReadFailed
            if isinstance(exc, CanonicalMarkdownReadFailed):
                raise _safe_error(
                    409, "markdown_file_missing",
                    "document.md not found or read failed.",
                ) from exc
            raise _safe_error(
                500, "internal_knowledge_error",
                f"markdown read failed: {type(exc).__name__}",
            ) from exc

        # Encode UTF-8 + return raw bytes (page markers preserved byte-identical)
        md_bytes = md_text.encode("utf-8")
        return Response(
            content=md_bytes,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'inline; filename="document.md"',
            },
        )

    # ========================================================================
    # P2-R5-B2: Library-scoped Knowledge Search REST
    # ========================================================================

    @router.post(
        "/libraries/{library_id}/search",
        response_model=LibrarySearchResponse,
    )
    async def search_library(
        library_id: Annotated[str, Path()],
        body: LibrarySearchRequest,
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
        chunk_store: Annotated[Any, Depends(get_chunk_store)],
        store: Annotated[KnowledgeStore, Depends(get_knowledge_store)],
    ) -> LibrarySearchResponse:
        """Search one Library's indexed chunks via BM25.

        Per P2-R5-A §5 (frozen contract):

        - Scope: explicit ``library_id`` path param; **never** search-all.
        - Ready-only: ``ChunkStore.search_chunks_fts`` filters
          ``documents.status='ready'`` by construction.
        - Safe FTS: ``ChunkStore.compile_literal_fts_query`` is sole owner
          of FTS compilation; this handler passes raw user ``query`` to
          ChunkStore — no handler-level grammar / mode / column added.
        - No session: REST Search is **not** the R4 Agent Tool. Library
          scope comes from path param; ``SearchKnowledgeService`` (R4
          session ACL) is **not** reused — no fake Session constructed.
        - Response excludes absolute paths, SQL, internal FTS query,
          Session ID, and Evidence ID (Evidence IDs are Agent-only).

        Errors: 400 validation_error / 404 library_not_found / 409
        library_not_ready / 500 internal_knowledge_error / 503
        knowledge_service_unavailable.
        """
        # 1. Validate library_id format
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise _safe_error(400, "validation_error", "Invalid library id.") from None

        # 2. Library must exist (404) and be active (409 library_not_ready)
        try:
            view = await service.get_library(library_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        if view.library.status != "active":
            raise _safe_error(
                409, "library_not_ready",
                "Library is not active; search rejected.",
            ) from None

        # 3. Run search via ChunkStore (sole FTS owner). library_ids is a
        #    single-element list — NEVER None — so search-all is impossible.
        from .chunk_store import (
            ChunkValidationError,
            FTSQueryError,
        )
        try:
            hits = await chunk_store.search_chunks_fts(
                body.query,
                library_ids=[library_id],
                limit=body.limit,
            )
        except FTSQueryError as exc:
            # Whitespace-only / too-long queries fail safe compilation.
            raise _safe_error(400, "validation_error", str(exc)) from exc
        except ChunkValidationError as exc:
            raise _safe_error(400, "validation_error", str(exc)) from exc

        # 4. Enrich hits with Document.source_name (NOT filesystem path).
        source_names = await _load_source_names_for_hits(store, hits)

        # 5. Build response DTO (no path / SQL / session / evidence_id)
        items = [
            LibrarySearchResultItem(
                document_id=h.document_id,
                source_name=source_names.get(h.document_id, ""),
                chunk_id=h.chunk_id,
                heading_path=list(h.heading_path),
                page_start=h.page_start,
                page_end=h.page_end,
                content=h.content,
                rank=h.rank,
            )
            for h in hits
        ]
        return LibrarySearchResponse(
            library_id=library_id,
            query=body.query,
            results=items,
        )


def register_session_knowledge_endpoints(router: APIRouter) -> None:
    """Session ↔ Library binding endpoints."""


    @router.get("/{session_id}/knowledge-libraries")
    async def get_session_bindings(
        session_id: Annotated[str, Path()],
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> SessionBindingResponse:
        try:
            bindings = await service.list_session_bindings(session_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        return SessionBindingResponse(
            session_id=session_id,
            library_ids=[b.library_id for b in bindings],
        )

    @router.put("/{session_id}/knowledge-libraries")
    async def replace_session_bindings(
        session_id: Annotated[str, Path()],
        body: SessionBindingPutRequest,
        service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
    ) -> SessionBindingResponse:
        # Validate library_id formats up-front
        for lib_id in body.library_ids:
            if not is_valid_library_id(lib_id):
                raise _safe_error(
                    400, "validation_error",
                    f"Invalid library_id format: {lib_id!r}",
                ) from None
        try:
            bindings = await service.replace_session_bindings(
                session_id, body.library_ids
            )
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc
        return SessionBindingResponse(
            session_id=session_id,
            library_ids=[b.library_id for b in bindings],
        )


# ============================================================================
# DTO assembly helpers
# ============================================================================


async def _load_source_names_for_hits(
    store: KnowledgeStore, hits: list[ChunkSearchHit]
) -> dict[str, str]:
    """Batch-load ``Document.source_name`` for each hit's document_id.

    Used by ``search_library`` (R5-B2) — mirrors the friend-access pattern
    in ``SearchKnowledgeService._load_source_names`` (R4-B1). Returns
    ``{document_id: source_name}``; missing documents map to "".
    """
    doc_ids = {h.document_id for h in hits}
    if not doc_ids:
        return {}
    db = store._require_db()
    placeholders = ", ".join(["?"] * len(doc_ids))
    async with db.execute(
        f"SELECT id, source_name FROM knowledge_documents "
        f"WHERE id IN ({placeholders})",
        tuple(doc_ids),
    ) as cursor:
        rows = await cursor.fetchall()
    return {row["id"]: row["source_name"] for row in rows}


def _library_view_to_response(view: LibraryView) -> LibraryResponse:
    """Convert service-layer LibraryView → API LibraryResponse.

    Never includes absolute path / parser internals.
    """
    lib = view.library
    return LibraryResponse(
        id=lib.id,
        name=lib.name,
        description=lib.description,
        status=lib.status,
        created_at=lib.created_at,
        updated_at=lib.updated_at,
        document_count=view.stats.document_count,
        binding_count=view.stats.binding_count,
    )


def _document_to_response(doc: Document) -> DocumentResponse:
    """Convert service-layer Document → API DocumentResponse.

    Rel paths only (P2-R0 §3.2 API path contract).
    """
    return DocumentResponse(
        id=doc.id,
        library_id=doc.library_id,
        source_name=doc.source_name,
        source_sha256=doc.source_sha256,
        source_relpath=doc.source_relpath,
        markdown_relpath=doc.markdown_relpath,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        page_count=doc.page_count,
        status=doc.status,
        parser_version=doc.parser_version,
        error_code=doc.error_code,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "DocumentResponse",
    "ErrorResponse",
    "LibraryCreateRequest",
    "LibraryPatchRequest",
    "LibraryResponse",
    "LibrarySearchRequest",
    "LibrarySearchResponse",
    "LibrarySearchResultItem",
    "SessionBindingPutRequest",
    "SessionBindingResponse",
    "build_knowledge_router",
    "build_session_knowledge_router",
    "get_knowledge_service",
    "register_knowledge_endpoints",
    "register_session_knowledge_endpoints",
]


# Suppress unused-symbol lint for type re-exports.
_: tuple[Any, ...] = (asdict,)
