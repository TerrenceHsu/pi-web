"""Heading-aware Knowledge Chunker (P2-R3-A).

Per P2-R3 startup directive §10-§16 + §22-§23:

- Pure function over Canonical Markdown text → ``list[KnowledgeChunk]``
- Deterministic — same ``(document_id, markdown, expected_page_count)`` always
  produces byte-identical chunks (same IDs / same content SHA / same page ranges)
- Heading-aware — maintains a heading stack from ``#`` .. ``######`` (excluding
  fenced code blocks and Canonical frontmatter)
- Page-marker aware — ``<!-- page:N -->`` lines segment the body into pages but
  are never emitted as chunk content
- Conservative — no LLM / no tokenizer model / no tiktoken / no Markdown AST
  library; stdlib only
- Boundary priority — heading > paragraph > list/code structural > sentence >
  line > hard character limit (``CHUNK_MAX_CHARS``)

R3-A scope: chunker algorithm only. File I/O, SHA-256 integrity verification,
DB persistence, FTS indexing, and worker wiring are R3-B / R3-C / R3-D scope.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

# ============================================================================
# Constants — chunk size contract (directive §11)
# ============================================================================

#: Soft target. Packer tries to flush around this size at structural boundaries.
CHUNK_TARGET_CHARS: Final[int] = 1200

#: Hard upper bound. Any chunk that would exceed this is force-split.
CHUNK_MAX_CHARS: Final[int] = 1800

#: Overlap carried between force-splits of the *same* oversized section only.
#: Never carried across different heading sections.
CHUNK_OVERLAP_CHARS: Final[int] = 160

# ============================================================================
# Constants — frontmatter / page marker / heading patterns
# ============================================================================

#: Frontmatter delimiter. Canonical Markdown always starts with ``---``.
_FRONTMATTER_DELIM: Final[str] = "---"

#: Unescaped system page marker on its own line. Source-text collisions are
#: already escaped by R2-B Builder; only unescaped markers reach this regex.
_PAGE_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"^<!--\s*page:(\d+)\s*-->$")

#: ATX heading line. Groups: level (``#`` count), text.
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

#: Fenced code block delimiter (```` ``` ```` or ``~~~``).
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^(```|~~~)(.*)$")

#: Frontmatter field extractors. R2-B serialises scalar values via
#: ``json.dumps(value, ensure_ascii=False)`` so values are JSON-quoted.
#: R2-B ``_build_frontmatter`` emits ``key: value`` (colon-separated,
#: per canonical_markdown.py:448 ``f"{key}: {value}"``). The regex
#: accepts an optional colon (``:?\s+``) so it also matches the
#: colon-less ``key value`` form used by some test fixtures.
_FM_DOCUMENT_ID_RE: Final[re.Pattern[str]] = re.compile(
    r'^document_id:?\s+(.+?)\s*$', re.MULTILINE
)
_FM_PAGE_COUNT_RE: Final[re.Pattern[str]] = re.compile(
    r'^page_count:?\s+(\d+)\s*$', re.MULTILINE
)

#: Sentence boundary for hard-split fallback.
_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.。!?！？])\s+")


# ============================================================================
# Errors
# ============================================================================


class ChunkerError(Exception):
    """Base class for chunker errors. Messages are safe (no paths / SQL)."""


class CanonicalMarkdownInvalid(ChunkerError):
    """Frontmatter missing / malformed / field mismatch.

    safe_error_code: ``canonical_markdown_invalid``
    """


class ChunkingEmpty(ChunkerError):
    """Body has zero non-whitespace content; no chunks can be produced.

    safe_error_code: ``chunking_empty``
    """


class ChunkingFailed(ChunkerError):
    """Generic chunking failure (unexpected).

    safe_error_code: ``chunking_failed``
    """


# ============================================================================
# Dataclass — KnowledgeChunk (R3 chunk DTO)
# ============================================================================


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    """A single deterministic knowledge chunk.

    All fields are immutable. ``id`` is derived from
    ``(document_id, ordinal, content_sha256)`` so re-indexing the same
    Document always yields the same chunk IDs (directive §9).
    """

    id: str
    document_id: str
    ordinal: int
    heading_path: tuple[str, ...]
    content: str
    page_start: int
    page_end: int
    char_count: int
    content_sha256: str


# ============================================================================
# Internal dataclass — _Block (atomic content block with page range)
# ============================================================================


@dataclass(frozen=True, slots=True)
class _Block:
    """An atomic content block: a paragraph, list item, or code block.

    ``heading_path`` is captured at the moment the block was emitted so
    later packing does not need to re-walk the heading stack. ``page_start``
    and ``page_end`` support a block that spans pages (rare; only when the
    same paragraph straddles a page marker — current extractor does not
    actually do this, but the field exists for future-proofing).
    """

    heading_path: tuple[str, ...]
    text: str
    page_start: int
    page_end: int


# ============================================================================
# Deterministic chunk ID + content SHA
# ============================================================================


def compute_chunk_id(document_id: str, ordinal: int, content_sha256: str) -> str:
    """Return the deterministic chunk ID.

    Per directive §9 — same ``(document_id, ordinal, content_sha256)`` always
    yields the same ID. The ID is the lowercased hex SHA-256 of the
    NUL-separated tuple.
    """
    if ordinal < 0:
        raise ValueError(f"ordinal must be >= 0; got {ordinal}")
    payload_str = f"{document_id}\0{ordinal}\0{content_sha256}"
    payload = payload_str.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_content_sha256(content: str) -> str:
    """Return lowercased hex SHA-256 of ``content`` encoded as UTF-8."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ============================================================================
