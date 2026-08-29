"""Immutable public identity for the verified Worker package.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only

This program is free software under GNU AGPL version 3 only. It comes with
ABSOLUTELY NO WARRANTY. See the LICENSE file in the component source root.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerComplianceIdentity:
    """Bounded identity for the separately verified OCI Worker release."""

    component_id: str
    version: str
    license_expression: str
    runtime_ready: bool
    source_offer_archive: str


def compliance_identity() -> WorkerComplianceIdentity:
    """Return the current audited identity without importing a parser runtime."""

    return WorkerComplianceIdentity(
        component_id="wiki-parser-worker",
        version="0.0.29",
        license_expression="AGPL-3.0-only",
        runtime_ready=True,
        source_offer_archive="wiki-parser-worker-0.0.29-source.tar.gz",
    )


__all__ = ["WorkerComplianceIdentity", "compliance_identity"]
