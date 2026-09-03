"""Serializable Telemetry schema vocabulary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class TelemetryAttributeDefinition:
    type: Literal["string", "number", "boolean", "string[]", "number[]", "boolean[]"]
    description: str
    required: bool = False
    sensitive: bool = False
    cardinality: Literal["low", "high"] = "low"


@dataclass(frozen=True, slots=True)
class TelemetryEventDefinition:
    description: str
    attributes: Mapping[str, TelemetryAttributeDefinition] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TelemetrySpanDefinition:
    description: str
    parents: tuple[str, ...] | Literal["any", "root_or_external"] = "any"
    start_attributes: Mapping[str, TelemetryAttributeDefinition] = field(default_factory=dict)
    end_attributes: Mapping[str, TelemetryAttributeDefinition] = field(default_factory=dict)
    events: Mapping[str, TelemetryEventDefinition] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TelemetrySchema:
    version: int
    spans: Mapping[str, TelemetrySpanDefinition]


def define_telemetry_schema(schema: TelemetrySchema) -> TelemetrySchema:
    """Identity helper that also rejects malformed schema versions/names."""

    if schema.version <= 0:
        raise ValueError("telemetry schema version must be positive")
    if not schema.spans or any(not name.strip() for name in schema.spans):
        raise ValueError("telemetry schema must define non-empty span names")
    return schema


__all__ = [
    "TelemetryAttributeDefinition",
    "TelemetryEventDefinition",
    "TelemetrySchema",
    "TelemetrySpanDefinition",
    "define_telemetry_schema",
]
