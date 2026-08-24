"""Deterministic and zero-network HTML parser behavior."""

from __future__ import annotations

import base64

import pytest

from pi_agent_core_py.web.wiki import WikiStoreError
from pi_agent_core_py.web.wiki.html_parser import HtmlParserLimits, parse_single_html


def test_html_parser_removes_active_content_and_external_resources() -> None:
    result = parse_single_html(
        b"""
        <!doctype html>
        <html><body>
          <h1>Safe title</h1>
          <script>fetch('https://attacker.invalid')</script>
          <style>body { background: url(https://attacker.invalid/x) }</style>
          <iframe src="https://attacker.invalid"><p>hidden</p></iframe>
          <p onclick="steal()">Readable <a href="javascript:steal()">link</a>.</p>
          <img src="https://attacker.invalid/tracker.png" alt="tracker">
        </body></html>
        """
    )

    assert "# Safe title" in result.markdown
    assert "Readable link\\." in result.markdown
    assert "attacker" not in result.markdown
    assert "javascript" not in result.markdown
    assert result.images == ()
    assert result.warnings == ("external_image_removed", "unsafe_html_removed")


def test_html_parser_extracts_only_bounded_data_images() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"image"
    encoded = base64.b64encode(png).decode("ascii")
    result = parse_single_html(
        f'<p>Before</p><img src="data:image/png;base64,{encoded}" alt="diagram">'
        '<img src="data:image/svg+xml;base64,PHN2Zz4=" alt="svg">'.encode()
    )

    assert len(result.images) == 1
    image = result.images[0]
    assert image.content == png
    assert image.mime_type == "image/png"
    assert f"![diagram]({image.path})" in result.markdown
    assert "external_image_removed" in result.warnings


def test_html_source_text_cannot_inject_markdown_image_or_link() -> None:
    result = parse_single_html(
        b"<p>![tracking](https://attacker.invalid/x) [run](javascript:bad)</p>"
    )
    assert "!\\[tracking\\]\\(https://attacker\\.invalid/x\\)" in result.markdown
    assert "\\[run\\]\\(javascript:bad\\)" in result.markdown


def test_html_parser_rejects_non_utf8_and_source_limit() -> None:
    with pytest.raises(WikiStoreError) as encoding_error:
        parse_single_html(b"\xff\xfe")
    assert encoding_error.value.code == "invalid_source"

    with pytest.raises(WikiStoreError) as size_error:
        parse_single_html(b"1234", limits=HtmlParserLimits(max_source_bytes=3))
    assert size_error.value.code == "file_too_large"


def test_html_parser_deduplicates_repeated_embedded_image() -> None:
    jpeg = b"\xff\xd8\xffsame\xff\xd9"
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    result = parse_single_html(f'<img src="{uri}"><img src="{uri}">'.encode())
    assert len(result.images) == 1
    assert result.markdown.count("images/") == 2
