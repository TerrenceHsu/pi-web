"""Deterministic Wiki entry-page proposals derived from Summary Drafts."""

from __future__ import annotations

import html
import json
import re
import unicodedata

from .errors import WikiStoreError
from .models import WikiJob, WikiPageProposal, WikiSourceSummaryDraft
from .store import WikiStore

_MARKDOWN_PUNCTUATION_RE = re.compile(r"([\\`*_[\]{}()#+.!|>~-])")


class WikiEntryPageService:
    """Render one immutable entry proposal without publishing a Wiki page."""

    def __init__(self, store: WikiStore) -> None:
        self._store = store

    async def create_entry_page(self, summary_id: str) -> WikiPageProposal:
        existing = await self._store.find_entry_page_proposal(summary_id)
        if existing is not None:
            return existing
        summary = await self._store.get_source_summary(summary_id)
        source = await self._store.get_source(summary.source_id, space_id=summary.space_id)
        if (
            source.status != "parsed"
            or source.source_sha256 != summary.source_sha256
            or source.selection_version != summary.selection_version
            or source.selected_parse_revision_id != summary.parse_revision_id
        ):
            raise WikiStoreError("source_conflict")

        job: WikiJob | None = None
        try:
            job = await self._store.create_job(
                summary.space_id,
                kind="synthesize_entry_page",
                source_id=summary.source_id,
            )
            job = await self._store.set_job_status(job.id, "running")
            markdown = self._render_entry_markdown(summary, source.display_name)
            cited_pages = {
                page for item in summary.content.key_points for page in item.page_numbers
            }
            cited_pages.update(
                page for item in summary.content.topics for page in item.page_numbers
            )
            page_numbers = sorted(cited_pages)
            locator_json = json.dumps(
                {
                    "source_id": summary.source_id,
                    "parse_revision_id": summary.parse_revision_id,
                    "summary_id": summary.id,
                    "page_numbers": page_numbers,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            aliases = (
                ()
                if source.display_name.casefold() == summary.content.suggested_title.casefold()
                else (source.display_name,)
            )
            proposal = self._store.new_entry_page_proposal(
                summary,
                job,
                title=summary.content.suggested_title,
                slug=self._entry_slug(summary),
                aliases=aliases,
                markdown=markdown,
                source_locator_json=locator_json,
            )
            return await self._store.complete_entry_page_proposal(summary, proposal)
        except WikiStoreError as exc:
            await self._fail_job_quietly(job, self._safe_failure_code(exc))
            raise
        except Exception as exc:
            await self._fail_job_quietly(job, "entry_page_failed")
            raise WikiStoreError("invalid_page_proposal") from exc

    async def create_topic_pages(
        self,
        summary_id: str,
    ) -> tuple[WikiPageProposal, ...]:
        summary = await self._store.get_source_summary(summary_id)
        entry = await self._store.find_entry_page_proposal(summary.id)
        if entry is None:
            raise WikiStoreError("page_proposal_not_found")
        existing = await self._store.list_topic_page_proposals(summary.id)
        if existing:
            if len(existing) != len(summary.content.topics):
                raise WikiStoreError("page_proposal_conflict")
            return existing
        if not summary.content.topics:
            return ()
        source = await self._store.get_source(summary.source_id, space_id=summary.space_id)
        if (
            source.status != "parsed"
            or source.source_sha256 != summary.source_sha256
            or source.selection_version != summary.selection_version
            or source.selected_parse_revision_id != summary.parse_revision_id
        ):
            raise WikiStoreError("source_conflict")

        job: WikiJob | None = None
        try:
            job = await self._store.create_job(
                summary.space_id,
                kind="synthesize_topic_pages",
                source_id=summary.source_id,
            )
            job = await self._store.set_job_status(job.id, "running")
            proposals: list[WikiPageProposal] = []
            for ordinal, topic in enumerate(summary.content.topics):
                locator_json = json.dumps(
                    {
                        "source_id": summary.source_id,
                        "parse_revision_id": summary.parse_revision_id,
                        "summary_id": summary.id,
                        "page_numbers": list(topic.page_numbers),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                proposals.append(
                    self._store.new_topic_page_proposal(
                        summary,
                        entry,
                        job,
                        topic_ordinal=ordinal,
                        title=topic.title,
                        slug=self._topic_slug(summary, topic.title, ordinal),
                        markdown=self._render_topic_markdown(summary, ordinal),
                        source_locator_json=locator_json,
                    )
                )
            return await self._store.complete_topic_page_proposals(
                summary,
                entry,
                proposals,
            )
        except WikiStoreError as exc:
            await self._fail_job_quietly(job, self._safe_failure_code(exc))
            raise
        except Exception as exc:
            await self._fail_job_quietly(job, "topic_pages_failed")
            raise WikiStoreError("invalid_page_proposal") from exc

    @classmethod
    def _render_entry_markdown(
        cls,
        summary: WikiSourceSummaryDraft,
        source_display_name: str,
    ) -> str:
        content = summary.content
        lines = [
            f"# {cls._escape_text(content.suggested_title)}",
            "",
            f"> 来源：{cls._escape_text(source_display_name)}",
            "",
            cls._escape_text(content.overview),
            "",
            "## 关键内容",
            "",
        ]
        for point in content.key_points:
            lines.append(f"- {cls._escape_text(point.text)} {cls._page_label(point.page_numbers)}")
        if content.topics:
            lines.extend(("", "## 主题导航", ""))
            for topic in content.topics:
                lines.append(
                    f"- **{cls._escape_text(topic.title)}**："
                    f"{cls._escape_text(topic.summary)} "
                    f"{cls._page_label(topic.page_numbers)}"
                )
        if content.caveats:
            lines.extend(("", "## 限制与注意事项", ""))
            lines.extend(f"- {cls._escape_text(item)}" for item in content.caveats)
        return "\n".join(lines).strip()

    @classmethod
    def _render_topic_markdown(
        cls,
        summary: WikiSourceSummaryDraft,
        ordinal: int,
    ) -> str:
        topic = summary.content.topics[ordinal]
        topic_pages = set(topic.page_numbers)
        relevant_points = [
            point
            for point in summary.content.key_points
            if topic_pages.intersection(point.page_numbers)
        ]
        lines = [
            f"# {cls._escape_text(topic.title)}",
            "",
            cls._escape_text(topic.summary),
            "",
            f"来源范围：{cls._page_label(topic.page_numbers)}",
        ]
        if relevant_points:
            lines.extend(("", "## 相关关键内容", ""))
            for point in relevant_points:
                cited = tuple(page for page in point.page_numbers if page in topic_pages)
                lines.append(f"- {cls._escape_text(point.text)} {cls._page_label(cited)}")
        return "\n".join(lines).strip()

    @staticmethod
    def _escape_text(value: str) -> str:
        escaped = html.escape(value, quote=False)
        escaped = _MARKDOWN_PUNCTUATION_RE.sub(r"\\\1", escaped)
        return escaped.replace("\n", "<br>\n")

    @staticmethod
    def _page_label(page_numbers: tuple[int, ...]) -> str:
        joined = "、".join(str(page) for page in page_numbers)
        return f"（来源页 {joined}）"

    @staticmethod
    def _entry_slug(summary: WikiSourceSummaryDraft) -> str:
        stem = WikiEntryPageService._slug_stem(summary.content.suggested_title)
        suffix = summary.source_id[-8:]
        stem = stem[: 120 - len(suffix) - 1].rstrip("-") or "source"
        return f"{stem}-{suffix}"

    @staticmethod
    def _topic_slug(
        summary: WikiSourceSummaryDraft,
        title: str,
        ordinal: int,
    ) -> str:
        stem = WikiEntryPageService._slug_stem(title)
        suffix = f"{summary.source_id[-8:]}-{ordinal + 1}"
        stem = stem[: 120 - len(suffix) - 1].rstrip("-") or "topic"
        return f"{stem}-{suffix}"

    @staticmethod
    def _slug_stem(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        pieces: list[str] = []
        pending_dash = False
        for char in normalized:
            if char.isalnum():
                if pending_dash and pieces:
                    pieces.append("-")
                pieces.append(char)
                pending_dash = False
            else:
                pending_dash = True
        return "".join(pieces).strip("-") or "source"

    async def _fail_job_quietly(self, job: WikiJob | None, code: str) -> None:
        if job is None or job.status != "running":
            return
        try:
            await self._store.set_job_status(job.id, "failed", safe_error_code=code)
        except WikiStoreError:
            pass

    @staticmethod
    def _safe_failure_code(exc: WikiStoreError) -> str:
        if exc.code == "source_conflict":
            return "source_changed"
        if exc.code in {"invalid_page_proposal", "page_proposal_conflict"}:
            return exc.code
        return "entry_page_failed"


__all__ = ["WikiEntryPageService"]
