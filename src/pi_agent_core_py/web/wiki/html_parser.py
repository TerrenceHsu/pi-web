"""Deterministic single-file HTML-to-Markdown parser with zero network I/O."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .errors import WikiStoreError

HTML_PARSER_VERSION = "builtin-html-1"

_BLOCKED_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "form",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "footer",
        "header",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "tr",
    }
)
_ALLOWED_IMAGE_MIME = frozenset({"image/png", "image/jpeg", "image/webp"})
_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})$",
    re.IGNORECASE,
)
_MARKDOWN_META_RE = re.compile(r"([\\`*_{}\[\]()#+.!|<>-])")


@dataclass(frozen=True, slots=True)
class HtmlParserLimits:
    max_source_bytes: int = 25 * 1024 * 1024
    max_markdown_bytes: int = 50 * 1024 * 1024
    max_image_count: int = 128
    max_image_bytes: int = 8 * 1024 * 1024
    max_total_image_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.max_source_bytes,
            self.max_markdown_bytes,
            self.max_image_count,
            self.max_image_bytes,
            self.max_total_image_bytes,
        )
        if any(value < 0 for value in values) or self.max_source_bytes == 0:
            raise ValueError("HTML parser limits must be non-negative")


@dataclass(frozen=True, slots=True)
class HtmlEmbeddedImage:
    path: str
    mime_type: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class HtmlParseResult:
    markdown: str
    images: tuple[HtmlEmbeddedImage, ...]
    warnings: tuple[str, ...]


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_META_RE.sub(r"\\\1", value)


def _safe_link(value: str) -> str | None:
    candidate = value.strip()
    if not candidate or any(ord(char) < 32 for char in candidate):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"", "http", "https", "mailto"}:
        return None
    if parsed.scheme == "" and candidate.startswith("//"):
        return None
    replacements = {
        "\\": "%5C",
        " ": "%20",
        '"': "%22",
        "'": "%27",
        "<": "%3C",
        ">": "%3E",
        "(": "%28",
        ")": "%29",
    }
    for raw, encoded in replacements.items():
        candidate = candidate.replace(raw, encoded)
    return candidate


def _validate_image_magic(mime_type: str, payload: bytes) -> bool:
    if mime_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff") and payload.endswith(b"\xff\xd9")
    if mime_type == "image/webp":
        return len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    return False


class _MarkdownHTMLParser(HTMLParser):
    def __init__(self, limits: HtmlParserLimits) -> None:
        super().__init__(convert_charrefs=True)
        self._limits = limits
        self._parts: list[str] = []
        self._blocked_depth = 0
        self._blocked_stack: list[str] = []
        self._links: list[str | None] = []
        self._pre_depth = 0
        self._list_stack: list[tuple[str, int]] = []
        self._images: dict[str, HtmlEmbeddedImage] = {}
        self._total_image_bytes = 0
        self._warnings: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        name = tag.casefold()
        if self._blocked_depth:
            if name in _BLOCKED_TAGS:
                self._blocked_depth += 1
                self._blocked_stack.append(name)
            return
        if name in _BLOCKED_TAGS:
            self._blocked_depth = 1
            self._blocked_stack.append(name)
            self._warnings.add("unsafe_html_removed")
            return
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if name in _BLOCK_TAGS:
            self._break()
            if name == "blockquote":
                self._parts.append("> ")
        elif name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._break()
            self._parts.append("#" * int(name[1]) + " ")
        elif name == "br":
            self._break()
        elif name in {"ul", "ol"}:
            self._list_stack.append((name, 0))
            self._break()
        elif name == "li":
            self._break()
            depth = max(len(self._list_stack) - 1, 0)
            marker = "- "
            if self._list_stack and self._list_stack[-1][0] == "ol":
                list_name, ordinal = self._list_stack[-1]
                ordinal += 1
                self._list_stack[-1] = (list_name, ordinal)
                marker = f"{ordinal}. "
            self._parts.append("  " * depth + marker)
        elif name == "pre":
            self._pre_depth += 1
            self._break()
        elif name == "a":
            href = _safe_link(attributes.get("href", ""))
            self._links.append(href)
            if href is not None:
                self._parts.append("[")
        elif name == "img":
            self._handle_image(attributes)
        elif name in {"td", "th"}:
            if self._parts and not self._parts[-1].endswith(("\n", " | ")):
                self._parts.append(" | ")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if self._blocked_depth:
            if self._blocked_stack and name == self._blocked_stack[-1]:
                self._blocked_stack.pop()
                self._blocked_depth -= 1
            return
        if name == "a":
            href = self._links.pop() if self._links else None
            if href is not None:
                self._parts.append(f"]({href})")
        elif name in {"ul", "ol"}:
            if self._list_stack:
                self._list_stack.pop()
            self._break()
        elif name == "pre":
            self._pre_depth = max(self._pre_depth - 1, 0)
            self._break()
        elif name in _BLOCK_TAGS or name in {
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
        }:
            self._break()

    def handle_data(self, data: str) -> None:
        if self._blocked_depth or not data:
            return
        if self._pre_depth:
            lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            self._parts.append("\n".join(f"    {line}" for line in lines))
            return
        normalized = re.sub(r"\s+", " ", data)
        if normalized.strip():
            self._parts.append(_escape_markdown(normalized))

    def result(self) -> HtmlParseResult:
        raw = "".join(self._parts).replace("\r", "")
        lines = [line.rstrip() for line in raw.split("\n")]
        output: list[str] = []
        blank = False
        for line in lines:
            cleaned = line.strip() if not line.startswith("    ") else line.rstrip()
            if not cleaned:
                if output and not blank:
                    output.append("")
                blank = True
                continue
            output.append(cleaned)
            blank = False
        markdown = "\n".join(output).strip() + "\n"
        if markdown == "\n":
            markdown = "_No readable content._\n"
            self._warnings.add("empty_readable_content")
        if len(markdown.encode("utf-8")) > self._limits.max_markdown_bytes:
            raise WikiStoreError("file_too_large")
        return HtmlParseResult(
            markdown=markdown,
            images=tuple(self._images.values()),
            warnings=tuple(sorted(self._warnings)),
        )

    def _break(self) -> None:
        if not self._parts or not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def _handle_image(self, attrs: dict[str, str]) -> None:
        source = attrs.get("src", "")
        match = _DATA_IMAGE_RE.fullmatch(source)
        if match is None:
            self._warnings.add("external_image_removed")
            return
        mime_type = match.group(1).casefold()
        if mime_type not in _ALLOWED_IMAGE_MIME:
            self._warnings.add("unsafe_image_removed")
            return
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            self._warnings.add("invalid_data_image_removed")
            return
        if (
            not payload
            or len(payload) > self._limits.max_image_bytes
            or not _validate_image_magic(mime_type, payload)
        ):
            self._warnings.add("invalid_data_image_removed")
            return
        digest = hashlib.sha256(payload).hexdigest()
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime_type]
        path = f"images/img_{digest[:24]}{suffix}"
        if path not in self._images:
            if len(self._images) >= self._limits.max_image_count:
                self._warnings.add("image_count_limit_reached")
                return
            if self._total_image_bytes + len(payload) > self._limits.max_total_image_bytes:
                self._warnings.add("image_bytes_limit_reached")
                return
            self._images[path] = HtmlEmbeddedImage(
                path=path,
                mime_type=mime_type,
                content=payload,
                sha256=digest,
            )
            self._total_image_bytes += len(payload)
        alt = _escape_markdown(attrs.get("alt", "image").strip() or "image")
        self._parts.append(f"![{alt}]({path})")


def parse_single_html(
    content: bytes,
    *,
    limits: HtmlParserLimits | None = None,
) -> HtmlParseResult:
    """Parse only supplied bytes; this function has no transport or filesystem API."""
    effective_limits = limits or HtmlParserLimits()
    if not content or len(content) > effective_limits.max_source_bytes:
        raise WikiStoreError("file_too_large")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise WikiStoreError("invalid_source") from exc
    parser = _MarkdownHTMLParser(effective_limits)
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise WikiStoreError("invalid_source") from exc
    return parser.result()


__all__ = [
    "HTML_PARSER_VERSION",
    "HtmlEmbeddedImage",
    "HtmlParseResult",
    "HtmlParserLimits",
    "parse_single_html",
]
