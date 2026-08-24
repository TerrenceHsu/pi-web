"""Stable errors for the new LLM Wiki persistence boundary."""

from __future__ import annotations

from typing import Literal

WikiErrorCode = Literal[
    "invalid_configuration",
    "invalid_identifier",
    "invalid_space",
    "space_not_found",
    "space_conflict",
    "invalid_source",
    "unsupported_parse_mode",
    "source_not_found",
    "source_conflict",
    "invalid_artifact",
    "artifact_not_found",
    "invalid_job",
    "job_not_found",
    "job_conflict",
    "invalid_status_transition",
    "schema_incompatible",
    "schema_rebuild_required",
    "database_corrupt",
    "path_unsafe",
    "mirror_failed",
    "legacy_busy",
    "legacy_invalid",
    "legacy_backup_failed",
    "legacy_cleanup_failed",
    "file_exists",
    "file_not_found",
    "file_too_large",
    "file_io_failed",
]

_SAFE_MESSAGES: dict[WikiErrorCode, str] = {
    "invalid_configuration": "Wiki storage configuration is invalid.",
    "invalid_identifier": "Wiki identifier is invalid.",
    "invalid_space": "Wiki space data is invalid.",
    "space_not_found": "Wiki space does not exist.",
    "space_conflict": "Wiki space was changed by another operation.",
    "invalid_source": "Wiki source data is invalid.",
    "unsupported_parse_mode": "Wiki parser does not support the requested mode.",
    "source_not_found": "Wiki source does not exist.",
    "source_conflict": "Wiki source conflicts with an existing source.",
    "invalid_artifact": "Wiki artifact data is invalid.",
    "artifact_not_found": "Wiki artifact does not exist.",
    "invalid_job": "Wiki job data is invalid.",
    "job_not_found": "Wiki job does not exist.",
    "job_conflict": "Wiki job state conflicts with another operation.",
    "invalid_status_transition": "Wiki space status transition is invalid.",
    "schema_incompatible": "Wiki database schema is incompatible.",
    "schema_rebuild_required": (
        "Wiki database uses a retired schema and must be rebuilt explicitly."
    ),
    "database_corrupt": "Wiki database failed its integrity check.",
    "path_unsafe": "Wiki filesystem path is unsafe.",
    "mirror_failed": "Wiki filesystem mirror could not be updated.",
    "legacy_busy": "Legacy Knowledge storage is still in use.",
    "legacy_invalid": "Legacy Knowledge storage is invalid.",
    "legacy_backup_failed": "Legacy Knowledge backup failed.",
    "legacy_cleanup_failed": "Legacy Knowledge backup exists but retirement cleanup failed.",
    "file_exists": "Wiki file already exists.",
    "file_not_found": "Wiki file does not exist.",
    "file_too_large": "Wiki file exceeds the configured limit.",
    "file_io_failed": "Wiki filesystem operation failed.",
}


class WikiStoreError(Exception):
    """Content- and path-safe public storage error."""

    def __init__(self, code: WikiErrorCode) -> None:
        super().__init__(_SAFE_MESSAGES[code])
        self.code = code

    def __repr__(self) -> str:
        return f"WikiStoreError(code={self.code!r})"


class WikiSchemaError(WikiStoreError):
    """The file is not a supported Wiki database."""


class WikiPathError(WikiStoreError):
    """A path or filesystem object crossed the fixed Wiki boundary."""


class WikiMirrorError(WikiStoreError):
    """The database committed but its rebuildable mirror failed."""


class LegacyRetirementError(WikiStoreError):
    """Legacy backup or post-backup cleanup did not safely finish."""

    def __init__(self, code: WikiErrorCode, *, receipt: object | None = None) -> None:
        super().__init__(code)
        self.receipt = receipt


__all__ = [
    "LegacyRetirementError",
    "WikiErrorCode",
    "WikiMirrorError",
    "WikiPathError",
    "WikiSchemaError",
    "WikiStoreError",
]
