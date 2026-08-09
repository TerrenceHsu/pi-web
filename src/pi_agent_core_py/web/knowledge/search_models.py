"""Knowledge search DTOs (P2-R4-B1).

Per P2-R4-A frozen contract §2.7-2.8 / §8:

- ``KnowledgeEvidence`` is the Agent-facing evidence DTO. It wraps R3-B's
  ``ChunkSearchHit`` with a turn-scoped ``evidence_id`` assigned by
  ``EvidenceRegistry``.
- ``SearchKnowledgeResult`` is the Service return type.

These DTOs are **never** persisted to the database (turn-scoped only).
"""
from __future__ import annotations

from dataclasses import dataclass

# ============================================================================
# Constants — frozen per R4-A §2.2
# ============================================================================

DEFAULT_TOOL_LIMIT: int = 5
MAX_TOOL_LIMIT: int = 10

# ============================================================================
# Error
# ============================================================================


class KnowledgeSearchError(Exception):
    """Controlled search error with stable ``code`` for Agent-safe messaging.

    ``code`` is one of the R4-A §2.12 frozen codes:
    ``knowledge_search_invalid_query`` /
    ``knowledge_search_session_missing`` /
    ``knowledge_search_unavailable`` /
    ``knowledge_search_failed``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ============================================================================
# DTOs
# ============================================================================


@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    """Single piece of retrieved knowledge, Agent-facing.

    ``evidence_id`` is assigned by ``EvidenceRegistry`` (turn-scoped
    ``E1`` / ``E2`` / ...). ``source_filename`` comes from
    ``Document.source_name`` (NOT from filesystem path). ``page_start``
    / ``page_end`` come from R3-A chunk metadata.
    """

    evidence_id: str
    document_id: str
    chunk_id: str
    source_filename: str
    heading_path: tuple[str, ...]
    page_start: int
    page_end: int
    content: str
    rank: float


@dataclass(frozen=True, slots=True)
class SearchKnowledgeResult:
    """Result of ``SearchKnowledgeService.search()``."""

    query: str
    hits: tuple[KnowledgeEvidence, ...]
