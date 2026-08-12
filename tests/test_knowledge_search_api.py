"""P2-R5-B2 / B3 — Library-scoped Knowledge Search REST API tests.

Per P2-R5-A §5 (frozen contract) + §19.1 test plan.

ID format: `lib_<body>` / `doc_<body>` where body is 12-32 chars from
``[a-z0-9]`` (P2-R0 §3.4 path safety; enforced by ``is_valid_library_id``
/ ``is_valid_document_id``). The helpers ``_L`` / ``_D`` pad a short tag
to the required length.

Coverage:
- valid library search
- empty result
- ready-only filter (failed / needs_ocr / indexing / normalizing excluded)
- cross-library isolation (A query does not return B chunks)
- deleted library → 404
- deleted document absent from results
- limit default / clamp / out-of-range
- blank query → 400
- query > 512 chars → 400
- literal FTS safety (operators / quotes / asterisks treated as literals)
- Chinese + Unicode filename
- page metadata + heading_path correctness
- library status != active → 409 library_not_ready
- no absolute path / SQL / session_id leak in response
- POST body validation (missing query / wrong types)
- response DTO shape (no extra fields)
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.knowledge.chunker import (
    KnowledgeChunk,
    compute_chunk_id,
    compute_content_sha256,
)
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# ID helpers — produce `lib_<body>` / `doc_<body>` with 12-32 alphanumeric body
# ============================================================================


def _L(tag: str) -> str:
    """Build a valid library_id `lib_<tag>00...` (padded to ≥12-char body)."""
    body = tag.lower()
    # Pad to exactly 14 chars (well above 12-char minimum) with zeros
    if len(body) < 14:
        body = body + "0" * (14 - len(body))
    assert all(c.isalnum() for c in body), body
    assert 12 <= len(body) <= 32, len(body)
    return f"lib_{body}"


def _D(tag: str) -> str:
    """Build a valid document_id `doc_<tag>00...` (padded to ≥12-char body)."""
    body = tag.lower()
    if len(body) < 14:
        body = body + "0" * (14 - len(body))
    assert all(c.isalnum() for c in body), body
    assert 12 <= len(body) <= 32, len(body)
    return f"doc_{body}"


# ============================================================================
# Fixtures
# ============================================================================


def _build_app(tmp_path: Path, *, enable_api: bool = True) -> FastAPI:
    deltas = ["hi"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))
    fake = FakeClient(scripts=[list(script) for _ in range(50)])
    from pi_agent_core_py.agent import Agent

    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    kwargs: dict[str, Any] = dict(
        harness=harness,
        db_path=str(tmp_path / "sessions.db"),
        uploads_dir=str(tmp_path / "uploads"),
        enable_trusted_host=True,
        credential_extra_hosts=("testserver",),
        credential_extra_ui_origins=(),
        knowledge_root=str(tmp_path / "knowledge"),
    )
    if not enable_api:
        kwargs["enable_knowledge_api"] = False
    return create_app(**kwargs)


def _headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


def _chunk(
    *,
    document_id: str,
    ordinal: int,
    content: str,
    heading_path: tuple[str, ...] = (),
    page_start: int = 1,
    page_end: int = 1,
) -> KnowledgeChunk:
    sha = compute_content_sha256(content)
    cid = compute_chunk_id(document_id, ordinal, sha)
    return KnowledgeChunk(
        id=cid,
        document_id=document_id,
        ordinal=ordinal,
        heading_path=heading_path,
        content=content,
        page_start=page_start,
        page_end=page_end,
        char_count=len(content),
        content_sha256=sha,
    )


async def _seed_doc_with_chunks(
    store: KnowledgeStore,
    *,
    library_id: str,
    document_id: str,
    source_name: str,
    status: str,
    chunks: list[KnowledgeChunk],
    library_status: str = "active",
) -> None:
    """Direct-SQL seed: Library + Document with given status + chunks.

    Bypasses the R2/R3 runtime (parser / orchestrator / worker) — we are
    testing the **REST Search surface**, not the ingestion pipeline. The
    R3-B2 test suite already covers replace_document_chunks correctness.
    """
    from pi_agent_core_py.web.knowledge.chunk_store import ChunkStore

    now = int(time.time())
    async with store._write_lock:
        await store._db.execute(
            "INSERT OR IGNORE INTO knowledge_libraries "
            "(id, name, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (library_id, library_id, "", library_status, now, now),
        )
        await store._db.execute(
            "INSERT OR REPLACE INTO knowledge_documents "
            "(id, library_id, source_name, source_sha256, source_relpath, "
            " markdown_relpath, mime_type, size_bytes, page_count, status, "
            " parser_version, error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                library_id,
                source_name,
                "a" * 64,
                f"documents/{document_id}/source.pdf",
                f"documents/{document_id}/document.md",
                "application/pdf",
                10,
                max((c.page_end for c in chunks), default=1),
                "chunking",  # required for replace_document_chunks
                "",
                "",
                now,
                now,
            ),
        )
        await store._db.commit()

    if chunks:
        cs = ChunkStore(store)
        await cs.replace_document_chunks(
            document_id=document_id,
            library_id=library_id,
            chunks=chunks,
        )

    # Now flip to the requested status (e.g. 'ready').
    async with store._write_lock:
        await store._db.execute(
            "UPDATE knowledge_documents SET status=? WHERE id=?",
            (status, document_id),
        )
        await store._db.commit()


@pytest.fixture
def app_client(tmp_path):
    app = _build_app(tmp_path)
    with TestClient(app) as client:
        yield app, client


def _get_knowledge_store(app: FastAPI) -> KnowledgeStore:
    return app.state.web.knowledge_store


# ============================================================================
# 1. Happy path
# ============================================================================


class TestSearchHappyPath:
    def test_basic_search_returns_hit(self, app_client, tmp_path):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("search"), _D("search")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="radiotherapy.pdf",
            status="ready",
            chunks=[
                _chunk(
                    document_id=D,
                    ordinal=0,
                    content="IMRT conformity beta radiation therapy",
                    heading_path=("Radiation Therapy", "IMRT"),
                    page_start=2,
                    page_end=2,
                ),
            ],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "IMRT"},
        )
        assert r.status_code == 200, f"body={r.text}"
        body = r.json()
        assert body["library_id"] == L
        assert body["query"] == "IMRT"
        assert len(body["results"]) == 1
        hit = body["results"][0]
        assert hit["document_id"] == D
        assert hit["source_name"] == "radiotherapy.pdf"
        assert hit["chunk_id"]
        assert hit["heading_path"] == ["Radiation Therapy", "IMRT"]
        assert hit["page_start"] == 2
        assert hit["page_end"] == 2
        assert "IMRT" in hit["content"]
        assert isinstance(hit["rank"], (int, float))

    def test_default_limit_is_10(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("many"), _D("many")

        chunks = [
            _chunk(
                document_id=D,
                ordinal=i,
                content=f"radiotherapy variant {i}",
                heading_path=(),
            )
            for i in range(15)
        ]
        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="many.pdf",
            status="ready",
            chunks=chunks,
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "radiotherapy"},
        )
        assert r.status_code == 200
        assert len(r.json()["results"]) == 10  # default limit


# ============================================================================
# 2. Empty result
# ============================================================================


class TestEmptyResult:
    def test_no_matches_returns_empty_results(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("empty"), _D("empty")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="x.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="radiotherapy planning")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "nonexistenttermxyz"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["results"] == []
        assert body["library_id"] == L


# ============================================================================
# 3. Ready-only filter
# ============================================================================


class TestReadyOnly:
    @pytest.mark.parametrize(
        "status",
        ["uploaded", "extracting", "normalizing", "chunking", "indexing", "failed", "needs_ocr"],
    )
    def test_non_ready_doc_excluded(self, app_client, status):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("ready"), _D("ready")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="r.pdf",
            status=status,
            chunks=[_chunk(
                document_id=D,
                ordinal=0,
                content="radiotherapy ready only filter test",
            )],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "radiotherapy"},
        )
        assert r.status_code == 200
        assert r.json()["results"] == [], (
            f"status={status!r} doc must NOT appear in search"
        )


# ============================================================================
# 4. Cross-library isolation
# ============================================================================


class TestCrossLibraryIsolation:
    def test_library_a_query_does_not_return_library_b_chunks(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        LA, DA = _L("isoaaa"), _D("isoaaa")
        LB, DB = _L("isobbb"), _D("isobbb")

        async def seed_both():
            await _seed_doc_with_chunks(
                store,
                library_id=LA,
                document_id=DA,
                source_name="a.pdf",
                status="ready",
                chunks=[_chunk(document_id=DA, ordinal=0, content="apple")],
            )
            await _seed_doc_with_chunks(
                store,
                library_id=LB,
                document_id=DB,
                source_name="b.pdf",
                status="ready",
                chunks=[_chunk(document_id=DB, ordinal=0, content="secret banana")],
            )

        asyncio.run(seed_both())

        # Library A: query "banana" must return empty
        r = client.post(
            f"/api/knowledge/libraries/{LA}/search",
            headers=_headers(),
            json={"query": "banana"},
        )
        assert r.status_code == 200
        assert r.json()["results"] == []

        # Library B: query "banana" returns the hit
        r = client.post(
            f"/api/knowledge/libraries/{LB}/search",
            headers=_headers(),
            json={"query": "banana"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["document_id"] == DB


# ============================================================================
# 5. Library existence + status
# ============================================================================


class TestLibraryStateGuards:
    def test_missing_library_returns_404(self, app_client):
        _app, client = app_client
        L = _L("missing")
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "anything"},
        )
        assert r.status_code == 404
        assert "library_not_found" in str(r.json())

    def test_invalid_library_id_format_returns_400_or_422(self, app_client):
        _app, client = app_client
        r = client.post(
            "/api/knowledge/libraries/NOT-VALID/search",
            headers=_headers(),
            json={"query": "anything"},
        )
        assert r.status_code in (400, 422)

    def test_archived_library_returns_409(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("archaaa"), _D("archaaa")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="a.pdf",
            status="ready",
            library_status="archived",
            chunks=[_chunk(document_id=D, ordinal=0, content="archived content")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "archived"},
        )
        assert r.status_code == 409
        assert "library_not_ready" in str(r.json())


# ============================================================================
# 6. Validation: query + limit
# ============================================================================


class TestValidation:
    def test_blank_query_returns_400(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("valid"), _D("valid")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="v.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="some content")],
        ))

        # Whitespace-only passes Pydantic min_length=1 but fails ChunkStore strip
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "   "},
        )
        assert r.status_code == 400
        assert "validation_error" in str(r.json())

    def test_missing_query_returns_422(self, app_client):
        _app, client = app_client
        L = _L("any")
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"limit": 5},  # missing query
        )
        assert r.status_code == 422

    def test_query_too_long_returns_400(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("long"), _D("long")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="l.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="x")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "x" * 513},
        )
        assert r.status_code == 400

    def test_limit_zero_returns_422(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("zero"), _D("zero")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="l.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="content")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "content", "limit": 0},
        )
        assert r.status_code == 422  # Pydantic ge=1

    def test_limit_above_max_returns_422(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("max"), _D("max")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="m.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="content")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "content", "limit": 51},  # MAX_FTS_LIMIT=50
        )
        assert r.status_code == 422  # Pydantic le=50

    def test_limit_50_accepted(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("fifty"), _D("fifty")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="fifty.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="fifty limit max boundary")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "fifty", "limit": 50},
        )
        assert r.status_code == 200


# ============================================================================
# 7. Safe FTS — literal-only
# ============================================================================


class TestSafeFTSLiteral:
    @pytest.mark.parametrize(
        "raw_query",
        [
            "a OR b",
            "a AND b",
            "a NEAR b",
            "title:bar",
            "a*",
            "(a)",
            '"unclosed',
            'foo"bar',
        ],
    )
    def test_fts_operators_treated_as_literals(self, app_client, raw_query):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("lit"), _D("lit")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="lit.pdf",
            status="ready",
            chunks=[
                _chunk(
                    document_id=D,
                    ordinal=0,
                    content="contains OR AND NEAR title asterisk parens",
                )
            ],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": raw_query},
        )
        assert r.status_code == 200, f"raw_query={raw_query!r} body={r.text}"


# ============================================================================
# 8. Unicode (Chinese + Unicode filename)
# ============================================================================


class TestUnicode:
    def test_chinese_query_and_unicode_filename(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("uni"), _D("uni")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="中文文件.pdf",
            status="ready",
            chunks=[
                _chunk(
                    document_id=D,
                    ordinal=0,
                    content="放射治疗 调强 放疗",
                    heading_path=("放射科", "调强放疗"),
                    page_start=1,
                    page_end=1,
                )
            ],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "调强"},
        )
        assert r.status_code == 200, f"body={r.text}"
        body = r.json()
        assert len(body["results"]) == 1
        hit = body["results"][0]
        assert hit["source_name"] == "中文文件.pdf"
        assert hit["heading_path"] == ["放射科", "调强放疗"]


# ============================================================================
# 9. Page + heading metadata correctness
# ============================================================================


class TestMetadataCorrectness:
    def test_page_range_and_heading_path_preserved(self, app_client):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("metadata"), _D("metadata")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="multi.pdf",
            status="ready",
            chunks=[
                _chunk(
                    document_id=D,
                    ordinal=0,
                    content="first section alpha",
                    heading_path=("Chapter 1", "Intro"),
                    page_start=1,
                    page_end=2,
                ),
                _chunk(
                    document_id=D,
                    ordinal=1,
                    content="second section beta",
                    heading_path=("Chapter 2", "Methods"),
                    page_start=3,
                    page_end=4,
                ),
            ],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "beta"},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["results"]) == 1
        hit = body["results"][0]
        assert hit["page_start"] == 3
        assert hit["page_end"] == 4
        assert hit["heading_path"] == ["Chapter 2", "Methods"]


# ============================================================================
# 10. Deleted resources absent
# ============================================================================


class TestDeletedResources:
    def test_deleted_document_absent_from_search(self, app_client):
        """Seed a single ready doc; search finds it; delete it; search
        returns empty. Avoids cross-loop aiosqlite issues by seeding only
        one doc per ``asyncio.run()`` invocation (multiple sequential
        ``asyncio.run()`` calls against the app's loop-bound connection
        can silently drop statements — see P2-R5-B test debugging notes).
        """
        app, client = app_client
        store = _get_knowledge_store(app)
        from pi_agent_core_py.web.knowledge.chunk_store import ChunkStore

        L, D = _L("deleted"), _D("deleted")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="keep.pdf",
            status="ready",
            chunks=[_chunk(
                document_id=D,
                ordinal=0,
                content="keep this chunk content radiotherapy",
            )],
        ))

        # Sanity: search finds the doc
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "radiotherapy"},
        )
        assert r.status_code == 200
        assert len(r.json()["results"]) == 1

        # Delete doc + its chunks
        async def delete_doc():
            cs = ChunkStore(store)
            await cs.delete_document_chunks(document_id=D)
            async with store._write_lock:
                await store._db.execute(
                    "DELETE FROM knowledge_documents WHERE id=?",
                    (D,),
                )
                await store._db.commit()

        asyncio.run(delete_doc())

        # Search now returns empty
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "radiotherapy"},
        )
        assert r.status_code == 200
        assert r.json()["results"] == []


# ============================================================================
# 11. No leak — paths / SQL / session_id / evidence_id
# ============================================================================


class TestNoLeak:
    def test_no_absolute_path_or_sql_or_session_or_evidence_in_response(
        self, app_client, tmp_path
    ):
        app, client = app_client
        store = _get_knowledge_store(app)
        L, D = _L("noleak"), _D("noleak")

        asyncio.run(_seed_doc_with_chunks(
            store,
            library_id=L,
            document_id=D,
            source_name="nk.pdf",
            status="ready",
            chunks=[_chunk(document_id=D, ordinal=0, content="no leak test radiotherapy")],
        ))

        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers=_headers(),
            json={"query": "radiotherapy"},
        )
        assert r.status_code == 200
        body_text = r.text

        # No absolute paths from temp dir
        assert str(tmp_path) not in body_text
        # No SQL keywords that would indicate raw SQL leak
        for forbidden in [
            "SELECT ",
            "FROM knowledge_chunks",
            "MATCH ",
            "bm25(",
        ]:
            assert forbidden not in body_text, f"leaked: {forbidden!r}"
        # No session_id / evidence_id keys
        body = r.json()
        assert "session_id" not in body
        assert "evidence_id" not in body["results"][0]
        # DTO shape: exactly the frozen keys
        expected_keys = {
            "document_id",
            "source_name",
            "chunk_id",
            "heading_path",
            "page_start",
            "page_end",
            "content",
            "rank",
        }
        assert set(body["results"][0].keys()) == expected_keys


# ============================================================================
# 12. Auth — security envelope
# ============================================================================


class TestSecurityEnvelope:
    def test_missing_ui_header_rejected(self, app_client):
        _app, client = app_client
        L = _L("any")
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            json={"query": "x"},  # no UI header
        )
        assert r.status_code == 400
        assert "missing_ui_header" in str(r.json())

    def test_invalid_origin_rejected(self, app_client):
        _app, client = app_client
        L = _L("any")
        r = client.post(
            f"/api/knowledge/libraries/{L}/search",
            headers={**_headers(), "Origin": "https://evil.example"},
            json={"query": "x"},
        )
        assert r.status_code == 403


# ============================================================================
# 13. Knowledge API disabled → 404 (router not mounted)
# ============================================================================


class TestApiDisabled:
    def test_search_returns_404_when_api_disabled(self, tmp_path):
        """When knowledge_root set but enable_knowledge_api=False, router
        is not mounted → all /api/knowledge/* paths 404 (composition-time
        decision, not a 503 runtime decision)."""
        app = _build_app(tmp_path, enable_api=False)
        L = _L("any")
        with TestClient(app) as client:
            r = client.post(
                f"/api/knowledge/libraries/{L}/search",
                headers=_headers(),
                json={"query": "x"},
            )
            assert r.status_code == 404
