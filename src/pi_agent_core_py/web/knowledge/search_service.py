"""Session-scoped Knowledge Search Service (P2-R4-B1).

Per P2-R4-A frozen contract §2.3-2.6 / §7 / §14-20:

- Trusted ``session_id`` → ``get_active_library_ids_for_session()``
  → ``allowed_library_ids``
- Empty binding → short-circuit return ``[]`` BEFORE ChunkStore
- NEVER pass ``library_ids=None`` to ChunkStore from production path
- Reuses R3-B ``ChunkStore.search_chunks_fts()`` (sole retrieval owner)
- Loads ``Document.source_name`` for Evidence ``source_filename``
- Delegates Evidence ID assignment to ``EvidenceRegistry``
"""
from __future__ import annotations

from .chunk_store import ChunkStore
from .evidence import EvidenceRegistry
from .search_models import (
    DEFAULT_TOOL_LIMIT,
    MAX_TOOL_LIMIT,
    KnowledgeSearchError,
    SearchKnowledgeResult,
)
from .store import KnowledgeStore


class SearchKnowledgeService:
    """Session-scoped knowledge retrieval with ACL enforcement.

    Construction::

        knowledge_store = await KnowledgeStore.open(db_path)
        chunk_store = ChunkStore(knowledge_store)
        service = SearchKnowledgeService(
            knowledge_store=knowledge_store,
            chunk_store=chunk_store,
        )

    Usage (each search call resolves ACL fresh)::

        registry = EvidenceRegistry()
        result = await service.search(
            session_id="sess_x",
            query="radiotherapy",
            limit=5,
            registry=registry,
        )

    The Service does NOT hold an EvidenceRegistry — the caller (Tool
    adapter in R4-B2) creates one per Assistant turn and passes it in.
    This prevents cross-request contamination (per directive §15).
    """

    def __init__(
        self,
        *,
        knowledge_store: KnowledgeStore,
        chunk_store: ChunkStore,
    ) -> None:
        self._store = knowledge_store
        self._chunk_store = chunk_store

    async def search(
        self,
        *,
        session_id: str,
        query: str,
        limit: int = DEFAULT_TOOL_LIMIT,
        registry: EvidenceRegistry,
    ) -> SearchKnowledgeResult:
        """Search knowledge scoped to ``session_id``'s bound libraries.

        Flow:
          1. Validate query + limit
          2. Resolve session → active library IDs (fresh each call)
          3. Empty binding → return empty result (ChunkStore NOT called)
          4. ChunkStore.search_chunks_fts(library_ids=allowed_ids)
          5. Load Document.source_name for source_filename
          6. EvidenceRegistry.register() each hit → KnowledgeEvidence
          7. Return SearchKnowledgeResult

        Raises:
            KnowledgeSearchError: invalid query / limit / session.
        """
        # 1. Validate
        if not query or not query.strip():
            raise KnowledgeSearchError(
                "knowledge_search_invalid_query",
                "query must be non-empty",
            )
        if limit < 1 or limit > MAX_TOOL_LIMIT:
            raise KnowledgeSearchError(
                "knowledge_search_invalid_query",
                f"limit must be in [1, {MAX_TOOL_LIMIT}]; got {limit}",
            )

        # 2. Resolve ACL (fresh each call — search-time resolution)
        allowed_ids = await self._store.get_active_library_ids_for_session(
            session_id
        )

        # 3. Empty binding short-circuit (HARD GATE)
        if not allowed_ids:
            return SearchKnowledgeResult(query=query, hits=())

        # 4. Retrieve via R3-B ChunkStore (NEVER pass None)
        hits = await self._chunk_store.search_chunks_fts(
            query,
            library_ids=list(allowed_ids),
            limit=limit,
        )

        if not hits:
            return SearchKnowledgeResult(query=query, hits=())

        # 5. Load Document.source_name for each unique document_id
        source_names = await self._load_source_names(hits)

        # 6. Register evidence (Registry assigns E1/E2/...)
        evidence_list = []
        for hit in hits:
            source_name = source_names.get(hit.document_id, "")
            evidence = registry.register(
                document_id=hit.document_id,
                chunk_id=hit.chunk_id,
                source_filename=source_name,
                heading_path=hit.heading_path,
                page_start=hit.page_start,
                page_end=hit.page_end,
                content=hit.content,
                rank=hit.rank,
            )
            evidence_list.append(evidence)

        return SearchKnowledgeResult(
            query=query,
            hits=tuple(evidence_list),
        )

    async def _load_source_names(
        self, hits: list
    ) -> dict[str, str]:
        """Batch-load ``Document.source_name`` for each hit's document_id.

        Uses the parent KnowledgeStore connection (friend access, same
        pattern as api.py helpers). Returns ``{document_id: source_name}``.
        """
        doc_ids = {h.document_id for h in hits}
        if not doc_ids:
            return {}
        db = self._store._require_db()
        placeholders = ", ".join(["?"] * len(doc_ids))
        async with db.execute(
            f"SELECT id, source_name FROM knowledge_documents "
            f"WHERE id IN ({placeholders})",
            tuple(doc_ids),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["id"]: row["source_name"] for row in rows}
