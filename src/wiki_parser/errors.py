"""Secret- and content-safe error taxonomy for Wiki parser providers."""

from __future__ import annotations

from typing import Literal

ParserErrorCode = Literal[
    "invalid_configuration",
    "license_not_configured",
    "unsupported_media_type",
    "unsupported_parse_mode",
    "invalid_source",
    "source_too_large",
    "preflight_failed",
    "routing_failed",
    "provider_unavailable",
    "job_not_found",
    "job_not_ready",
    "request_timeout",
    "parsing_failed",
    "quality_rejected",
    "artifact_unavailable",
    "artifact_invalid",
    "resource_limit",
    "cancelled",
    "protocol_error",
    "unknown_error",
]

_SAFE_MESSAGES: dict[ParserErrorCode, str] = {
    "invalid_configuration": "Parser provider configuration is invalid.",
    "license_not_configured": "Parser provider license eligibility is not configured.",
    "unsupported_media_type": "Parser provider does not support this media type.",
    "unsupported_parse_mode": "Parser provider does not support this parse mode.",
    "invalid_source": "Parser source is invalid.",
    "source_too_large": "Parser source exceeds the configured size limit.",
    "preflight_failed": "PDF preflight failed.",
    "routing_failed": "PDF parser routing failed.",
    "provider_unavailable": "Parser provider is unavailable.",
    "job_not_found": "Parser job does not exist or is no longer available.",
    "job_not_ready": "Parser job is not ready for this operation.",
    "request_timeout": "Parser provider request timed out.",
    "parsing_failed": "Document parsing failed.",
    "quality_rejected": "Parsed document did not pass quality checks.",
    "artifact_unavailable": "Parser artifact is unavailable.",
    "artifact_invalid": "Parser artifact is invalid.",
    "resource_limit": "Parser resource limit was exceeded.",
    "cancelled": "Parser job was cancelled.",
    "protocol_error": "Parser provider returned an invalid response.",
    "unknown_error": "Parser operation failed.",
}

_RETRYABLE_CODES: frozenset[ParserErrorCode] = frozenset(
    {
        "provider_unavailable",
        "request_timeout",
    }
)


class ParserError(Exception):
    """Stable public parser error without source content or raw exceptions."""

    def __init__(
        self,
        code: ParserErrorCode,
        *,
        provider: str | None = None,
    ) -> None:
        super().__init__(_SAFE_MESSAGES[code])
        self.code = code
        self.provider = provider
        self.retryable = code in _RETRYABLE_CODES

    def __repr__(self) -> str:
        return (
            "ParserError("
            f"code={self.code!r}, provider={self.provider!r}, "
            f"retryable={self.retryable!r})"
        )


__all__ = ["ParserError", "ParserErrorCode"]
