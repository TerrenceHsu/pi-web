"""Managed Sandbox product composition for the coding-agent application."""

from .automation import (
    AUTOMATED_CODING_PROMPT,
    AUTOMATED_CODING_REPAIR_PROMPT,
    AutomatedCodingResult,
    CodingSandboxAutomation,
    CodingSandboxAutomationError,
    CodingToolBootstrapModelClient,
)
from .workspace import (
    WorkspaceSandboxArtifactPublisher,
    WorkspaceSandboxBaselineProvider,
)

__all__ = [
    "AUTOMATED_CODING_PROMPT",
    "AUTOMATED_CODING_REPAIR_PROMPT",
    "AutomatedCodingResult",
    "CodingSandboxAutomation",
    "CodingSandboxAutomationError",
    "CodingToolBootstrapModelClient",
    "WorkspaceSandboxArtifactPublisher",
    "WorkspaceSandboxBaselineProvider",
]
