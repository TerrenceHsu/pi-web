"""Read bounded Chromium image headers, without decoding pixels or adding an SDK."""

from __future__ import annotations

import struct


def image_size(data: bytes, mime: str) -> tuple[int, int] | None:
    if mime == "image/png":
        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            width, height = struct.unpack("!II", data[16:24])
            return width, height
        return None
    if mime != "image/jpeg" or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    # SOF precedes pixel data in Chromium JPEGs. Never scan the encoded image body.
    limit = min(len(data), 65536)
    while offset + 4 <= limit:
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (0xDA, 0xD9):
            return None
        length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if length < 2 or offset + 2 + length > limit:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if length < 8:
                return None
            height, width = struct.unpack("!HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + length
    return None


def matches_viewport(data: bytes, mime: str, width: int, height: int, dpr: float) -> bool:
    size = image_size(data, mime)
    # CDP screencast uses CSS pixels; still captures use physical pixels. Allow
    # one physical pixel of rounding for the fractional DPR pixel-budget clamp.
    scale = dpr if mime == "image/png" else 1.0
    return size is not None and all(
        abs(actual - expected * scale) <= (1 if mime == "image/png" else 0)
        for actual, expected in zip(size, (width, height), strict=True)
    )
