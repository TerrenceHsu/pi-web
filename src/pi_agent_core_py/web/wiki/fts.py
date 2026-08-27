"""Safe literal query compiler for approved Wiki page FTS5."""

from __future__ import annotations

import re

from .errors import WikiStoreError

MAX_WIKI_FTS_QUERY_CHARS = 200
_TERM_SPLIT_RE = re.compile(r"\s+")


def compile_wiki_fts_query(query: str) -> str:
    """Turn user text into implicit-AND quoted phrases, never raw FTS syntax."""
    if not isinstance(query, str):
        raise WikiStoreError("invalid_search_query")
    stripped = query.strip()
    if (
        not stripped
        or len(query) > MAX_WIKI_FTS_QUERY_CHARS
        or "\x00" in query
        or any(ord(char) < 32 and not char.isspace() for char in query)
    ):
        raise WikiStoreError("invalid_search_query")
    terms = [term for term in _TERM_SPLIT_RE.split(stripped) if term]
    if not terms:
        raise WikiStoreError("invalid_search_query")
    quoted: list[str] = []
    for term in terms:
        escaped = term.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


__all__ = ["MAX_WIKI_FTS_QUERY_CHARS", "compile_wiki_fts_query"]
