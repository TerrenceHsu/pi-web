"""Stable Change Set payload and diff generation for Wiki page proposals."""

from __future__ import annotations

import difflib
import hashlib
import json

from .errors import WikiStoreError
from .models import (
    WikiChangeSet,
    WikiChangeSetItem,
    WikiChangeSetOperationKind,
    WikiConversation,
    WikiPage,
    WikiPageProposal,
    WikiPageRevision,
    WikiRelationType,
)
from .store import WikiStore


class WikiChangeSetService:
    """Freeze all proposals from one Summary Draft into one approval unit."""

    def __init__(self, store: WikiStore) -> None:
        self._store = store

    async def create_from_summary(
        self,
        summary_id: str,
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        existing = await self._store.find_change_set_for_summary(summary_id)
        if existing is not None:
            return existing, await self._store.list_change_set_items(existing.id)
        summary = await self._store.get_source_summary(summary_id)
        entry = await self._store.find_entry_page_proposal(summary.id)
        if entry is None:
            raise WikiStoreError("page_proposal_not_found")
        topics = await self._store.list_topic_page_proposals(summary.id)
        if len(topics) != len(summary.content.topics):
            raise WikiStoreError("page_proposal_conflict")
        proposals = (entry, *topics)
        item_specs: list[tuple[WikiChangeSetOperationKind, str, str]] = [
            (
                "page_create",
                self._page_create_payload(proposal),
                self._page_create_diff(proposal),
            )
            for proposal in proposals
        ]
        item_specs.extend(
            (
                "edge_add",
                self._edge_add_payload(topic, entry),
                self._edge_add_diff(topic, entry),
            )
            for topic in topics
        )
        return await self._store.create_page_proposal_change_set(
            summary,
            proposals,
            item_specs,
        )

    async def decide(
        self,
        change_set_id: str,
        *,
        approve: bool,
    ) -> tuple[WikiChangeSet, tuple[WikiPage, ...]]:
        change_set, pages = await self._store.decide_change_set(
            change_set_id,
            approve=approve,
        )
        if change_set.status == "stale":
            raise WikiStoreError("change_set_stale")
        return change_set, pages

    async def propose_page_patch(
        self,
        conversation_id: str,
        *,
        page_id: str,
        title: str,
        markdown: str,
        aliases: tuple[str, ...] | None = None,
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        conversation = await self._store.get_conversation(conversation_id)
        if conversation.status != "active":
            raise WikiStoreError("conversation_conflict")
        page = await self._store.get_page(page_id)
        if (
            page.space_id != conversation.space_id
            or page.status != "active"
            or page.current_revision_id is None
        ):
            raise WikiStoreError("page_not_found")
        revision = await self._store.get_page_revision(page.current_revision_id)
        resolved_aliases = page.aliases if aliases is None else aliases
        content_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        payload = json.dumps(
            {
                "schema": "llm-wiki-page-update/v1",
                "page_id": page.id,
                "base_revision_id": revision.id,
                "title": title,
                "aliases": list(resolved_aliases),
                "markdown": markdown,
                "content_sha256": content_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        diff = "\n".join(
            difflib.unified_diff(
                revision.markdown.splitlines(),
                markdown.splitlines(),
                fromfile=f"pages/{page.slug}.md",
                tofile=f"pages/{page.slug}.md",
                lineterm="",
            )
        )
        if not diff:
            raise WikiStoreError("change_set_conflict")
        return await self._store.create_conversation_page_update_change_set(
            conversation,
            page,
            revision,
            title=title,
            aliases=resolved_aliases,
            markdown=markdown,
            payload_json=payload,
            unified_diff=f"{diff}\n",
        )

    async def propose_page_create(
        self,
        conversation_id: str,
        *,
        slug: str,
        title: str,
        markdown: str,
        aliases: tuple[str, ...] = (),
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        conversation = await self._active_conversation(conversation_id)
        content_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        try:
            page = WikiPage(
                id=f"page_{'0' * 24}",
                space_id=conversation.space_id,
                slug=slug,
                title=title,
                aliases=aliases,
                status="active",
                current_revision_id=f"page_revision_{'0' * 24}",
                version=1,
                created_at_ms=0,
                updated_at_ms=0,
            )
            WikiPageRevision(
                id=page.current_revision_id or "",
                page_id=page.id,
                version=1,
                title=title,
                markdown=markdown,
                content_sha256=content_sha256,
                change_set_id=f"change_set_{'0' * 24}",
                author_kind="agent",
                created_at_ms=0,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_change_set") from exc
        if any(
            existing.slug == page.slug
            for existing in await self._store.list_pages(
                conversation.space_id,
                include_deleted=True,
            )
        ):
            raise WikiStoreError("change_set_conflict")
        payload = json.dumps(
            {
                "schema": "llm-wiki-page-create-agent/v1",
                "slug": page.slug,
                "title": page.title,
                "aliases": list(page.aliases),
                "markdown": markdown,
                "content_sha256": content_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        diff = self._page_create_content_diff(page.slug, markdown)
        return await self._store.create_conversation_change_set(
            conversation,
            summary=f"Create Wiki page {page.title}.",
            item_specs=(("page_create", "", None, "", payload, diff),),
        )

    async def propose_page_delete(
        self,
        conversation_id: str,
        *,
        page_id: str,
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        conversation = await self._active_conversation(conversation_id)
        page = await self._active_page_in_conversation(conversation, page_id)
        if page.current_revision_id is None:
            raise WikiStoreError("page_not_found")
        revision = await self._store.get_page_revision(page.current_revision_id)
        payload = json.dumps(
            {
                "schema": "llm-wiki-page-delete/v1",
                "page_id": page.id,
                "base_revision_id": revision.id,
                "slug": page.slug,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        diff = self._page_delete_diff(page.slug, revision.markdown)
        return await self._store.create_conversation_change_set(
            conversation,
            summary=f"Delete Wiki page {page.title}.",
            item_specs=(
                (
                    "page_delete",
                    page.id,
                    page.version,
                    revision.content_sha256,
                    payload,
                    diff,
                ),
            ),
        )

    async def propose_edge_changes(
        self,
        conversation_id: str,
        *,
        additions: tuple[tuple[str, str, WikiRelationType], ...] = (),
        deletions: tuple[str, ...] = (),
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        conversation = await self._active_conversation(conversation_id)
        if not additions and not deletions:
            raise WikiStoreError("invalid_change_set")
        existing_edges = await self._store.list_edges(conversation.space_id)
        existing_by_id = {edge.id: edge for edge in existing_edges}
        existing_keys = {
            (edge.from_page_id, edge.to_page_id, edge.relation_type) for edge in existing_edges
        }
        specs: list[tuple[WikiChangeSetOperationKind, str, int | None, str, str, str]] = []
        pending_keys: set[tuple[str, str, WikiRelationType]] = set()
        deleting_ids: set[str] = set()
        for edge_id in deletions:
            edge = existing_by_id.get(edge_id)
            if edge is None or edge_id in deleting_ids:
                raise WikiStoreError("change_set_conflict")
            deleting_ids.add(edge_id)
            from_page = await self._active_page_in_conversation(
                conversation,
                edge.from_page_id,
            )
            to_page = await self._active_page_in_conversation(
                conversation,
                edge.to_page_id,
            )
            payload = json.dumps(
                {
                    "schema": "llm-wiki-edge-delete/v1",
                    "edge_id": edge.id,
                    "from_page_id": edge.from_page_id,
                    "to_page_id": edge.to_page_id,
                    "relation_type": edge.relation_type,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            diff = (
                f"- edge pages/{from_page.slug}.md --{edge.relation_type}--> "
                f"pages/{to_page.slug}.md\n"
            )
            specs.append(("edge_delete", edge.id, None, "", payload, diff))
        for raw_from_id, raw_to_id, relation_type in additions:
            if relation_type not in {
                "related_to",
                "references",
                "extends",
                "contradicts",
                "part_of",
            }:
                raise WikiStoreError("invalid_change_set")
            from_page = await self._active_page_in_conversation(
                conversation,
                raw_from_id,
            )
            to_page = await self._active_page_in_conversation(
                conversation,
                raw_to_id,
            )
            from_id, to_id = from_page.id, to_page.id
            if relation_type == "related_to" and from_id > to_id:
                from_id, to_id = to_id, from_id
                from_page, to_page = to_page, from_page
            key = (from_id, to_id, relation_type)
            if (
                from_id == to_id
                or key in pending_keys
                or (
                    key in existing_keys
                    and not any(
                        edge.id in deleting_ids
                        and (
                            edge.from_page_id,
                            edge.to_page_id,
                            edge.relation_type,
                        )
                        == key
                        for edge in existing_edges
                    )
                )
            ):
                raise WikiStoreError("change_set_conflict")
            pending_keys.add(key)
            payload = json.dumps(
                {
                    "schema": "llm-wiki-edge-add-agent/v1",
                    "from_page_id": from_id,
                    "to_page_id": to_id,
                    "relation_type": relation_type,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            diff = (
                f"+ edge pages/{from_page.slug}.md --{relation_type}--> pages/{to_page.slug}.md\n"
            )
            specs.append(("edge_add", "", None, "", payload, diff))
        return await self._store.create_conversation_change_set(
            conversation,
            summary=f"Change {len(specs)} Wiki page relation(s).",
            item_specs=tuple(specs),
        )

    async def _active_conversation(
        self,
        conversation_id: str,
    ) -> WikiConversation:
        conversation = await self._store.get_conversation(conversation_id)
        if conversation.status != "active":
            raise WikiStoreError("conversation_conflict")
        return conversation

    async def _active_page_in_conversation(
        self,
        conversation: WikiConversation,
        page_id: str,
    ) -> WikiPage:
        page = await self._store.get_page(page_id)
        if page.space_id != conversation.space_id or page.status != "active":
            raise WikiStoreError("page_not_found")
        return page

    @staticmethod
    def _page_create_content_diff(slug: str, markdown: str) -> str:
        lines = markdown.splitlines()
        return "\n".join(
            (
                "--- /dev/null",
                f"+++ pages/{slug}.md",
                f"@@ -0,0 +1,{len(lines)} @@",
                *(f"+{line}" for line in lines),
                "",
            )
        )

    @staticmethod
    def _page_delete_diff(slug: str, markdown: str) -> str:
        lines = markdown.splitlines()
        return "\n".join(
            (
                f"--- pages/{slug}.md",
                "+++ /dev/null",
                f"@@ -1,{len(lines)} +0,0 @@",
                *(f"-{line}" for line in lines),
                "",
            )
        )

    @staticmethod
    def _page_create_payload(proposal: WikiPageProposal) -> str:
        try:
            locator = json.loads(proposal.source_locator_json)
        except (TypeError, ValueError) as exc:
            raise WikiStoreError("invalid_page_proposal") from exc
        payload = {
            "schema": "llm-wiki-page-create/v1",
            "proposal_id": proposal.id,
            "proposal_kind": proposal.kind,
            "source_id": proposal.source_id,
            "summary_id": proposal.summary_id,
            "parent_proposal_id": proposal.parent_proposal_id,
            "slug": proposal.slug,
            "title": proposal.title,
            "aliases": list(proposal.aliases),
            "markdown": proposal.markdown,
            "content_sha256": proposal.content_sha256,
            "source_locator": locator,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _page_create_diff(proposal: WikiPageProposal) -> str:
        lines = proposal.markdown.splitlines()
        header = [
            "--- /dev/null",
            f"+++ pages/{proposal.slug}.md",
            f"@@ -0,0 +1,{len(lines)} @@",
        ]
        return "\n".join((*header, *(f"+{line}" for line in lines))) + "\n"

    @staticmethod
    def _edge_add_payload(
        topic: WikiPageProposal,
        entry: WikiPageProposal,
    ) -> str:
        payload = {
            "schema": "llm-wiki-edge-add/v1",
            "from_proposal_id": topic.id,
            "to_proposal_id": entry.id,
            "relation_type": "part_of",
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _edge_add_diff(
        topic: WikiPageProposal,
        entry: WikiPageProposal,
    ) -> str:
        return f"+ edge pages/{topic.slug}.md --part_of--> pages/{entry.slug}.md\n"


__all__ = ["WikiChangeSetService"]
