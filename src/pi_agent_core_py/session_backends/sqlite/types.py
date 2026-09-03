"""SQLite-specific row and connection types.

The public Session contracts live in ``agent.harness.session.types``.  This
module is deliberately limited to the physical SQLite adapter boundary.
"""

from __future__ import annotations

from typing import TypeAlias, TypedDict

import aiosqlite

SqliteDatabase: TypeAlias = aiosqlite.Connection


class SessionRow(TypedDict):
    id: str
    title: str
    created_at: int
    updated_at: int
    metadata_json: str
    active_lane: str
    parent_session_id: str | None


class EntryRow(TypedDict):
    id: str
    session_id: str
    parent_id: str | None
    entry_type: str
    payload_json: str
    global_seq: int
    created_at: int


class RecordRow(TypedDict):
    id: str
    session_id: str
    seq: int
    lane: str
    run_id: str | None
    type: str
    operation_kind: str | None
    timestamp: int
    payload_json: str


__all__ = ["EntryRow", "RecordRow", "SessionRow", "SqliteDatabase"]
