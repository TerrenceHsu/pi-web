"""Materialize the exact offline Docling/RapidOCR model tree for OCI builds.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import cast

_PREFIXES = {
    "docling-layout-heron": "docling-project--docling-layout-heron",
    "tableformer-accurate": "docling-project--docling-models",
    "rapidocr-ppocrv6-multilingual-onnx": "RapidOcr",
}


def _download(url: str, destination: Path, expected_size: int, expected_sha: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "pi-wiki-parser-build/0.0.29"})
    digest = hashlib.sha256()
    size = 0
    temporary = destination.with_name(f".{destination.name}.download")
    try:
        with (
            urllib.request.urlopen(request, timeout=300) as response,
            temporary.open("xb") as output,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected_size:
                    raise ValueError("model download exceeds pinned size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != expected_size or digest.hexdigest() != expected_sha:
            raise ValueError("model download does not match pinned identity")
        os.replace(temporary, destination)
        os.chmod(destination, 0o444)
    finally:
        temporary.unlink(missing_ok=True)


def _rapidocr_source(filename: str) -> Path:
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or not spec.submodule_search_locations:
        raise ValueError("RapidOCR package is unavailable")
    root = Path(next(iter(spec.submodule_search_locations))).resolve(strict=True)
    source = root / "models" / filename
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise ValueError("RapidOCR model is not a regular package file")
    return source


def materialize(manifest_path: Path, output_root: Path) -> None:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    models = payload.get("models")
    if not isinstance(models, list) or len(models) != 3:
        raise ValueError("runtime manifest model list is invalid")
    output_root.mkdir(mode=0o755, parents=True, exist_ok=False)
    for raw_model in models:
        if not isinstance(raw_model, dict):
            raise ValueError("runtime manifest model entry is invalid")
        model = cast(dict[str, object], raw_model)
        name = model.get("name")
        revision = model.get("revision")
        repository = model.get("repository")
        files = model.get("files")
        if not isinstance(name, str) or name not in _PREFIXES or not isinstance(files, list):
            raise ValueError("runtime manifest model identity is invalid")
        model_root = output_root / _PREFIXES[name]
        for raw_file in files:
            if not isinstance(raw_file, dict):
                raise ValueError("runtime manifest model file is invalid")
            item = cast(dict[str, object], raw_file)
            relative = item.get("path")
            size = item.get("size_bytes")
            sha256 = item.get("sha256")
            if (
                not isinstance(relative, str)
                or not isinstance(size, int)
                or not isinstance(sha256, str)
            ):
                raise ValueError("runtime manifest model file identity is invalid")
            if name == "rapidocr-ppocrv6-multilingual-onnx":
                destination = model_root / PurePosixPath(relative).name
                destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                source = _rapidocr_source(destination.name)
                shutil.copyfile(source, destination)
                if destination.stat().st_size != size or hashlib.sha256(
                    destination.read_bytes()
                ).hexdigest() != sha256:
                    raise ValueError("RapidOCR package model does not match runtime manifest")
                os.chmod(destination, 0o444)
                continue
            if not isinstance(repository, str) or not isinstance(revision, str):
                raise ValueError("downloaded model lacks immutable repository identity")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or str(pure) != relative:
                raise ValueError("model path is unsafe")
            destination = model_root.joinpath(*pure.parts)
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            quoted_path = "/".join(
                urllib.parse.quote(part, safe="") for part in pure.parts
            )
            url = f"https://huggingface.co/{repository}/resolve/{revision}/{quoted_path}"
            _download(url, destination, size, sha256)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    materialize(arguments.manifest, arguments.output)


if __name__ == "__main__":
    main()
