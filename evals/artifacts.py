"""Private-by-default persistence for local eval observations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .comparison import EvalReport
from .harness import EvalSuite
from .models import EvalScoredObservation, JsonValue
from .reporter import format_markdown_report


def default_artifact_directory(project_root: Path | None = None) -> Path:
    root = project_root or Path.cwd()
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return root / ".eval" / f"{timestamp}_{uuid4().hex}"


@dataclass(slots=True)
class EvalArtifactWriter:
    directory: Path
    include_content: bool = False

    def initialize(self, suites: tuple[EvalSuite, ...]) -> None:
        managed_names = ("manifest.json", "runs.jsonl", "summary.json", "summary.md")
        existing = [name for name in managed_names if (self.directory / name).exists()]
        if existing:
            raise FileExistsError(
                "eval artifact directory already contains a run: "
                + ", ".join(existing)
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.directory, 0o700)
        manifest: dict[str, JsonValue] = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "include_content": self.include_content,
            "offline": True,
            "suites": [
                {
                    "name": suite.name,
                    "baseline": suite.baseline.name,
                    "candidates": [candidate.name for candidate in suite.candidates],
                    "cases": [case.id for case in suite.cases],
                    "repetitions": suite.repetitions,
                    "minimum_candidate_score": suite.minimum_candidate_score,
                }
                for suite in suites
            ],
        }
        self._write_json("manifest.json", manifest)

    def append(self, result: EvalScoredObservation) -> None:
        path = self.directory / "runs.jsonl"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    result.to_record(include_content=self.include_content),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            stream.write("\n")
        _restrict_permissions(path, 0o600)

    def write_report(self, report: EvalReport) -> None:
        self._write_json("summary.json", report.to_record())
        path = self.directory / "summary.md"
        path.write_text(format_markdown_report(report), encoding="utf-8", newline="\n")
        _restrict_permissions(path, 0o600)

    def _write_json(self, name: str, value: dict[str, JsonValue]) -> None:
        path = self.directory / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _restrict_permissions(path, 0o600)


def _restrict_permissions(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        return


__all__ = ["EvalArtifactWriter", "default_artifact_directory"]
