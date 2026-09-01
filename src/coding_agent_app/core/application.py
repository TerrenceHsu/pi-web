"""Top-level coding-agent application composition object."""

from __future__ import annotations

from dataclasses import dataclass

from .runtime import CodingAgentRuntime, CodingAgentSessionFactory
from .services import CodingAgentServices, create_coding_agent_services
from .session import CodingAgentSession


@dataclass(slots=True)
class CodingAgentApplication:
    services: CodingAgentServices
    runtime: CodingAgentRuntime

    async def session(self, session_id: str) -> CodingAgentSession:
        return await self.runtime.get_or_create(session_id)

    async def close(self) -> None:
        await self.runtime.close()


def create_coding_agent_application(
    *,
    session_factory: CodingAgentSessionFactory,
    services: CodingAgentServices | None = None,
) -> CodingAgentApplication:
    resolved_services = services or create_coding_agent_services()
    return CodingAgentApplication(
        services=resolved_services,
        runtime=CodingAgentRuntime(
            services=resolved_services,
            session_factory=session_factory,
        ),
    )


__all__ = ["CodingAgentApplication", "create_coding_agent_application"]
