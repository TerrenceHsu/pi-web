"""Helpers to generate minimal deterministic PDF fixtures for R2-A tests.

Per P2-R2-A §20:

- Tests must be fully offline; no network downloads, no external fixture URLs
- No reportlab or other PDF generation deps
- Use pypdf itself (PdfWriter) to construct tiny deterministic PDFs
- All fixtures are project-generated; no copyrighted / third-party content
- Total fixture footprint kept minimal

This module is import-safe: importing it does NOT import pypdf at module load
(pypdf import is deferred into the helper functions, matching the adapter's
lazy-import pattern).
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path


def write_text_pdf(
    path: Path,
    *,
    pages: Iterable[str],
    metadata: dict[str, str] | None = None,
) -> None:
    """Create a multi-page text PDF at ``path``.

    Each entry in ``pages`` becomes a separate page containing that text.
    Metadata dict keys must be pypdf metadata keys (e.g. ``/Title``).
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    pages_list = list(pages)
    for body in pages_list:
        # PdfWriter.add_blank_page then inject text via low-level content stream
        page = writer.add_blank_page(width=612, height=792)  # US Letter
        if body:
            _inject_text_into_page(page, body)

    if metadata:
        # writer.add_metadata expects a plain dict; pypdf converts internally
        safe_meta = {
            (k if k.startswith("/") else f"/{k}"): v
            for k, v in metadata.items()
        }
        try:
            writer.add_metadata(safe_meta)
        except Exception:
            pass  # don't let metadata failure break fixture creation

    with open(path, "wb") as f:
        writer.write(f)


def write_blank_pdf(path: Path, *, page_count: int = 1) -> None:
    """Create a PDF with N blank pages (no text). Used for scan-only / empty
    extraction tests."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as f:
        writer.write(f)


def write_encrypted_pdf(
    path: Path,
    *,
    pages: Iterable[str],
    user_password: str,
) -> None:
    """Create an encrypted PDF (user-password protected).

    Per R2-A §18 the adapter refuses any encrypted PDF; this fixture lets
    tests verify the refusal path.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for body in list(pages):
        page = writer.add_blank_page(width=612, height=792)
        if body:
            _inject_text_into_page(page, body)

    writer.encrypt(user_password=user_password, owner_password=user_password)
    with open(path, "wb") as f:
        writer.write(f)


def write_corrupted_pdf(path: Path) -> None:
    """Write bytes that look like a PDF header but are truncated / invalid."""
    path.write_bytes(b"%PDF-1.4\n%binary garbage not a real pdf body\n%%EOF")


def file_sha256(path: Path) -> str:
    """SHA-256 hex digest of file content. Used to verify adapter does not
    modify source PDFs (P2-R2-A §29 'source file integrity')."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ============================================================================
# Internal — low-level text injection (pypdf 6.x has no high-level add-text)
# ============================================================================


def _inject_text_into_page(page: object, text: str) -> None:
    """Append a content stream to ``page`` drawing ``text`` at fixed coords.

    Uses pypdf's low-level generic objects. Text is escaped minimally
    (parens / backslashes). Returns no value; mutation is in-place on ``page``.
    """
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )

    # PDF string literal escape: \, (, ) must be backslash-escaped
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    safe = safe.replace("\n", " ").replace("\r", " ")

    stream_text = (
        f"BT\n/F1 12 Tf\n72 700 Td\n({safe}) Tj\nET\n"
    ).encode("latin-1", errors="replace")

    stream_obj = DecodedStreamObject()
    stream_obj.set_data(stream_text)

    page_obj = page  # type: ignore[assignment]
    page_obj[NameObject("/Contents")] = stream_obj  # type: ignore[index]

    # Provide /Resources /Font entry so extract_text() returns the text
    resources = page_obj.get("/Resources")  # type: ignore[attr-defined]
    if resources is None:
        resources = DictionaryObject()
        page_obj[NameObject("/Resources")] = resources  # type: ignore[index]
    fonts = resources.get("/Font")  # type: ignore[attr-defined]
    if fonts is None:
        fonts = DictionaryObject()
        resources[NameObject("/Font")] = fonts  # type: ignore[index]
    if "/F1" not in fonts:  # type: ignore[operator]
        helv = DictionaryObject()
        helv[NameObject("/Type")] = NameObject("/Font")
        helv[NameObject("/Subtype")] = NameObject("/Type1")
        helv[NameObject("/BaseFont")] = NameObject("/Helvetica")
        fonts[NameObject("/F1")] = helv  # type: ignore[index]


__all__ = [
    "file_sha256",
    "write_blank_pdf",
    "write_corrupted_pdf",
    "write_encrypted_pdf",
    "write_text_pdf",
]
