"""Provider-facing tool definition contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolDef(BaseModel):
    """Provider-neutral tool schema sent to a model."""

    name: str
    label: str
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
    )


__all__ = ["ToolDef"]
