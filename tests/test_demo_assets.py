"""Public demo fixtures are synthetic, deterministic and self-contained."""

from __future__ import annotations

import csv
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"


def test_sales_demo_matches_bilingual_walkthrough() -> None:
    with (DEMO / "sales.csv").open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    regions: dict[str, int] = defaultdict(int)
    months: dict[str, int] = defaultdict(int)
    for row in rows:
        regions[row["region"]] += int(row["sales"])
        months[row["date"][:7]] += int(row["sales"])
    assert len(rows) == 6
    assert dict(regions) == {"North": 2600, "South": 3400}
    assert dict(months) == {"2026-01": 2700, "2026-02": 3300}
    assert sum(regions.values()) == 6000


def test_wiki_demo_has_no_scripts_or_remote_resources() -> None:
    class SourceParser(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            assert tag not in {"script", "iframe", "object", "embed", "form", "link"}
            for name, value in attrs:
                assert not name.lower().startswith("on")
                assert name.lower() not in {"src", "srcset", "href", "action", "style"}
                assert value is None or "https://" not in value

    SourceParser().feed((DEMO / "wiki-source.html").read_text(encoding="utf-8"))


def test_demo_notes_explicitly_identify_synthetic_data() -> None:
    notes = (DEMO / "project-notes.md").read_text(encoding="utf-8")
    assert "fictional" in notes
    assert "虚构" in notes
    assert "2600" in notes and "3400" in notes and "6000" in notes
