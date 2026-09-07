"""Command-line entry point for the isolated OCI Worker.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .protocol import read_json_object
from .service import PROBE_NAME, serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="pi-wiki-parser-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--queue-root", type=Path, required=True)
    serve_parser.add_argument("--config", type=Path, required=True)
    serve_parser.add_argument("--model-root", type=Path, required=True)
    serve_parser.add_argument("--poll-interval", type=float, default=0.05)
    probe_parser = commands.add_parser("probe")
    probe_parser.add_argument("--queue-root", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "serve":
        if not 0.01 <= arguments.poll_interval <= 5.0:
            parser.error("--poll-interval must be between 0.01 and 5 seconds")
        serve(
            queue_root=arguments.queue_root,
            config_path=arguments.config,
            model_root=arguments.model_root,
            poll_interval_seconds=arguments.poll_interval,
        )
        return
    probe = read_json_object(arguments.queue_root / PROBE_NAME)
    print(json.dumps(probe, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    observed = probe.get("observed_at_ms")
    if (
        probe.get("available") is not True
        or type(observed) is not int
        or not 0 <= time.time_ns() // 1_000_000 - observed <= 30_000
    ):
        raise SystemExit(1)


__all__ = ["main"]
