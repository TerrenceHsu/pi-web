"""Materialize hash-pinned AGPL dependency source archives for OCI builds.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import cast

_EXPECTED_AGPL_PACKAGES = frozenset({"pymupdf", "pymupdf-layout", "pymupdf4llm"})
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _download(url: str, destination: Path, expected_sha256: str) -> int:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("source archive URL must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "pi-wiki-parser-source-build/0.0.28"},
    )
    digest = hashlib.sha256()
    size = 0
    temporary = destination.with_name(f".{destination.name}.download")
    try:
        with (
            urllib.request.urlopen(request, timeout=300) as response,
            temporary.open("xb") as output,
        ):
            final_url = urllib.parse.urlsplit(response.geturl())
            if final_url.scheme != "https":
                raise ValueError("source archive redirected away from HTTPS")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_ARCHIVE_BYTES:
                    raise ValueError("source archive exceeds the build limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0 or digest.hexdigest() != expected_sha256:
            raise ValueError("source archive does not match pinned identity")
        os.replace(temporary, destination)
        os.chmod(destination, 0o444)
        return size
    finally:
        temporary.unlink(missing_ok=True)


def materialize(manifest_path: Path, output_root: Path) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_packages = payload.get("packages")
    if not isinstance(raw_packages, list):
        raise ValueError("runtime manifest package list is invalid")
    selected: dict[str, dict[str, object]] = {}
    for raw_package in raw_packages:
        if not isinstance(raw_package, dict):
            raise ValueError("runtime manifest package entry is invalid")
        package = cast(dict[str, object], raw_package)
        if package.get("selected_license") != "AGPL-3.0-only":
            continue
        name = package.get("name")
        if not isinstance(name, str) or name in selected:
            raise ValueError("AGPL source package identity is invalid")
        selected[name] = package
    if set(selected) != _EXPECTED_AGPL_PACKAGES:
        raise ValueError("runtime manifest AGPL source set is incomplete")

    output_root.mkdir(mode=0o755, parents=True, exist_ok=False)
    evidence: list[dict[str, object]] = []
    for name in sorted(selected):
        package = selected[name]
        version = package.get("version")
        raw_source = package.get("source")
        if not isinstance(version, str) or not isinstance(raw_source, dict):
            raise ValueError("AGPL source metadata is incomplete")
        source = cast(dict[str, object], raw_source)
        url = source.get("url")
        archive_sha256 = source.get("archive_sha256")
        if not isinstance(url, str) or not isinstance(archive_sha256, str):
            raise ValueError("AGPL source archive identity is incomplete")
        if len(archive_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in archive_sha256
        ):
            raise ValueError("AGPL source archive SHA-256 is invalid")
        url_path = urllib.parse.urlsplit(url).path
        if not (url_path.endswith(".tar.gz") or "/tar.gz/" in url_path):
            raise ValueError("AGPL source archive format is not supported")
        filename = f"{name}-{version}-source.tar.gz"
        size = _download(url, output_root / filename, archive_sha256)
        evidence.append(
            {
                "filename": filename,
                "name": name,
                "sha256": archive_sha256,
                "size_bytes": size,
                "url": url,
                "version": version,
            }
        )
    manifest = {
        "archives": evidence,
        "schema": "pi-wiki-parser-upstream-sources/v1",
    }
    manifest_bytes = _canonical_json(manifest)
    manifest_path_out = output_root / "manifest.json"
    manifest_path_out.write_bytes(manifest_bytes)
    os.chmod(manifest_path_out, 0o444)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    materialize(arguments.manifest, arguments.output)


if __name__ == "__main__":
    main()
