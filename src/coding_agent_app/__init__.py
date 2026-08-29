"""Product composition adapters for the coding-agent application."""

from .sandbox_workspace import (
    WorkspaceSandboxArtifactPublisher,
    WorkspaceSandboxBaselineProvider,
)

__all__ = [
    "WorkspaceSandboxArtifactPublisher",
    "WorkspaceSandboxBaselineProvider",
]
