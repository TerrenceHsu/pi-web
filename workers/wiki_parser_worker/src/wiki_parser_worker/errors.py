"""Safe runtime errors for the isolated PDF parser Worker.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Literal

WorkerErrorCode = Literal[
    "dependency_unavailable",
    "dependency_version_mismatch",
    "invalid_configuration",
    "invalid_source",
    "unsafe_source",
    "source_changed",
    "source_too_complex",
    "parser_failed",
    "artifact_limit_exceeded",
    "quality_rejected",
]


class WorkerRuntimeError(RuntimeError):
    """An error whose public representation is only a fixed safe code."""

    def __init__(self, code: WorkerErrorCode) -> None:
        super().__init__(code)
        self.code = code


__all__ = ["WorkerErrorCode", "WorkerRuntimeError"]
