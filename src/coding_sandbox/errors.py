"""Secret-safe error taxonomy for managed coding sandboxes.

Provider adapters must translate raw SDK/network exceptions into these fixed
codes.  The public exception text intentionally excludes raw provider messages,
request payloads, local paths, command arguments, and credentials.
"""

from __future__ import annotations

from typing import Literal

SandboxErrorCode = Literal[
    "invalid_configuration",
    "authentication_failed",
    "permission_denied",
    "provider_unavailable",
    "rate_limited",
    "sandbox_not_found",
    "sandbox_not_ready",
    "request_timeout",
    "command_timeout",
    "transfer_failed",
    "protocol_error",
    "resource_limit",
    "cancelled",
    "unknown_error",
]

_SAFE_MESSAGES: dict[SandboxErrorCode, str] = {
    "invalid_configuration": "Sandbox configuration is invalid.",
    "authentication_failed": "Sandbox provider authentication failed.",
    "permission_denied": "Sandbox provider permission was denied.",
    "provider_unavailable": "Sandbox provider is unavailable.",
    "rate_limited": "Sandbox provider rate limit was reached.",
    "sandbox_not_found": "Sandbox does not exist or is no longer available.",
    "sandbox_not_ready": "Sandbox is not ready for this operation.",
    "request_timeout": "Sandbox provider request timed out.",
    "command_timeout": "Sandbox command timed out.",
    "transfer_failed": "Sandbox file transfer failed.",
    "protocol_error": "Sandbox provider returned an invalid response.",
    "resource_limit": "Sandbox resource limit was exceeded.",
    "cancelled": "Sandbox operation was cancelled.",
    "unknown_error": "Sandbox operation failed.",
}

_RETRYABLE_CODES: frozenset[SandboxErrorCode] = frozenset(
    {
        "provider_unavailable",
        "rate_limited",
        "request_timeout",
    }
)


class SandboxError(Exception):
    """Base error with stable code and credential-free text.

    ``provider`` is a short backend identifier such as ``"e2b"``.  It is
    diagnostic metadata, not raw provider output.  Callers may chain a private
    cause for debugging, but logs and API responses must serialize this object
    rather than rendering the exception chain.
    """

    def __init__(
        self,
        code: SandboxErrorCode,
        *,
        provider: str | None = None,
    ) -> None:
        super().__init__(_SAFE_MESSAGES[code])
        self.code = code
        self.provider = provider
        self.retryable = code in _RETRYABLE_CODES

    def __repr__(self) -> str:
        return (
            "SandboxError("
            f"code={self.code!r}, provider={self.provider!r}, "
            f"retryable={self.retryable!r})"
        )


__all__ = ["SandboxError", "SandboxErrorCode"]
