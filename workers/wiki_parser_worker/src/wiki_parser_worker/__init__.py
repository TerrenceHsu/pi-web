"""Audited adapter source for the isolated MinerU PDF parser Worker.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from .compliance import WorkerComplianceIdentity, compliance_identity
from .config import WorkerRoutingConfig, load_routing_config
from .errors import WorkerRuntimeError
from .parsers import MineruParser
from .preflight import inspect_pdf, route_pdf
from .quality import QualityEvaluator

__version__ = "0.0.29"

__all__ = [
    "MineruParser",
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
