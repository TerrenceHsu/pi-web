"""Isolated, provider-neutral contracts for LLM Wiki document parsing.

This package intentionally does not import pi-agent, Marker, Torch, Surya,
Transformers, FastAPI, or any process/container SDK.  Real parser runtimes are
optional Sidecar adapters outside the main application dependency graph.
"""

from .errors import ParserError, ParserErrorCode
from .fake import FakeParserImage, FakeParserOutput, FakeParserProvider
from .models import (
    PARSER_ARTIFACT_SCHEMA,
    PARSER_CONTRACT_VERSION,
    ParserArtifactFile,
    ParserArtifactKind,
    ParserArtifactManifest,
    ParserArtifactReceipt,
    ParserCapabilities,
    ParserImageMimeType,
    ParserJobHandle,
    ParserJobSpec,
    ParserJobState,
    ParserJobStatus,
    ParserLicenseMode,
    ParserLimits,
    ParserMediaType,
    ParserMode,
    ParserProbe,
    ParserProviderName,
    ParserSourceSpec,
    validate_artifact_path,
)
from .provider import ParserProvider

__all__ = [
    "PARSER_ARTIFACT_SCHEMA",
    "PARSER_CONTRACT_VERSION",
    "FakeParserImage",
    "FakeParserOutput",
    "FakeParserProvider",
    "ParserArtifactFile",
    "ParserArtifactKind",
    "ParserArtifactManifest",
    "ParserArtifactReceipt",
    "ParserCapabilities",
    "ParserError",
    "ParserErrorCode",
    "ParserImageMimeType",
    "ParserJobHandle",
    "ParserJobSpec",
    "ParserJobState",
    "ParserJobStatus",
    "ParserLicenseMode",
    "ParserLimits",
    "ParserMediaType",
    "ParserMode",
    "ParserProbe",
    "ParserProvider",
    "ParserProviderName",
    "ParserSourceSpec",
    "validate_artifact_path",
]
