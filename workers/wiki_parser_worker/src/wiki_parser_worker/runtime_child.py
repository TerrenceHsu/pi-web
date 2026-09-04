"""Persistent parser child process controlled by the OCI queue supervisor.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import create_runtime_engine
from .protocol import canonical_json_bytes, require_job_directory, require_safe_directory


def _emit(value: object) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    sys.stdout.buffer.flush()


def run_child(*, queue_root: Path, config_path: Path, model_root: Path) -> int:
    root = require_safe_directory(queue_root)
    engine = create_runtime_engine(config_path=config_path, model_root=model_root)
    _emit(
        {
            "config_revision": engine.config.revision,
            "config_sha256": engine.config.sha256,
            "type": "ready",
        }
    )
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > 4096:
            return 2
        try:
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 2
        if not isinstance(payload, dict) or set(payload) != {"op", "provider_job_id"}:
            return 2
        if payload["op"] == "stop":
            return 0
        if payload["op"] != "run" or not isinstance(payload["provider_job_id"], str):
            return 2
        job_dir = require_job_directory(root, payload["provider_job_id"])
        engine.execute(job_dir)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(
        run_child(
            queue_root=arguments.queue_root,
            config_path=arguments.config,
            model_root=arguments.model_root,
        )
    )


if __name__ == "__main__":
    main()
