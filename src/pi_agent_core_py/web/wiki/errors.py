"""Stable errors for the new LLM Wiki persistence boundary."""

from __future__ import annotations

from typing import Literal

WikiErrorCode = Literal[
    "invalid_configuration",
    "invalid_identifier",
    "invalid_space",
    "space_not_found",
    "space_conflict",
    "space_in_use",
    "space_read_only",
    "invalid_source",
    "unsupported_parse_mode",
    "source_not_found",
    "source_conflict",
    "source_in_use",
    "source_not_available",
    "invalid_artifact",
    "artifact_not_found",
    "invalid_summary",
    "summary_not_found",
    "summary_generation_failed",
    "summary_too_large",
    "invalid_page_proposal",
    "page_proposal_not_found",
    "page_proposal_conflict",
    "invalid_change_set",
    "change_set_not_found",
    "change_set_conflict",
    "change_set_stale",
    "invalid_conversation",
    "conversation_not_found",
    "conversation_conflict",
    "page_not_found",
    "page_revision_not_found",
    "invalid_search_query",
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
    "space_in_use": "Wiki space still contains active content or work.",
    "space_read_only": "Wiki space is not active and cannot be modified.",
    "invalid_source": "Wiki source data is invalid.",
    "unsupported_parse_mode": "Wiki parser does not support the requested mode.",
    "source_not_found": "Wiki source does not exist.",
    "source_conflict": "Wiki source conflicts with an existing source.",
    "source_in_use": "Wiki source is still referenced by an active page.",
    "source_not_available": "Wiki source Raw content is no longer available.",
    "invalid_artifact": "Wiki artifact data is invalid.",
    "artifact_not_found": "Wiki artifact does not exist.",
    "invalid_summary": "Wiki source summary is invalid.",
    "summary_not_found": "Wiki source summary does not exist.",
    "summary_generation_failed": "Wiki source summary generation failed.",
    "summary_too_large": "Wiki source is too large for the configured summarizer.",
    "invalid_page_proposal": "Wiki page proposal is invalid.",
    "page_proposal_not_found": "Wiki page proposal does not exist.",
    "page_proposal_conflict": "Wiki page proposal conflicts with another proposal.",
    "invalid_change_set": "Wiki change set is invalid.",
    "change_set_not_found": "Wiki change set does not exist.",
    "change_set_conflict": "Wiki change set conflicts with another operation.",
    "change_set_stale": "Wiki change set is stale and was not published.",
    "invalid_conversation": "Wiki conversation is invalid.",
    "conversation_not_found": "Wiki conversation does not exist.",
    "conversation_conflict": "Wiki conversation conflicts with another operation.",
    "page_not_found": "Wiki page does not exist.",
    "page_revision_not_found": "Wiki page revision does not exist.",
    "invalid_search_query": "Wiki page search query is invalid.",
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
