"""Coherent services supplied to a coding-agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from .resources import (
    CodingAgentResourceLoader,
    ResourceDiagnostic,
    StaticCodingAgentResourceLoader,
)
from .settings import (
    CodingAgentSettingsProvider,
    StaticCodingAgentSettingsProvider,
)


@dataclass(slots=True)
class CodingAgentServices:
    """Product services shared by Sessions but owned outside Agent core.

    The optional live service fields deliberately use narrow opaque references:
    their concrete stores belong to the application packages, while the coding
    runtime only coordinates their lifecycle.
    """

    settings: CodingAgentSettingsProvider = field(
        default_factory=StaticCodingAgentSettingsProvider
    )
    resources: CodingAgentResourceLoader = field(
        default_factory=StaticCodingAgentResourceLoader
    )
    session_store: object | None = None
    workspace_store: object | None = None
    provider_runtime: object | None = None
    sandbox_lifecycle: object | None = None
    diagnostics: list[ResourceDiagnostic] = field(default_factory=list)


def create_coding_agent_services(
    *,
    settings: CodingAgentSettingsProvider | None = None,
    resources: CodingAgentResourceLoader | None = None,
    session_store: object | None = None,
    workspace_store: object | None = None,
    provider_runtime: object | None = None,
    sandbox_lifecycle: object | None = None,
) -> CodingAgentServices:
    """Create the product service bundle without starting network resources."""

    return CodingAgentServices(
        settings=settings or StaticCodingAgentSettingsProvider(),
        resources=resources or StaticCodingAgentResourceLoader(),
        session_store=session_store,
        workspace_store=workspace_store,
        provider_runtime=provider_runtime,
        sandbox_lifecycle=sandbox_lifecycle,
    )


__all__ = ["CodingAgentServices", "create_coding_agent_services"]
