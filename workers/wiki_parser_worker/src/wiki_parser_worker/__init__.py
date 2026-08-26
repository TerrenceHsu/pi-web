"""Audited adapter source for the isolated LLM Wiki PDF parser Worker.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only

This program is free software under GNU AGPL version 3 only. It comes with
ABSOLUTELY NO WARRANTY. See the LICENSE file in the component source root.
"""

from __future__ import annotations

from .compliance import WorkerComplianceIdentity, compliance_identity
from .config import WorkerRoutingConfig, load_routing_config
from .errors import WorkerRuntimeError
from .parsers import DoclingAccurateParser, PyMuPdf4LlmFastParser
from .preflight import inspect_pdf, route_pdf
from .quality import QualityEvaluator

__version__ = "0.0.28"

__all__ = [
    "DoclingAccurateParser",
    "PyMuPdf4LlmFastParser",
    "QualityEvaluator",
    "WorkerComplianceIdentity",
    "WorkerRoutingConfig",
    "WorkerRuntimeError",
    "__version__",
    "compliance_identity",
    "inspect_pdf",
    "load_routing_config",
    "route_pdf",
]
