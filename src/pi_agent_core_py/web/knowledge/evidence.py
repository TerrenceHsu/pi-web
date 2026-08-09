"""Turn-scoped Evidence Registry (P2-R4-B1).

Per P2-R4-A frozen contract §2.8 / §10-14:

- One Assistant turn → one EvidenceRegistry
- Evidence IDs: ``E1``, ``E2``, ... (sequential, turn-unique)
- Duplicate ``chunk_id`` → reuse same evidence_id (first-seen wins)
- NOT persisted to database (pure in-memory, request-scoped)
- Cross-turn reset: next turn starts fresh from E1
- Concurrent request isolation: each request gets its own registry

The Registry is deliberately simple — no DB, no async, no I/O.
"""
from __future__ import annotations

from .search_models import KnowledgeEvidence


class EvidenceRegistry:
    """Turn-scoped registry that assigns stable Evidence IDs.

    Usage::

        registry = EvidenceRegistry()
        evidence = registry.register(
            document_id="doc_x",
            chunk_id="chunk_y",
            source_filename="report.pdf",
            heading_path=("Methods", "Analysis"),
            page_start=12, page_end=13,
            content="...", rank=-1.2,
        )
        assert evidence.evidence_id == "E1"

        # Same chunk_id again → returns same evidence_id "E1"
        again = registry.register(...)
        assert again.evidence_id == "E1"

    Thread-safety: NOT thread-safe by design — one event loop, one
    request. Concurrent requests MUST use separate Registry instances.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, KnowledgeEvidence] = {}
        self._by_chunk_id: dict[str, str] = {}
        self._counter: int = 0

    def register(
        self,
        *,
        document_id: str,
        chunk_id: str,
        source_filename: str,
        heading_path: tuple[str, ...],
        page_start: int,
        page_end: int,
        content: str,
        rank: float,
    ) -> KnowledgeEvidence:
        """Register a hit and return ``KnowledgeEvidence``.

        If ``chunk_id`` was already registered in this turn, the
        original ``KnowledgeEvidence`` is returned (first-seen snapshot
        wins — rank from a later search is ignored to preserve citation
        identity stability).
        """
        existing_eid = self._by_chunk_id.get(chunk_id)
        if existing_eid is not None:
            return self._by_id[existing_eid]

        self._counter += 1
        eid = f"E{self._counter}"
        evidence = KnowledgeEvidence(
            evidence_id=eid,
            document_id=document_id,
            chunk_id=chunk_id,
            source_filename=source_filename,
            heading_path=heading_path,
            page_start=page_start,
            page_end=page_end,
            content=content,
            rank=rank,
        )
        self._by_id[eid] = evidence
        self._by_chunk_id[chunk_id] = eid
        return evidence

    def lookup(self, evidence_id: str) -> KnowledgeEvidence | None:
        """Return the evidence for ``evidence_id`` or ``None`` if unknown."""
        return self._by_id.get(evidence_id)

    @property
    def all_evidence(self) -> tuple[KnowledgeEvidence, ...]:
        """All registered evidence in registration order."""
        return tuple(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)
