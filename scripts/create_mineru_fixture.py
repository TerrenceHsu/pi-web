"""Generate a public synthetic PDF fixture; uses optional authoring dependencies only."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pdf = Canvas(str(args.output), pagesize=(612, 792), invariant=1)
    pdf.setTitle("Synthetic MinerU engineering acceptance fixture")
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(48, 736, "Synthetic engineering report")
    pdf.setFont("Helvetica", 11)
    lines = [
        "Public synthetic test data. No customer information is included.",
        "Page 1 tests native text, a table and exact numerical extraction.",
        "The workflow must preserve page order and source attribution.",
        "Approved reports remain separate from runtime execution permission.",
        "A parser failure must not silently substitute another parsing profile.",
        "Revenue for North, Central and South is 101, 202 and 303 respectively.",
        "The total revenue is 606 units. The reporting period is September 2026.",
    ]
    for index, line in enumerate(lines):
        pdf.drawString(48, 696 - index * 22, line)
    top = 490
    for row, values in enumerate([
        ("Region", "Revenue", "Status"), ("North", "101", "Reviewed"),
        ("Central", "202", "Reviewed"), ("South", "303", "Reviewed"),
        ("Total", "606", "Verified"),
    ]):
        pdf.rect(48, top - row * 34, 510, 34)
        for x, value in zip((60, 238, 410), values, strict=True):
            pdf.drawString(x, top - row * 34 + 12, value)
    pdf.drawString(48, 248, "Expected total: 606. Expected row count: 3 regions.")
    pdf.drawString(
        48, 64, "Synthetic corpus - not a general document-quality benchmark. Page 1 / 2",
    )
    pdf.showPage()
    scan = Image.new("RGB", (1200, 1550), "white")
    draw = ImageDraw.Draw(scan)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path) if font_path.exists() else "DejaVuSans.ttf", 28)
    scan_lines = [
        "SCANNED OPERATIONS MEMO", "Public synthetic OCR acceptance page.",
        "Document ID: SYNTHETIC-2026-001", "This page contains image pixels, not PDF text.",
        "The approval code is ORANGE-741.", "The scheduled inspection date is 2026-09-06.",
        "Three workspaces use independent execution copies.",
        "Revoking one workspace does not cancel its neighbour.",
        "Data is written back only after explicit publication approval.",
        "The recorded total from page one is 606 units.",
        "A completed parser run must preserve this source page.",
        "End of synthetic test memo. Page 2 of 2.",
    ]
    for index, line in enumerate(scan_lines):
        draw.text((70, 100 + index * 76), line, fill="black", font=font)
    pdf.drawImage(ImageReader(scan), 0, 0, width=612, height=792)
    pdf.save()


if __name__ == "__main__":
    main()