# Chunker
# ============================================================================


class HeadingAwareChunker:
    """Heading-aware deterministic chunker.

    Usage::

        chunker = HeadingAwareChunker()
        chunks = chunker.chunk(
            document_id="doc_abc123",
            markdown=text,
            expected_page_count=12,
        )

    Thread-safety: stateless; safe to share across coroutines / threads.
    No I/O, no DB, no network, no LLM.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(
        self,
        *,
        document_id: str,
        markdown: str,
        expected_page_count: int,
    ) -> list[KnowledgeChunk]:
        """Split Canonical Markdown into deterministic chunks.

        Raises:
            CanonicalMarkdownInvalid: frontmatter missing/malformed or
                ``document_id`` / ``page_count`` mismatch.
            ChunkingEmpty: body has zero non-whitespace content.
        """
        if not document_id:
            raise CanonicalMarkdownInvalid("document_id is required")
        if expected_page_count < 1:
            raise CanonicalMarkdownInvalid(
                f"expected_page_count must be >= 1; got {expected_page_count}"
            )
        if markdown is None:
            raise CanonicalMarkdownInvalid("markdown is None")

        frontmatter, body = self._split_frontmatter(markdown)
        self._verify_frontmatter(frontmatter, document_id, expected_page_count)

        page_segments = self._split_pages(body, expected_page_count)
        blocks = self._extract_blocks(page_segments)
        if not blocks:
            raise ChunkingEmpty(
                "Canonical Markdown body produced zero content blocks"
            )

        packed = self._pack_blocks(blocks)
        if not packed:
            raise ChunkingEmpty("packer produced zero chunks")

        return self._finalize(document_id, packed)

    # ------------------------------------------------------------------
    # Frontmatter
    # ------------------------------------------------------------------

    @staticmethod
    def _split_frontmatter(markdown: str) -> tuple[str, str]:
        """Return ``(frontmatter_block, body)``.

        Frontmatter is delimited by ``---`` on its own line at the very
        start of the document, followed by a closing ``---``.
        """
        text = markdown.lstrip("\ufeff")
        if not text.startswith(_FRONTMATTER_DELIM):
            raise CanonicalMarkdownInvalid(
                "Canonical Markdown must start with '---' frontmatter delimiter"
            )
        rest = text[len(_FRONTMATTER_DELIM) :]
        if not rest or rest[0] not in ("\n", "\r"):
            raise CanonicalMarkdownInvalid(
                "frontmatter opening '---' must be followed by a newline"
            )
        # Normalise to ``\n`` to simplify line walking.
        rest = rest.replace("\r\n", "\n").replace("\r", "\n")
        lines = rest.split("\n")
        close_idx = -1
        for idx, line in enumerate(lines):
            if line == _FRONTMATTER_DELIM:
                close_idx = idx
                break
        if close_idx == -1:
            raise CanonicalMarkdownInvalid(
                "frontmatter missing closing '---' delimiter"
            )
        frontmatter = "\n".join(lines[:close_idx])
        body = "\n".join(lines[close_idx + 1 :])
        return frontmatter, body

    @staticmethod
    def _verify_frontmatter(
        frontmatter: str,
        document_id: str,
        expected_page_count: int,
    ) -> None:
        did_match = _FM_DOCUMENT_ID_RE.search(frontmatter)
        if did_match is None:
            raise CanonicalMarkdownInvalid(
                "frontmatter missing 'document_id' field"
            )
        captured = did_match.group(1).strip()
        if captured.startswith('"') and captured.endswith('"'):
            captured = captured[1:-1]
        if captured != document_id:
            raise CanonicalMarkdownInvalid(
                "frontmatter document_id does not match caller-supplied id"
            )

        pc_match = _FM_PAGE_COUNT_RE.search(frontmatter)
        if pc_match is None:
            raise CanonicalMarkdownInvalid(
                "frontmatter missing 'page_count' field"
            )
        actual_pc = int(pc_match.group(1))
        if actual_pc != expected_page_count:
            raise CanonicalMarkdownInvalid(
                "frontmatter page_count does not match expected_page_count"
            )

    # ------------------------------------------------------------------
    # Page splitting
    # ------------------------------------------------------------------

    @staticmethod
    def _split_pages(body: str, expected_page_count: int) -> list[tuple[int, str]]:
        """Return ``[(page_number, page_body), ...]`` strictly ``1..N``.

        Raises ``CanonicalMarkdownInvalid`` on missing / duplicate / skipped
        / out-of-range markers.
        """
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        # Drop a single leading blank line so the first body content line
        # is the page-1 marker.
        body = body.lstrip("\n")

        lines = body.split("\n")
        pages: dict[int, list[str]] = {}
        current_page: int | None = None
        for line in lines:
            m = _PAGE_MARKER_RE.match(line)
            if m is not None:
                current_page = int(m.group(1))
                if current_page < 1 or current_page > expected_page_count:
                    raise CanonicalMarkdownInvalid(
                        f"page marker out of range: {current_page}"
                    )
                if current_page in pages:
                    raise CanonicalMarkdownInvalid(
                        f"duplicate page marker: {current_page}"
                    )
                pages[current_page] = []
                continue
            if current_page is None:
                if line.strip() == "":
                    continue
                raise CanonicalMarkdownInvalid(
                    "body content appears before first page marker"
                )
            pages[current_page].append(line)

        if not pages:
            raise CanonicalMarkdownInvalid("no page markers found in body")
        for n in range(1, expected_page_count + 1):
            if n not in pages:
                raise CanonicalMarkdownInvalid(f"missing page marker: page {n}")
        return [(n, "\n".join(pages[n])) for n in sorted(pages)]

    # ------------------------------------------------------------------
    # Block extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_blocks(
        page_segments: list[tuple[int, str]],
    ) -> list[_Block]:
        """Tokenise each page body into atomic content blocks.

        A block is a paragraph, list item, or fenced code block. Headings
        update the running stack but do not produce a content block
        themselves (heading text is captured in ``heading_path`` of the
        following blocks).
        """
        blocks: list[_Block] = []
        heading_stack: list[str] = []

        for page_number, page_body in page_segments:
            lines = page_body.split("\n")
            i = 0
            n = len(lines)
            buf: list[str] = []
            in_fence = False
            fence_marker = ""

            def flush(page: int = page_number) -> None:
                nonlocal buf
                if not buf:
                    return
                text = "\n".join(buf).strip()
                buf = []
                if text:
                    blocks.append(
                        _Block(
                            heading_path=tuple(heading_stack),
                            text=text,
                            page_start=page,
                            page_end=page,
                        )
                    )

            while i < n:
                line = lines[i]
                fm = _FENCE_RE.match(line)
                if fm is not None:
                    marker = fm.group(1)
                    if not in_fence:
                        in_fence = True
                        fence_marker = marker
                        flush()
                        buf.append(line)
                    elif marker == fence_marker:
                        in_fence = False
                        fence_marker = ""
                        buf.append(line)
                        flush()
                    else:
                        # Different fence marker inside an open fence —
                        # treat as plain code line.
                        buf.append(line)
                    i += 1
                    continue

                if in_fence:
                    buf.append(line)
                    i += 1
                    continue

                hm = _HEADING_RE.match(line)
                if hm is not None:
                    flush()
                    level = len(hm.group(1))
                    text = hm.group(2).strip()
                    del heading_stack[level - 1 :]
                    heading_stack.append(text)
                    i += 1
                    continue

                if line.strip() == "":
                    flush()
                    i += 1
                    continue

                buf.append(line)
                i += 1

            flush()

        return blocks

    # ------------------------------------------------------------------
    # Packing
    # ------------------------------------------------------------------

    @staticmethod
    def _pack_blocks(blocks: list[_Block]) -> list[_Block]:
        """Pack atomic blocks into chunk-sized outputs.

        Strategy:
        1. Walk blocks in order. Accumulate into the current chunk.
        2. Flush at the smaller of:
           (a) heading-section change — new ``heading_path`` differs in any
               element other than appending a deeper level;
           (b) accumulated size reached ``CHUNK_TARGET_CHARS`` and the next
               block would push past it.
        3. If a single block exceeds ``CHUNK_MAX_CHARS``, force-split it
           along paragraph / sentence / line / hard-char boundaries,
           carrying ``CHUNK_OVERLAP_CHARS`` between splits — but only
           within the same heading section.
        """
        # Intermediate (heading_path, text, page_min, page_max) tuples.
        intermediate: list[tuple[tuple[str, ...], str, int, int]] = []

        cur_text: list[str] = []
        cur_size = 0
        cur_h: tuple[str, ...] = ()
        cur_pmin: int | None = None
        cur_pmax: int | None = None

        def flush() -> None:
            nonlocal cur_text, cur_size, cur_pmin, cur_pmax
            if not cur_text:
                return
            joined = "\n\n".join(cur_text)
            assert cur_pmin is not None and cur_pmax is not None
            stripped = joined.strip()
            if stripped:
                intermediate.append((cur_h, stripped, cur_pmin, cur_pmax))
            cur_text = []
            cur_size = 0
            cur_pmin = None
            cur_pmax = None

        def heading_section_changed(
            old: tuple[str, ...], new: tuple[str, ...]
        ) -> bool:
            """Return True when ``new`` differs from ``old`` other than by
            appending a deeper level.

            e.g. ("A","B") -> ("A","B","C") is *not* a section change.
                 ("A","B") -> ("A","D")     *is* a section change.
            """
            if not old:
                return False
            if len(new) > len(old):
                return old != new[: len(old)]
            if len(new) == len(old):
                return old != new
            # New is shorter — section changed (popped a level).
            return True

        for blk in blocks:
            # Force-split oversized single block before accumulation.
            if len(blk.text) > CHUNK_MAX_CHARS:
                # Flush current accumulator first (preserves heading context).
                flush()
                for piece in _force_split(blk.text):
                    if piece.strip():
                        intermediate.append(
                            (blk.heading_path, piece.strip(), blk.page_start, blk.page_end)
                        )
                # Continue with a fresh accumulator.
                cur_h = blk.heading_path
                continue

            # Heading-section change → flush.
            if cur_text and heading_section_changed(cur_h, blk.heading_path):
                flush()

            cur_h = blk.heading_path

            addition = len(blk.text) + (2 if cur_text else 0)
            if (
                cur_size + addition > CHUNK_TARGET_CHARS
                and cur_size > 0
                and cur_text
            ):
                flush()

            cur_text.append(blk.text)
            cur_size += addition
            if cur_pmin is None:
                cur_pmin = blk.page_start
                cur_pmax = blk.page_end
            else:
                assert cur_pmax is not None
                cur_pmin = min(cur_pmin, blk.page_start)
                cur_pmax = max(cur_pmax, blk.page_end)

        flush()

        return [
            _Block(heading_path=h, text=t, page_start=pmin, page_end=pmax)
            for (h, t, pmin, pmax) in intermediate
        ]

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    @staticmethod
    def _finalize(document_id: str, packed: list[_Block]) -> list[KnowledgeChunk]:
        out: list[KnowledgeChunk] = []
        for ordinal, blk in enumerate(packed):
            content = blk.text
            sha = compute_content_sha256(content)
            cid = compute_chunk_id(document_id, ordinal, sha)
            out.append(
                KnowledgeChunk(
                    id=cid,
                    document_id=document_id,
                    ordinal=ordinal,
                    heading_path=blk.heading_path,
                    content=content,
                    page_start=blk.page_start,
                    page_end=blk.page_end,
                    char_count=len(content),
                    content_sha256=sha,
                )
            )
        return out


# ============================================================================
# Force-split helpers
# ============================================================================


def _force_split(text: str) -> list[str]:
    """Split a single oversized block into ``<= CHUNK_MAX_CHARS`` pieces.

    Strategy (directive §15):
      1. paragraph boundaries (``\\n\\n``)
      2. line boundaries (``\\n``)
      3. sentence boundaries (``. `` / ``。``)
      4. hard character cut

    Overlap of ``CHUNK_OVERLAP_CHARS`` is carried between adjacent pieces
    of the same oversized block only.
    """
    pieces: list[str] = []
    paragraphs = re.split(r"\n\s*\n", text)
    buf: list[str] = []
    buf_size = 0
    for para in paragraphs:
        if len(para) > CHUNK_MAX_CHARS:
            # Flush buffer.
            if buf:
                pieces.append("\n\n".join(buf))
                buf = []
                buf_size = 0
            # Line-split the oversized paragraph.
            for piece in _split_by_lines(para):
                pieces.append(piece)
            continue
        addition = len(para) + (2 if buf else 0)
        if buf_size + addition > CHUNK_MAX_CHARS and buf:
            pieces.append("\n\n".join(buf))
            tail = pieces[-1][-CHUNK_OVERLAP_CHARS :]
            buf = [tail] if tail else []
            buf_size = len(tail)
        buf.append(para)
        buf_size += addition
    if buf:
        pieces.append("\n\n".join(buf))

    final: list[str] = []
    for piece in pieces:
        if len(piece) <= CHUNK_MAX_CHARS:
            final.append(piece)
            continue
        for sub in _hard_split(piece):
            final.append(sub)
    return final


def _split_by_lines(text: str) -> list[str]:
    """Split an oversized paragraph by line boundaries."""
    pieces: list[str] = []
    lines = text.split("\n")
    buf: list[str] = []
    buf_size = 0
    for line in lines:
        if len(line) > CHUNK_MAX_CHARS:
            if buf:
                pieces.append("\n".join(buf))
                buf = []
                buf_size = 0
            for sub in _hard_split(line):
                pieces.append(sub)
            continue
        addition = len(line) + (1 if buf else 0)
        if buf_size + addition > CHUNK_MAX_CHARS and buf:
            pieces.append("\n".join(buf))
            tail = pieces[-1][-CHUNK_OVERLAP_CHARS :]
            buf = [tail] if tail else []
            buf_size = len(tail)
        buf.append(line)
        buf_size += addition
    if buf:
        pieces.append("\n".join(buf))
    return pieces


def _hard_split(text: str) -> list[str]:
    """Last-resort split: sentence boundaries then hard char cut."""
    if len(text) <= CHUNK_MAX_CHARS:
        return [text]
    sentences = _SENTENCE_SPLIT_RE.split(text)
    pieces: list[str] = []
    buf = ""
    for sent in sentences:
        if len(sent) > CHUNK_MAX_CHARS:
            if buf:
                pieces.append(buf)
                buf = ""
            for i in range(0, len(sent), CHUNK_MAX_CHARS):
                pieces.append(sent[i : i + CHUNK_MAX_CHARS])
            continue
        if len(buf) + len(sent) + 1 > CHUNK_MAX_CHARS and buf:
            pieces.append(buf)
            buf = sent
        else:
            buf = (buf + " " + sent).strip() if buf else sent
    if buf:
        pieces.append(buf)
    return pieces
