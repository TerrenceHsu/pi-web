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

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from ..credentials_api import (
    SafeValidationErrorResponse,
    require_allowed_origin_dep,
    require_ui_header_dep,
)
from ..local_web_security import WebSecurityConfig
from .models import (
    MAX_LIBRARY_DESCRIPTION_LENGTH,
    MAX_LIBRARY_NAME_LENGTH,
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
    return svc


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
    ) -> None:
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise _safe_error(400, "validation_error", "Invalid library id.") from None
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
    ) -> None:
        if not is_valid_document_id(document_id):
            raise _safe_error(400, "validation_error", "Invalid document id.") from None
        try:
            await service.delete_document(document_id)
        except KnowledgeServiceError as exc:
            raise _translate_service_error(exc) from exc


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


def _library_view_to_response(view) -> LibraryResponse:
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


def _document_to_response(doc) -> DocumentResponse:
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
    "DocumentResponse",
    "ErrorResponse",
    "LibraryCreateRequest",
    "LibraryPatchRequest",
    "LibraryResponse",
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
