"""Source Summary Draft generation and trust-boundary regressions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.wiki import (
    CoreAgentWikiSummaryAgent,
    WikiChangeSetService,
    WikiEntryPageService,
    WikiIngestionService,
    WikiSourceSummaryContent,
    WikiStore,
    WikiStoreError,
    WikiSummaryAgentResponse,
    WikiSummaryService,
    compile_wiki_fts_query,
)
from pi_agent_core_py.web.wiki.knowledge_agent import (
    KnowledgeAgentBinding,
    build_knowledge_tool_registry,
    knowledge_agent_binding,
)


def _summary_json(*, title: str = "Guide") -> str:
    return json.dumps(
        {
            "suggested_title": title,
            "overview": "A concise, source-grounded guide.",
            "key_points": [
                {"text": "The guide says hello.", "page_numbers": [1]},
            ],
            "topics": [
                {
                    "title": "Greeting",
                    "summary": "The source introduces a greeting.",
                    "page_numbers": [1],
                }
            ],
            "caveats": ["Only one source page was available."],
        },
        ensure_ascii=False,
    )


async def _parsed_html_store(tmp_path: Path) -> tuple[WikiStore, WikiIngestionService, str]:
    space_ids: Iterator[str] = iter(
        (
            "space_000000000000000000000001",
            "space_000000000000000000000002",
        )
    )
    job_ids: Iterator[str] = iter(f"job_{index:024x}" for index in range(1, 9))
    revision_ids: Iterator[str] = iter(
        (
            "parse_revision_000000000000000000000001",
            "parse_revision_000000000000000000000002",
        )
    )
    attempt_ids: Iterator[str] = iter(
        (
            "parse_attempt_000000000000000000000001",
            "parse_attempt_000000000000000000000002",
        )
    )
    artifact_ids: Iterator[str] = iter(f"artifact_{index:024x}" for index in range(1, 7))
    store = await WikiStore.open(
        tmp_path,
        clock_ms=lambda: 100,
        id_factory=lambda: next(space_ids),
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: next(job_ids),
        parse_revision_id_factory=lambda: next(revision_ids),
        parse_attempt_id_factory=lambda: next(attempt_ids),
        artifact_id_factory=lambda: next(artifact_ids),
        summary_id_factory=lambda: "summary_000000000000000000000001",
    )
    space = await store.create_space(name="Summary")
    ingestion = WikiIngestionService(store)
    source = await ingestion.upload_source(
        space.id,
        display_name="guide.html",
        mime_type="text/html",
        content=b"<h1>Guide</h1><p>Hello. RawOnlySecret.</p>",
    )
    await ingestion.parse_source(source.id)
    return store, ingestion, source.id


async def test_agent_summary_is_immutable_and_does_not_write_raw_or_pages(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    service = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    try:
        source = await store.get_source(source_id)
        raw_before = await store.read_source_content(source)
        summary = await service.summarize_source(source_id)

        assert summary.content.suggested_title == "Guide"
        assert summary.selection_version == source.selection_version == 1
        assert summary.parse_revision_id == source.selected_parse_revision_id
        assert summary.provider == "fake"
        assert summary.model == "fake-1"
        assert await store.get_source_summary(summary.id) == summary
        assert await store.list_source_summaries(source_id) == (summary,)
        assert await store.read_source_content(await store.get_source(source_id)) == raw_before
        job = await store.get_job(summary.job_id)
        assert job.kind == "summarize_source"
        assert job.status == "succeeded"
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_pages") as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert client.last_tools is None
        assert "<h1>" not in client.last_system_prompt
        assert "Hello" in client.last_messages[0].content[0].text
    finally:
        await store.close()


async def test_invalid_agent_json_fails_job_without_persisting_summary(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta="```json\n{}\n```"), DoneEvent(stop_reason="stop")]])
    service = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    try:
        with pytest.raises(WikiStoreError) as exc_info:
            await service.summarize_source(source_id)
        assert exc_info.value.code == "invalid_summary"
        assert await store.list_source_summaries(source_id) == ()
        db = store._require_db()
        async with db.execute(
            "SELECT status, safe_error_code FROM wiki_jobs WHERE kind = 'summarize_source'"
        ) as cursor:
            row = await cursor.fetchone()
        assert tuple(row) == ("failed", "invalid_summary")
    finally:
        await store.close()


async def test_entry_page_proposal_is_deterministic_and_not_published(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    entries = WikiEntryPageService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        proposal = await entries.create_entry_page(summary.id)
        repeated = await entries.create_entry_page(summary.id)

        assert repeated == proposal
        assert proposal.kind == "entry"
        assert proposal.summary_id == summary.id
        assert proposal.slug == "guide-00000001"
        assert proposal.aliases == ("guide.html",)
        assert proposal.markdown.startswith("# Guide\n")
        assert "## 关键内容" in proposal.markdown
        assert "（来源页 1）" in proposal.markdown
        assert "Greeting" in proposal.markdown
        assert hashlib.sha256(proposal.markdown.encode()).hexdigest() == (proposal.content_sha256)
        assert await store.list_page_proposals(source_id) == (proposal,)
        job = await store.get_job(proposal.job_id)
        assert job.kind == "synthesize_entry_page"
        assert job.status == "succeeded"
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_pages") as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with db.execute(
            "SELECT COUNT(*) FROM wiki_jobs WHERE kind = 'synthesize_entry_page'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await store.close()


async def test_entry_page_rejects_summary_after_source_reparse(tmp_path: Path) -> None:
    store, ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    entries = WikiEntryPageService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        await ingestion.parse_source(source_id)
        with pytest.raises(WikiStoreError) as exc_info:
            await entries.create_entry_page(summary.id)
        assert exc_info.value.code == "source_conflict"
        assert await store.list_page_proposals(source_id) == ()
    finally:
        await store.close()


async def test_topic_page_proposals_are_atomic_children_of_entry(tmp_path: Path) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    pages = WikiEntryPageService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        entry = await pages.create_entry_page(summary.id)
        topics = await pages.create_topic_pages(summary.id)
        repeated = await pages.create_topic_pages(summary.id)

        assert repeated == topics
        assert len(topics) == 1
        topic = topics[0]
        assert topic.kind == "topic"
        assert topic.topic_ordinal == 0
        assert topic.parent_proposal_id == entry.id
        assert topic.slug == "greeting-00000001-1"
        assert topic.aliases == ()
        assert topic.markdown.startswith("# Greeting\n")
        assert "## 相关关键内容" in topic.markdown
        assert json.loads(topic.source_locator_json)["page_numbers"] == [1]
        assert await store.list_topic_page_proposals(summary.id) == topics
        assert await store.list_page_proposals(source_id) == (entry, topic)
        job = await store.get_job(topic.job_id)
        assert job.kind == "synthesize_topic_pages"
        assert job.status == "succeeded"
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_pages") as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with db.execute(
            "SELECT COUNT(*) FROM wiki_jobs WHERE kind = 'synthesize_topic_pages'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await store.close()


async def test_topic_pages_require_an_entry_proposal(tmp_path: Path) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    pages = WikiEntryPageService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        with pytest.raises(WikiStoreError) as exc_info:
            await pages.create_topic_pages(summary.id)
        assert exc_info.value.code == "page_proposal_not_found"
        assert await store.list_topic_page_proposals(summary.id) == ()
    finally:
        await store.close()


async def test_change_set_freezes_all_page_proposals_and_stable_diffs(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    pages = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        entry = await pages.create_entry_page(summary.id)
        topics = await pages.create_topic_pages(summary.id)
        change_set, items = await changes.create_from_summary(summary.id)
        repeated = await changes.create_from_summary(summary.id)

        assert repeated == (change_set, items)
        assert change_set.status == "awaiting_approval"
        assert change_set.source_summary_id == summary.id
        assert change_set.base_graph_revision == 0
        assert len(items) == 3
        assert [item.ordinal for item in items] == [0, 1, 2]
        assert [item.operation_kind for item in items] == [
            "page_create",
            "page_create",
            "edge_add",
        ]
        assert [json.loads(item.payload_json)["proposal_id"] for item in items[:2]] == [
            entry.id,
            topics[0].id,
        ]
        assert json.loads(items[2].payload_json) == {
            "from_proposal_id": topics[0].id,
            "relation_type": "part_of",
            "schema": "llm-wiki-edge-add/v1",
            "to_proposal_id": entry.id,
        }
        assert items[2].unified_diff == (
            f"+ edge pages/{topics[0].slug}.md --part_of--> pages/{entry.slug}.md\n"
        )
        assert items[0].unified_diff.startswith(
            f"--- /dev/null\n+++ pages/{entry.slug}.md\n@@ -0,0 +1,"
        )
        assert f"+# {entry.title}\n" in items[0].unified_diff
        assert await store.get_change_set(change_set.id) == change_set
        assert await store.list_change_set_items(change_set.id) == items
        assert await store.list_change_sets(summary.space_id) == (change_set,)
        assert await store.search_pages(summary.space_id, "Guide") == ()
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_pages") as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with db.execute("SELECT COUNT(*) FROM wiki_page_revisions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await store.close()


async def test_approval_atomically_publishes_pages_revisions_sources_and_mirrors(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    proposals = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        entry = await proposals.create_entry_page(summary.id)
        topics = await proposals.create_topic_pages(summary.id)
        change_set, _items = await changes.create_from_summary(summary.id)

        approved, pages = await changes.decide(change_set.id, approve=True)
        repeated = await changes.decide(change_set.id, approve=True)

        assert repeated == (approved, pages)
        assert approved.status == "approved"
        assert approved.decided_at_ms is not None
        assert approved.published_at_ms is not None
        assert [page.slug for page in pages] == [entry.slug, topics[0].slug]
        assert all(page.status == "active" and page.version == 1 for page in pages)
        assert await store.list_pages(summary.space_id) == tuple(
            sorted(pages, key=lambda page: (page.slug, page.id))
        )
        for page in pages:
            assert page.current_revision_id is not None
            revision = await store.get_page_revision(page.current_revision_id)
            assert revision.page_id == page.id
            assert revision.change_set_id == approved.id
            assert revision.author_kind == "agent"
            assert await store.list_page_revisions(page.id) == (revision,)
            mirror = store.file_store.read_owned_file(
                page.space_id,
                f"pages/{page.slug}.md",
                max_bytes=500_000,
            ).decode()
            assert mirror == revision.markdown
        items = await store.list_change_set_items(approved.id)
        assert [item.target_id for item in items[:2]] == [page.id for page in pages]
        assert items[2].target_id.startswith("edge_")
        edges = await store.list_edges(summary.space_id)
        assert len(edges) == 1
        assert edges[0].id == items[2].target_id
        assert edges[0].from_page_id == pages[1].id
        assert edges[0].to_page_id == pages[0].id
        assert edges[0].relation_type == "part_of"
        graph = await store.get_graph_snapshot(summary.space_id)
        assert graph.graph_revision == 1
        listed_page_ids = [page.id for page in await store.list_pages(summary.space_id)]
        assert [(node.kind, node.id) for node in graph.nodes] == [
            *(("page", page_id) for page_id in listed_page_ids),
            ("source", source_id),
        ]
        assert [edge.relation_type for edge in graph.edges] == [
            "part_of",
            "derived_from",
            "derived_from",
        ]
        assert graph.edges[0].system_managed is False
        assert all(edge.system_managed for edge in graph.edges[1:])
        neighborhood = await store.get_page_graph_neighborhood(
            summary.space_id,
            pages[0].id,
        )
        assert {node.id for node in neighborhood.nodes} == {
            pages[0].id,
            pages[1].id,
            source_id,
        }
        assert [edge.relation_type for edge in neighborhood.edges] == [
            "part_of",
            "derived_from",
        ]
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_page_sources") as cursor:
            assert (await cursor.fetchone())[0] == 2
        async with db.execute(
            "SELECT COUNT(*) FROM wiki_edges WHERE relation_type = 'derived_from'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert (await store.get_space(summary.space_id)).graph_revision == 1
        guide_hits = await store.search_pages(summary.space_id, "Guide")
        assert guide_hits
        assert guide_hits[0].page_id == pages[0].id
        assert await store.search_pages(summary.space_id, "RawOnlySecret") == ()
        assert await store.search_pages(summary.space_id, summary.id) == ()
        other_space = await store.create_space(name="Other")
        assert await store.search_pages(other_space.id, "Guide") == ()
        missing_mirror = tmp_path / "spaces" / summary.space_id / "pages" / f"{pages[0].slug}.md"
        missing_mirror.unlink()
        await store.close()
        reopened = await WikiStore.open(tmp_path)
        try:
            repaired_revision = await reopened.get_page_revision(pages[0].current_revision_id or "")
            assert missing_mirror.read_text(encoding="utf-8") == (repaired_revision.markdown)
            assert await reopened.search_pages(summary.space_id, "Guide")
        finally:
            await reopened.close()
    finally:
        await store.close()


def test_wiki_fts_query_compiler_quotes_every_user_term() -> None:
    assert compile_wiki_fts_query("guide topic") == '"guide" "topic"'
    assert compile_wiki_fts_query("foo OR *") == '"foo" "OR" "*"'
    assert compile_wiki_fts_query('a"b') == '"a""b"'
    with pytest.raises(WikiStoreError) as exc_info:
        compile_wiki_fts_query("   ")
    assert exc_info.value.code == "invalid_search_query"


async def test_rejection_is_idempotent_and_publishes_nothing(tmp_path: Path) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    proposals = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        await proposals.create_entry_page(summary.id)
        await proposals.create_topic_pages(summary.id)
        change_set, _items = await changes.create_from_summary(summary.id)

        rejected = await changes.decide(change_set.id, approve=False)
        assert await changes.decide(change_set.id, approve=False) == rejected
        assert rejected[0].status == "rejected"
        assert rejected[1] == ()
        assert await store.list_pages(summary.space_id) == ()
        assert (await store.get_space(summary.space_id)).graph_revision == 0
    finally:
        await store.close()


async def test_part_of_cycle_rejects_the_whole_change_set(tmp_path: Path) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    proposals = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        entry = await proposals.create_entry_page(summary.id)
        topic = (await proposals.create_topic_pages(summary.id))[0]
        change_set, _items = await changes.create_from_summary(summary.id)
        reverse_payload = json.dumps(
            {
                "schema": "llm-wiki-edge-add/v1",
                "from_proposal_id": entry.id,
                "to_proposal_id": topic.id,
                "relation_type": "part_of",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        db = store._require_db()
        await db.execute(
            """
            INSERT INTO wiki_change_set_items (
                id, change_set_id, ordinal, operation_kind, target_id,
                base_version, before_sha256, payload_json, unified_diff,
                created_at_ms
            ) VALUES (?, ?, 3, 'edge_add', '', NULL, '', ?, ?, 100)
            """,
            (
                "change_item_ffffffffffffffffffffffff",
                change_set.id,
                reverse_payload,
                (f"+ edge pages/{entry.slug}.md --part_of--> pages/{topic.slug}.md\n"),
            ),
        )
        await db.commit()

        with pytest.raises(WikiStoreError) as exc_info:
            await changes.decide(change_set.id, approve=True)
        assert exc_info.value.code == "invalid_change_set"
        assert await store.list_pages(summary.space_id) == ()
        assert await store.list_edges(summary.space_id) == ()
        assert (await store.get_space(summary.space_id)).graph_revision == 0
        assert (await store.get_change_set(change_set.id)).status == ("awaiting_approval")
    finally:
        await store.close()


async def test_knowledge_page_patch_requires_approval_and_stales_on_race(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    proposals = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        await proposals.create_entry_page(summary.id)
        await proposals.create_topic_pages(summary.id)
        initial_change_set, _items = await changes.create_from_summary(summary.id)
        _approved, pages = await changes.decide(initial_change_set.id, approve=True)
        page = pages[0]
        conversation = await store.create_conversation(
            summary.space_id,
            session_id="sess-100-aaaaaaaa",
            title="Edit Guide",
        )
        other_space = await store.create_space(name="Other Wiki")
        other_conversation = await store.create_conversation(
            other_space.id,
            session_id="sess-100-bbbbbbbb",
            title="Other",
        )
        with pytest.raises(WikiStoreError) as cross_space:
            await changes.propose_page_patch(
                other_conversation.id,
                page_id=page.id,
                title="Forbidden",
                markdown="# Forbidden",
            )
        assert cross_space.value.code == "page_not_found"

        registry = build_knowledge_tool_registry(store, changes)
        binding_token = knowledge_agent_binding.set(
            KnowledgeAgentBinding(
                conversation_id=conversation.id,
                space_id=conversation.space_id,
                session_id=conversation.session_id,
            )
        )
        try:
            tool_result = await registry.get("wiki_propose_page_patch").execute(
                "tool-call-1",
                {
                    "page_id": page.id,
                    "title": "Updated Guide",
                    "aliases": ["Guide v2"],
                    "markdown": "# Updated Guide\n\nApprovalOnlyTerm",
                },
            )
        finally:
            knowledge_agent_binding.reset(binding_token)
        result_payload = json.loads(tool_result.content[0].text)
        assert result_payload["published"] is False
        assert result_payload["approval_required"] is True
        first = await store.get_change_set(result_payload["change_set"]["id"])
        first_items = await store.list_change_set_items(first.id)
        repeated = await changes.propose_page_patch(
            conversation.id,
            page_id=page.id,
            title="Updated Guide",
            aliases=("Guide v2",),
            markdown="# Updated Guide\n\nApprovalOnlyTerm",
        )
        competing, _ = await changes.propose_page_patch(
            conversation.id,
            page_id=page.id,
            title="Competing Guide",
            markdown="# Competing Guide\n\nLosesRaceTerm",
        )

        assert repeated == (first, first_items)
        assert first.conversation_id == conversation.id
        assert first.source_summary_id is None
        assert first.status == "awaiting_approval"
        assert len(first_items) == 1
        assert first_items[0].operation_kind == "page_update"
        assert first_items[0].target_id == page.id
        assert first_items[0].base_version == 1
        assert await store.search_pages(summary.space_id, "ApprovalOnlyTerm") == ()

        approved, updated_pages = await changes.decide(first.id, approve=True)
        assert approved.status == "approved"
        assert len(updated_pages) == 1
        updated = updated_pages[0]
        assert updated.id == page.id
        assert updated.version == 2
        assert updated.title == "Updated Guide"
        assert updated.aliases == ("Guide v2",)
        hits = await store.search_pages(summary.space_id, "ApprovalOnlyTerm")
        assert [hit.page_id for hit in hits] == [page.id]
        revision = await store.get_page_revision(updated.current_revision_id or "")
        assert revision.version == 2
        assert revision.author_kind == "agent"
        assert revision.change_set_id == approved.id
        assert (
            store.file_store.read_owned_file(
                updated.space_id,
                f"pages/{updated.slug}.md",
                max_bytes=500_000,
            ).decode()
            == revision.markdown
        )

        with pytest.raises(WikiStoreError) as exc_info:
            await changes.decide(competing.id, approve=True)
        assert exc_info.value.code == "change_set_stale"
        assert (await store.get_change_set(competing.id)).status == "stale"
        assert await store.search_pages(summary.space_id, "LosesRaceTerm") == ()
    finally:
        await store.close()


async def test_knowledge_mutation_tools_publish_only_after_approval(
    tmp_path: Path,
) -> None:
    store, _ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    proposals = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        await proposals.create_entry_page(summary.id)
        await proposals.create_topic_pages(summary.id)
        initial, _items = await changes.create_from_summary(summary.id)
        _approved, initial_pages = await changes.decide(initial.id, approve=True)
        entry_page = initial_pages[0]
        conversation = await store.create_conversation(
            summary.space_id,
            session_id="sess-100-mutations",
            title="Manage Wiki",
        )
        registry = build_knowledge_tool_registry(store, changes)
        binding_token = knowledge_agent_binding.set(
            KnowledgeAgentBinding(
                conversation_id=conversation.id,
                space_id=conversation.space_id,
                session_id=conversation.session_id,
            )
        )
        try:
            create_result = await registry.get("wiki_propose_page_create").execute(
                "tool-create",
                {
                    "slug": "agent-notes",
                    "title": "Agent Notes",
                    "aliases": ["Notes"],
                    "markdown": "# Agent Notes\n\nCreateApprovalOnlyTerm",
                },
            )
            create_payload = json.loads(create_result.content[0].text)
            assert create_payload["published"] is False
            assert create_payload["approval_required"] is True
            assert not any(
                page.slug == "agent-notes" for page in await store.list_pages(summary.space_id)
            )
            assert await store.search_pages(summary.space_id, "CreateApprovalOnlyTerm") == ()

            create_change_set_id = create_payload["change_set"]["id"]
            approved_create, created_pages = await changes.decide(
                create_change_set_id,
                approve=True,
            )
            assert approved_create.status == "approved"
            assert len(created_pages) == 1
            created_page = created_pages[0]
            assert created_page.slug == "agent-notes"
            assert [
                hit.page_id
                for hit in await store.search_pages(
                    summary.space_id,
                    "CreateApprovalOnlyTerm",
                )
            ] == [created_page.id]
            assert store.file_store.owned_file_exists(
                summary.space_id,
                "pages/agent-notes.md",
            )

            edge_result = await registry.get("wiki_propose_edge_changes").execute(
                "tool-edge-add",
                {
                    "additions": [
                        {
                            "from_page_id": created_page.id,
                            "to_page_id": entry_page.id,
                            "relation_type": "related_to",
                        }
                    ],
                    "deletions": [],
                },
            )
            edge_payload = json.loads(edge_result.content[0].text)
            assert not any(
                edge.relation_type == "related_to"
                and {edge.from_page_id, edge.to_page_id} == {created_page.id, entry_page.id}
                for edge in await store.list_edges(summary.space_id)
            )
            await changes.decide(edge_payload["change_set"]["id"], approve=True)
            related_edge = next(
                edge
                for edge in await store.list_edges(summary.space_id)
                if edge.relation_type == "related_to"
                and {edge.from_page_id, edge.to_page_id} == {created_page.id, entry_page.id}
            )

            delete_edge_result = await registry.get("wiki_propose_edge_changes").execute(
                "tool-edge-delete",
                {"additions": [], "deletions": [related_edge.id]},
            )
            delete_edge_payload = json.loads(delete_edge_result.content[0].text)
            assert any(
                edge.id == related_edge.id for edge in await store.list_edges(summary.space_id)
            )
            await changes.decide(
                delete_edge_payload["change_set"]["id"],
                approve=True,
            )
            assert not any(
                edge.id == related_edge.id for edge in await store.list_edges(summary.space_id)
            )

            with pytest.raises(WikiStoreError) as derived_from:
                await registry.get("wiki_propose_edge_changes").execute(
                    "tool-forbidden-edge",
                    {
                        "additions": [
                            {
                                "from_page_id": created_page.id,
                                "to_page_id": entry_page.id,
                                "relation_type": "derived_from",
                            }
                        ]
                    },
                )
            assert derived_from.value.code == "invalid_change_set"

            delete_result = await registry.get("wiki_propose_page_delete").execute(
                "tool-page-delete",
                {"page_id": created_page.id},
            )
            delete_payload = json.loads(delete_result.content[0].text)
            assert (await store.get_page(created_page.id)).status == "active"
            await changes.decide(delete_payload["change_set"]["id"], approve=True)
            assert (await store.get_page(created_page.id)).status == "deleted"
            assert await store.search_pages(summary.space_id, "CreateApprovalOnlyTerm") == ()
            assert not store.file_store.owned_file_exists(
                summary.space_id,
                "pages/agent-notes.md",
            )

            with pytest.raises(WikiStoreError) as reused_slug:
                await changes.propose_page_create(
                    conversation.id,
                    slug="agent-notes",
                    title="Reused Notes",
                    markdown="# Reused Notes",
                )
            assert reused_slug.value.code == "change_set_conflict"
        finally:
            knowledge_agent_binding.reset(binding_token)
    finally:
        await store.close()


async def test_reparse_before_approval_marks_whole_change_set_stale(
    tmp_path: Path,
) -> None:
    store, ingestion, source_id = await _parsed_html_store(tmp_path)
    client = FakeClient([[TextDeltaEvent(delta=_summary_json()), DoneEvent(stop_reason="stop")]])
    summaries = WikiSummaryService(store, CoreAgentWikiSummaryAgent(lambda: client))
    proposals = WikiEntryPageService(store)
    changes = WikiChangeSetService(store)
    try:
        summary = await summaries.summarize_source(source_id)
        await proposals.create_entry_page(summary.id)
        await proposals.create_topic_pages(summary.id)
        change_set, _items = await changes.create_from_summary(summary.id)
        await ingestion.parse_source(source_id)

        with pytest.raises(WikiStoreError) as exc_info:
            await changes.decide(change_set.id, approve=True)
        assert exc_info.value.code == "change_set_stale"
        assert (await store.get_change_set(change_set.id)).status == "stale"
        assert await store.list_pages(summary.space_id) == ()
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_page_revisions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await store.close()


class _ReparsingAgent:
    def __init__(self, ingestion: WikiIngestionService, source_id: str) -> None:
        self._ingestion = ingestion
        self._source_id = source_id

    async def generate(self, prompt: str) -> WikiSummaryAgentResponse:
        del prompt
        await self._ingestion.parse_source(self._source_id)
        return WikiSummaryAgentResponse(
            text=_summary_json(),
            provider="fake",
            model="fake-1",
        )


async def test_reparse_during_agent_call_rejects_stale_summary(tmp_path: Path) -> None:
    store, ingestion, source_id = await _parsed_html_store(tmp_path)
    service = WikiSummaryService(store, _ReparsingAgent(ingestion, source_id))
    try:
        with pytest.raises(WikiStoreError) as exc_info:
            await service.summarize_source(source_id)
        assert exc_info.value.code == "source_conflict"
        assert await store.list_source_summaries(source_id) == ()
        source = await store.get_source(source_id)
        assert source.selection_version == 2
        db = store._require_db()
        async with db.execute(
            "SELECT status, safe_error_code FROM wiki_jobs WHERE kind = 'summarize_source'"
        ) as cursor:
            row = await cursor.fetchone()
        assert tuple(row) == ("failed", "source_changed")
    finally:
        await store.close()


def test_summary_content_rejects_out_of_order_page_numbers() -> None:
    payload = json.loads(_summary_json())
    payload["key_points"][0]["page_numbers"] = [2, 1]
    with pytest.raises(ValueError):
        WikiSourceSummaryContent.model_validate(payload)
