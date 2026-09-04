"""Run representative PDFs through the persistent, network-free OCI Worker.

The Worker must already be running with the same absolute exchange directory.
This script prints only identities, parser decisions, sizes, hashes, and timings;
it never prints document content or absolute input paths.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from wiki_parser import (
    ParserArtifactManifestV2,
    ParserJobSpecV2,
    ParserLimits,
    ParserRoutingConfigIdentity,
    ParserSourceSpec,
    PersistentOciParserProvider,
)

_WORKER_SOURCE_ROOT = (
    Path(__file__).resolve().parents[1] / "workers" / "wiki_parser_worker"
).resolve()


@dataclass(frozen=True, slots=True)
class SmokeCase:
    name: str
    mode: Literal["pipeline", "gpu-medium", "gpu-high"]
    source_path: Path


def _canonical_sha256(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_cases(values: list[list[str]]) -> tuple[SmokeCase, ...]:
    result: list[SmokeCase] = []
    names: set[str] = set()
    for name, mode, source in values:
        if (
            re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?",
                name,
            )
            is None
        ):
            raise ValueError(f"invalid case name: {name!r}")
        if name in names:
            raise ValueError(f"duplicate case name: {name}")
        if mode not in {"pipeline", "gpu-medium", "gpu-high"}:
            raise ValueError(f"invalid mode for {name}: {mode}")
        path = Path(source).resolve(strict=True)
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            raise ValueError(f"case {name} is not a regular .pdf file")
        names.add(name)
        result.append(
            SmokeCase(
                name=name,
                mode=cast(Literal["pipeline", "gpu-medium", "gpu-high"], mode),
                source_path=path,
            )
        )
    return tuple(result)


def _reject_worker_source_runtime_path(path: Path, *, purpose: str) -> Path:
    """Keep mutable smoke state out of the exact Corresponding Source tree."""

    resolved = path.resolve()
    if resolved == _WORKER_SOURCE_ROOT or _WORKER_SOURCE_ROOT in resolved.parents:
        raise ValueError(f"{purpose} must be outside the Worker source tree")
    return resolved


def _manifest_from_artifact(path: Path) -> ParserArtifactManifestV2:
    with tarfile.open(path, mode="r:") as archive:
        member = archive.getmember("manifest.json")
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError("artifact manifest is unavailable")
        return ParserArtifactManifestV2.model_validate_json(stream.read())


async def _run_case(
    *,
    case: SmokeCase,
    provider: PersistentOciParserProvider,
    routing: ParserRoutingConfigIdentity,
    output_root: Path,
    timeout_seconds: int,
) -> None:
    source_size = case.source_path.stat().st_size
    source_sha256 = hashlib.sha256(case.source_path.read_bytes()).hexdigest()
    suffix = uuid4().hex
    spec = ParserJobSpecV2(
        job_id=f"smoke-{case.name}-{suffix}",
        source=ParserSourceSpec(
            source_id=f"source-{case.name}-{suffix}",
            display_name=f"{case.name}.pdf",
            size_bytes=source_size,
            sha256=source_sha256,
        ),
        requested_mode=case.mode,
        routing_config=routing,
        limits=ParserLimits(timeout_seconds=timeout_seconds),
    )
    handle = await provider.create_job(spec, source_path=case.source_path)
    try:
        async with asyncio.timeout(timeout_seconds + 60):
            status = await provider.wait(handle)
        if status.state != "succeeded":
            raise RuntimeError(
                f"case {case.name} failed with safe error {status.safe_error_code}"
            )
        attempt_parsers = [attempt.parser for attempt in status.attempts]
        attempt_states = [attempt.state for attempt in status.attempts]
        if attempt_parsers != ["mineru"] or attempt_states != ["succeeded"]:
            raise RuntimeError(f"case {case.name} did not run one MinerU attempt")
        artifact_path = output_root / f"{case.name}.tar"
        receipt = await provider.download_artifact(
            handle,
            local_path=artifact_path,
            expected_sha256=status.artifact_sha256,
        )
        manifest = _manifest_from_artifact(artifact_path)
        if manifest.source_sha256 != source_sha256:
            raise RuntimeError(f"case {case.name} changed source identity")
        print(
            json.dumps(
                {
                    "artifact_sha256": receipt.sha256,
                    "artifact_size_bytes": receipt.size_bytes,
                    "attempt_parsers": attempt_parsers,
                    "attempt_states": attempt_states,
                    "case": case.name,
                    "mode": case.mode,
                    "page_count": manifest.page_count,
                    "selected_parser": manifest.parser,
                    "source_sha256": source_sha256,
                    "status": "passed",
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    finally:
        await provider.destroy(handle)


async def _run(arguments: argparse.Namespace) -> None:
    cases = _parse_cases(arguments.case)

    exchange_root = _reject_worker_source_runtime_path(
        arguments.exchange_root.resolve(strict=True),
        purpose="--exchange-root",
    )
    output_root = _reject_worker_source_runtime_path(
        arguments.output_root,
        purpose="--output-root",
    )
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    routing = ParserRoutingConfigIdentity(
        revision=arguments.routing_revision,
        sha256=_canonical_sha256(arguments.routing_config.resolve(strict=True)),
    )
    provider = PersistentOciParserProvider(
        exchange_root=exchange_root,
        routing_config=routing,
        management_timeout_seconds=60.0,
    )
    probe = await provider.probe()
    if not probe.available:
        raise RuntimeError(f"worker probe failed with safe error {probe.error_code}")
    for case in cases:
        await _run_case(
            case=case,
            provider=provider,
            routing=routing,
            output_root=output_root,
            timeout_seconds=arguments.timeout_seconds,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--routing-config", type=Path, required=True)
    parser.add_argument(
        "--routing-revision",
        default="mineru_profiles_2026_09_04_v2",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--case",
        action="append",
        nargs=3,
        metavar=("NAME", "MODE", "PDF"),
        required=True,
    )
    arguments = parser.parse_args()
    if not 30 <= arguments.timeout_seconds <= 86_400:
        parser.error("--timeout-seconds must be between 30 and 86400")
    asyncio.run(_run(arguments))


if __name__ == "__main__":
    main()
