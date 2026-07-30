"""Knowledge Library domain models (P2-R1).

Pure dataclasses + enums only — **no** SQLite / FastAPI / filesystem deps.
Schema mirrors the frozen DDL in ``docs/design/p2-r0-rag-contract.md §2``
(amendment-1 preserves the DDL; only R1/R2 scope ownership changed).

Status enumerations (per contract §2.2 + §2.4):

- ``LibraryStatus`` — ``active`` / ``archived`` / ``deleting`` / ``failed``
- ``DocumentStatus`` — full state-machine values from contract §2.4
- ``IngestionStage`` — ``extract`` / ``normalize`` / ``chunk`` / ``index``
- ``IngestionJobStatus`` — ``running`` / ``completed`` / ``failed``
- ``SessionLibraryAccessMode`` — first version is fixed to ``read``

These types are the **only** shape API responses may take; nothing in
``models`` may leak absolute paths, raw exceptions, or SQLite rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ============================================================================
# Status enums (Literal types — match contract §2.2 / §2.4 exactly)
# ============================================================================

LibraryStatus = Literal["active", "archived", "deleting", "failed"]
_VALID_LIBRARY_STATUSES: frozenset[str] = frozenset(
    {"active", "archived", "deleting", "failed"}
)

DocumentStatus = Literal[
    "uploaded",
    "extracting",
    "normalizing",
    "chunking",
    "indexing",
    "ready",
    "failed",
    "needs_ocr",
    "deleting",
]
_VALID_DOCUMENT_STATUSES: frozenset[str] = frozenset(
    {
        "uploaded",
        "extracting",
        "normalizing",
        "chunking",
        "indexing",
        "ready",
        "failed",
        "needs_ocr",
        "deleting",
    }
)

IngestionStage = Literal["extract", "normalize", "chunk", "index"]
_VALID_INGESTION_STAGES: frozenset[str] = frozenset(
    {"extract", "normalize", "chunk", "index"}
)

IngestionJobStatus = Literal["running", "completed", "failed"]
_VALID_INGESTION_JOB_STATUSES: frozenset[str] = frozenset(
    {"running", "completed", "failed"}
)

SessionLibraryAccessMode = Literal["read"]
_VALID_ACCESS_MODES: frozenset[str] = frozenset({"read"})


# ============================================================================
# Validation helpers
# ============================================================================

#: Max Library name length (P2-R0 contract §9.1 — name 长度上限)
MAX_LIBRARY_NAME_LENGTH: int = 200

#: Max Library description length
MAX_LIBRARY_DESCRIPTION_LENGTH: int = 2000

#: Max Document source_name length (sanitized filename)
MAX_SOURCE_NAME_LENGTH: int = 255

#: ID format — must match `^(lib|doc|chunk|job)_<base32>[a-z0-9]{12,32}$`
#: (P2-R0 contract §3.4 path safety)
_ID_PATTERN_PREFIXES: tuple[str, ...] = ("lib_", "doc_", "chunk_", "job_")


def is_valid_library_id(value: str) -> bool:
    """Library id must be backend-generated ``lib_<body>`` form.

    Body chars: ``[a-z0-9]``; length 12-32 (excluding prefix).
    """
    return _is_valid_id(value, "lib_")


def is_valid_document_id(value: str) -> bool:
    """Document id must be backend-generated ``doc_<body>`` form."""
    return _is_valid_id(value, "doc_")


def is_valid_chunk_id(value: str) -> bool:
    """Chunk id must be backend-generated ``chunk_<body>`` form."""
    return _is_valid_id(value, "chunk_")


def is_valid_job_id(value: str) -> bool:
    """Ingestion Job id must be backend-generated ``job_<body>`` form."""
    return _is_valid_id(value, "job_")


def _is_valid_id(value: str, prefix: str) -> bool:
    if not isinstance(value, str):
        return False
    if not value.startswith(prefix):
        return False
    body = value[len(prefix):]
    if not (12 <= len(body) <= 32):
        return False
    return all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in body)


def is_valid_library_name(value: str) -> bool:
    """Library name validation (P2-R0 §9.1)."""
    if not isinstance(value, str):
        return False
    if not value.strip():
        return False
    return len(value) <= MAX_LIBRARY_NAME_LENGTH


def is_valid_library_description(value: str) -> bool:
    """Library description validation."""
    if not isinstance(value, str):
        return False
    return len(value) <= MAX_LIBRARY_DESCRIPTION_LENGTH


def is_valid_library_status(value: str) -> bool:
    return value in _VALID_LIBRARY_STATUSES


def is_valid_document_status(value: str) -> bool:
    return value in _VALID_DOCUMENT_STATUSES


def is_valid_ingestion_stage(value: str) -> bool:
    return value in _VALID_INGESTION_STAGES


def is_valid_ingestion_job_status(value: str) -> bool:
    return value in _VALID_INGESTION_JOB_STATUSES


def is_valid_access_mode(value: str) -> bool:
    return value in _VALID_ACCESS_MODES


# ============================================================================
# Document state machine (P2-R0 contract §2.4)
# ============================================================================

#: Allowed transitions — keys are source state, values are allowed target states.
#: Anything not listed is rejected. ``needs_ocr`` is terminal (no auto-retry).
#: ``ready`` is terminal success. ``failed`` is recoverable (retry creates new Job,
#: document itself moves back to ``uploaded`` or directly to ``extracting``).
_DOCUMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"extracting", "deleting"}),
    "extracting": frozenset({"normalizing", "failed", "needs_ocr", "deleting"}),
    "normalizing": frozenset({"chunking", "failed", "deleting"}),
    "chunking": frozenset({"indexing", "failed", "deleting"}),
    "indexing": frozenset({"ready", "failed", "deleting"}),
    "ready": frozenset({"deleting"}),
    "failed": frozenset({"extracting", "deleting"}),
    "needs_ocr": frozenset({"deleting"}),
    "deleting": frozenset(),  # terminal — only cascade delete
}


def is_valid_document_transition(src: str, dst: str) -> bool:
    """Check whether ``src`` → ``dst`` is a permitted transition.

    Per P2-R0 §2.4: ``needs_ocr`` / ``ready`` / ``deleting`` are terminal.
    """
    allowed = _DOCUMENT_TRANSITIONS.get(src)
    if allowed is None:
        return False
    return dst in allowed


# ============================================================================
# Dataclasses — DTOs for Library / Document / Job / Chunk / Binding
# ============================================================================


@dataclass(frozen=True)
class Library:
    """Knowledge Library metadata (no document content / no path)."""

    id: str
    name: str
    description: str
    status: LibraryStatus
    created_at: int  # ms epoch
    updated_at: int  # ms epoch


@dataclass(frozen=True)
class Document:
    """Knowledge Document metadata.

    ``source_relpath`` / ``markdown_relpath`` are **relative** to the library
    root (``documents/{doc_id}/source.pdf`` etc.) — never absolute paths
    (per P2-R0 §3.2 API path contract).
    """

    id: str
    library_id: str
    source_name: str
    source_sha256: str
    source_relpath: str
    markdown_relpath: str
    mime_type: str
    size_bytes: int
    page_count: int
    status: DocumentStatus
    parser_version: str
    error_code: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class IngestionJob:
    """Ingestion pipeline Job audit row.

    One Job per stage attempt; retries create new Jobs (history preserved).
    ``safe_error_code`` must be a stable short string — no path / body / secret.
    """

    id: str
    document_id: str
    stage: IngestionStage
    status: IngestionJobStatus
    attempt: int
    started_at: int
    finished_at: int | None
    safe_error_code: str


@dataclass(frozen=True)
class Chunk:
    """Chunk row.

    Fields per P2-R0 §5.1. R1 stores the row shape (no chunking logic yet —
    that arrives in R2). ``content_hash`` is SHA-256 of ``content``.
    """

    id: str
    library_id: str
    document_id: str
    ordinal: int
    heading_path: str
    page_start: int
    page_end: int
    content: str
    content_hash: str
    token_count: int
    created_at: int = 0


@dataclass(frozen=True)
class SessionLibraryBinding:
    """Session ↔ Library binding row.

    P2-R0 §7 — ``access_mode`` is fixed to ``read`` in R1.
    """

    session_id: str
    library_id: str
    access_mode: SessionLibraryAccessMode
    created_at: int


# ============================================================================
# Internal helpers (used by store / service / api)
# ============================================================================


def validate_library_id_or_raise(value: str) -> str:
    """Return ``value`` if valid; raise ``ValueError`` otherwise.

    Used at module boundary (API request DTOs, store method entry).
    """
    if not is_valid_library_id(value):
        raise ValueError(f"invalid library_id: {value!r}")
    return value


def validate_document_id_or_raise(value: str) -> str:
    if not is_valid_document_id(value):
        raise ValueError(f"invalid document_id: {value!r}")
    return value


def validate_session_id_or_raise(value: str) -> str:
    """Session id format check — must be ``sess_<body>`` or equivalent.

    P2-R0 §3.4 only specifies lib/doc/chunk/job prefixes; sessions are owned
    by ``session_sqlite`` so we accept the broader ``sess_*`` / ``sess-``
    patterns produced by ``SQLiteSessionStore``.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid session_id: {value!r}")
    # SQLiteSessionStore uses `sess-{ts}-{hex}` form — accept any non-empty
    # string that doesn't contain path separators / control chars.
    if any(c in value for c in ("/", "\\", "\x00")):
        raise ValueError(f"invalid session_id: {value!r}")
    return value


@dataclass(frozen=True)
class LibraryUpdate:
    """PATCH payload for Library — only ``name`` / ``description`` mutable.

    ``status`` is **not** settable via PATCH — it transitions only via
    delete / internal recovery (P2-R0 §9.4).
    """

    name: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class LibraryStats:
    """Aggregate stats for a Library (optional, used in list responses)."""

    document_count: int = 0
    binding_count: int = 0


@dataclass(frozen=True)
class LibraryView:
    """Library + aggregate stats for API responses."""

    library: Library
    stats: LibraryStats = field(default_factory=LibraryStats)
