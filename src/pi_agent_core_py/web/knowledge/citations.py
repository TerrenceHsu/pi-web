"""Citation parsing, validation, and rendering (P2-R4-C1).

Per P2-R4-A frozen contract §25-35 / §45-52:

- ``[cite:E1]`` is the only citation token format
- Strict regex: ``\\[cite:(E[1-9][0-9]*)\\]``
- Server controls citation metadata (filename / page) — LLM only chooses evidence
- First-use numbering: E4 before E2 → E4=[1], E2=[2]
- Same evidence re-cited → same number
- Unknown evidence → remove token + warning
- Renderer produces ``[1] filename.pdf · p.12`` / ``pp.12–13``
- No DB access — operates purely on EvidenceRegistry snapshot
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import EvidenceRegistry

# ============================================================================
# Markdown escape — prevents source_name injection
# ============================================================================

#: Characters that can form Markdown structure when embedded in text.
#: Escaped with backslash per CommonMark §2.4. Prevents filenames like
#: ``[click](url).pdf`` or ``**bold**.pdf`` from injecting links,
#: emphasis, code spans, images, or autolinks into citation footers.
_MD_ESCAPE_RE: re.Pattern[str] = re.compile(r"([\\`*\[\]()!<|>])")


def _escape_markdown_text(s: str) -> str:
    """Escape Markdown-special characters in plain text.

    Applied to ``source_filename`` before embedding in citation footer
    so that user-supplied filenames cannot inject Markdown structure
    (links, images, emphasis, code spans, autolinks, table separators).

    Does NOT escape ``_``, ``#``, ``+``, ``-``, ``.``, ``{``, ``}``,
    ``~``, ``"`` — these are either safe in mid-line text (not at line
    start in the footer context) or form only benign emphasis (not
    security-relevant).
    """
    return _MD_ESCAPE_RE.sub(r"\\\1", s)

# ============================================================================
# Constants
# ============================================================================

#: Strict citation token regex. Case-sensitive lowercase ``cite``,
#: uppercase ``E``, positive integer (E0 invalid).
_CITATION_RE: re.Pattern[str] = re.compile(r"\[cite:(E[1-9][0-9]*)\]")

# ============================================================================
# DTOs
# ============================================================================


@dataclass(frozen=True, slots=True)
class CitationEntry:
    """A single resolved citation in the final rendered output."""

    number: int
    evidence_id: str
    source_filename: str
    page_start: int
    page_end: int


@dataclass(frozen=True, slots=True)
class CitationResult:
    """Result of citation processing on assistant text.

    ``rendered_content`` has ``[cite:E1]`` replaced with ``[1]`` etc.
    ``citations`` is the source list (may be appended as a footer).
    ``warnings`` lists unknown evidence IDs that were removed.
    """

    rendered_content: str
    citations: tuple[CitationEntry, ...]
    warnings: tuple[str, ...]


# ============================================================================
# CitationParser
# ============================================================================


def parse_citations(text: str) -> list[str]:
    """Extract all evidence IDs from ``[cite:EX]`` tokens in ``text``.

    Returns IDs in order of first appearance. Duplicates preserved
    (caller decides how to handle). Malformed tokens (``[cite:]``,
    ``[cite:e1]``, ``[CITE:E1]``, ``[cite:E0]``) are NOT matched.
    """
    return [m.group(1) for m in _CITATION_RE.finditer(text)]


# ============================================================================
# CitationProcessor — validate + render in one pass
# ============================================================================


def process_citations(
    text: str,
    registry: EvidenceRegistry,
) -> CitationResult:
    """Process assistant text: validate citations, render numbered refs.

    Flow:
      1. Find all ``[cite:EX]`` tokens in order.
      2. For each: look up EX in registry.
         - Valid → assign citation number (first-use order).
         - Invalid → record warning, will be removed.
      3. Replace tokens in text: valid → ``[N]``, invalid → removed.
      4. Build ``CitationEntry`` list for source footer.

    Args:
        text: The raw assistant-generated text containing ``[cite:EX]``.
        registry: The turn-scoped EvidenceRegistry with all evidence.

    Returns:
        CitationResult with rendered content, citation entries, warnings.
    """
    # Assign citation numbers in first-use order.
    number_map: dict[str, int] = {}  # evidence_id → citation_number
    next_number = 1
    warnings: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        nonlocal next_number
        eid = match.group(1)
        evidence = registry.lookup(eid)
        if evidence is None:
            warnings.append(eid)
            return ""  # remove invalid citation token
        if eid not in number_map:
            number_map[eid] = next_number
            next_number += 1
        return f"[{number_map[eid]}]"

    rendered = _CITATION_RE.sub(_replace, text)

    # Build citation entries in number order.
    id_by_number = {n: eid for eid, n in number_map.items()}
    citations: list[CitationEntry] = []
    for num in sorted(number_map.values()):
        eid = id_by_number[num]
        ev = registry.lookup(eid)
        assert ev is not None  # validated above
        citations.append(
            CitationEntry(
                number=num,
                evidence_id=eid,
                source_filename=ev.source_filename,
                page_start=ev.page_start,
                page_end=ev.page_end,
            )
        )

    return CitationResult(
        rendered_content=rendered,
        citations=tuple(citations),
        warnings=tuple(warnings),
    )


# ============================================================================
# CitationRenderer — format source list
# ============================================================================


def render_source_footer(citations: tuple[CitationEntry, ...]) -> str:
    """Render a ``Sources:`` footer from citation entries.

    Format::

        Sources:
        [1] filename.pdf · p.12
        [2] other.pdf · pp.7–8

    Empty input → empty string (no footer).
    """
    if not citations:
        return ""

    lines = ["Sources:"]
    for c in citations:
        if c.page_start == c.page_end:
            pages = f"p.{c.page_start}"
        else:
            pages = f"pp.{c.page_start}–{c.page_end}"
        safe_name = _escape_markdown_text(c.source_filename)
        lines.append(f"[{c.number}] {safe_name} · {pages}")
    return "\n".join(lines)


def render_citation_inline(entry: CitationEntry) -> str:
    """Render a single citation's inline format (without brackets).

    ``filename.pdf · p.12`` or ``filename.pdf · pp.12–13``
    """
    if entry.page_start == entry.page_end:
        pages = f"p.{entry.page_start}"
    else:
        pages = f"pp.{entry.page_start}–{entry.page_end}"
    safe_name = _escape_markdown_text(entry.source_filename)
    return f"{safe_name} · {pages}"
