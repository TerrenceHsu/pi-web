"""search_knowledge Agent Tool (P2-R4-B2).

Per P2-R4-A frozen contract §2.1-2.2 / §10 / §36:

- Tool name: ``search_knowledge``
- Tool args: ``query`` + ``limit`` ONLY (``additionalProperties: false``)
- FORBIDDEN args: ``session_id``, ``library_id``, ``library_ids``, etc.
- Session from ``session_id_getter`` closure (existing pattern)
- EvidenceRegistry from ``evidence_registry_getter`` closure (lazy init)
- Calls ``SearchKnowledgeService.search()`` (R4-B1)
- Serializes result as structured text for LLM
- Does NOT implement Citation parsing/validation/rendering (R4-C)
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...messages import TextContent
from ...tools import AgentTool, ToolResult
from .evidence import EvidenceRegistry
from .search_models import DEFAULT_TOOL_LIMIT, KnowledgeSearchError
from .search_service import SearchKnowledgeService

if TYPE_CHECKING:
    pass


class SearchKnowledgeTool(AgentTool):
    """Agent-facing knowledge search tool.

    Registered when Knowledge subsystem is enabled. Provides
    Session-scoped FTS5 search via ``SearchKnowledgeService``.

    The LLM controls ``query`` and ``limit``. The Runtime controls
    Session identity, Library ACL, and Evidence numbering.
    """

    name = "search_knowledge"
    label = "Search Knowledge"
    description = (
        "Search knowledge libraries available to the current conversation "
        "for information relevant to the user's question.\n\n"
        "The search scope is automatically restricted to knowledge "
        "libraries available to the current session.\n\n"
        "Use the evidence IDs returned by this tool when referring to "
        "retrieved knowledge.\n\n"
        "Do not invent source filenames, page numbers, or evidence IDs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The knowledge query to search for.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
                "description": "Maximum number of results to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    execution_mode = "parallel"

    def __init__(
        self,
        *,
        search_service: SearchKnowledgeService,
        session_id_getter: Callable[[], str | None],
        evidence_registry_getter: Callable[[], EvidenceRegistry],
    ) -> None:
        self._service = search_service
        self._session_id_getter = session_id_getter
        self._evidence_registry_getter = evidence_registry_getter

    async def execute(
        self, tool_call_id: str, args: dict[str, Any]
    ) -> ToolResult:
        query = args.get("query", "")
        limit = args.get("limit", DEFAULT_TOOL_LIMIT)

        # Trusted session from closure (NOT from args).
        session_id = self._session_id_getter()
        if not session_id:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(
                    text="Knowledge search is unavailable: no active session.",
                )],
                is_error=True,
                details={"error_code": "knowledge_search_session_missing"},
            )

        registry = self._evidence_registry_getter()

        try:
            result = await self._service.search(
                session_id=session_id,
                query=query,
                limit=limit,
                registry=registry,
            )
        except KnowledgeSearchError as exc:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(
                    text=f"Knowledge search error: {exc.message}",
                )],
                is_error=True,
                details={"error_code": exc.code},
            )
        except Exception:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(
                    text="Knowledge search is temporarily unavailable.",
                )],
                is_error=True,
                details={"error_code": "knowledge_search_failed"},
            )

        text = self._serialize_result(query, result.hits)
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details={
                "query": query,
                "limit": limit,
                "result_count": len(result.hits),
            },
        )

    @staticmethod
    def _serialize_result(
        query: str,
        hits: tuple,
    ) -> str:
        """Serialize search hits as structured text for the LLM.

        Format per R4-A §34:
        ::
            Search results for: "query"

            [E1]
            Source: filename.pdf
            Pages: 12-13
            Heading: A > B > C
            Content: ...

            [E2]
            ...
        """
        if not hits:
            return (
                "No relevant knowledge was found in the libraries "
                "available to this conversation."
            )

        lines = [f'Search results for: "{query}"', ""]
        for ev in hits:
            lines.append(f"[{ev.evidence_id}]")
            lines.append(f"Source: {ev.source_filename}")
            if ev.page_start == ev.page_end:
                lines.append(f"Page: {ev.page_start}")
            else:
                lines.append(f"Pages: {ev.page_start}-{ev.page_end}")
            if ev.heading_path:
                lines.append(f"Heading: {' > '.join(ev.heading_path)}")
            lines.append(f"Content: {ev.content}")
            lines.append("")
        return "\n".join(lines).rstrip()


def create_search_knowledge_tool(
    *,
    search_service: SearchKnowledgeService,
    session_id_getter: Callable[[], str | None],
    evidence_registry_getter: Callable[[], EvidenceRegistry],
) -> SearchKnowledgeTool:
    """Factory: construct search_knowledge tool with trusted closures."""
    return SearchKnowledgeTool(
        search_service=search_service,
        session_id_getter=session_id_getter,
        evidence_registry_getter=evidence_registry_getter,
    )
