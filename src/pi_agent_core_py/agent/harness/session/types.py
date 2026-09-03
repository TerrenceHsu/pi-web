"""Provider-neutral durable Session contracts.

The contracts describe the Agent Harness view of persistence.  They do not
mention SQLite, Web transports, or product-specific stores, so an in-memory,
SQLite, JSONL, or remote backend can share the same conformance suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)

EntryOrder = Literal["newest_first", "oldest_first"]
ForkScope = Literal["branch", "tree"]


class SessionBackendError(RuntimeError):
    """Stable backend error carrying a machine-readable category."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionMetadata(BaseModel):
    id: str
    created_at: int
    updated_at: int | None = None
    parent_session_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionStats(BaseModel):
    message_count: int = 0
    cached_tokens: float = 0
    uncached_tokens: float = 0
    total_tokens: float = 0
    cost_total: float = 0


class NewSessionEntry(BaseModel):
    id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    terminate: bool = False


class StoredSessionEntry(NewSessionEntry):
    seq: int
    parent_id: str | None
    timestamp: int


class NewSessionRecord(BaseModel):
    id: str
    lane: str
    type: str
    run_id: str | None = None
    operation_kind: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class StoredSessionRecord(NewSessionRecord):
    seq: int
    timestamp: int


class LanePointer(BaseModel):
    lane: str
    leaf_id: str | None


class EntryCursor(BaseModel):
    after_seq: int


class EntryQuery(BaseModel):
    type: str | None = None
    custom_type: str | None = None
    order: EntryOrder = "newest_first"
    limit: int | None = None
    cursor: EntryCursor | None = None


class BranchBounds(BaseModel):
    start: str | None = None
    stop_at_type: str | None = None
    stop_at_id: str | None = None


class RecordQuery(BaseModel):
    lane: str | None = None
    type: str | None = None
    run_id: str | None = None
    operation_kind: str | None = None
    after_seq: int | None = None
    order: EntryOrder = "newest_first"
    limit: int | None = None


class SessionLogItem(BaseModel):
    seq: int
    kind: Literal["entry", "record", "lane", "fact"]
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionSearchOptions(BaseModel):
    entry_types: tuple[str, ...] | None = None
    limit: int | None = None
    cancel_event: asyncio.Event | None = None

    model_config = {"arbitrary_types_allowed": True}


class SessionSearchHit(BaseModel):
    session_id: str
    entry_id: str
    timestamp: int
    score: float
    metadata: SessionMetadata


@runtime_checkable
class SessionStorage(Protocol):
    """One opened Session with durable write ownership."""

    async def get_metadata(self) -> SessionMetadata: ...

    async def get_lanes(self) -> list[LanePointer]: ...

    async def create_lane(self, lane: str, at: str | None) -> None: ...

    async def move_lane(self, lane: str, to: str | None) -> None: ...

    async def append_entry(
        self, entry: NewSessionEntry, lane: str
    ) -> StoredSessionEntry: ...

    async def append_record(
        self, record: NewSessionRecord
    ) -> StoredSessionRecord: ...

    async def get_entry(self, entry_id: str) -> StoredSessionEntry | None: ...

    async def find_entries(
        self, query: EntryQuery | None = None
    ) -> list[StoredSessionEntry]: ...

    async def find_entries_on_branch(
        self, query: EntryQuery, bounds: BranchBounds
    ) -> list[StoredSessionEntry]: ...

    async def find_records(
        self, query: RecordQuery | None = None
    ) -> list[StoredSessionRecord]: ...

    async def find_open_operations(
        self, lane: str, *, limit: int = 2
    ) -> list[StoredSessionRecord]: ...

    async def get_log(
        self, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[SessionLogItem]: ...

    async def get_name(self) -> str | None: ...

    async def set_name(self, name: str | None) -> None: ...

    async def get_label(self, entry_id: str) -> str | None: ...

    async def set_label(self, entry_id: str, label: str | None) -> None: ...

    async def get_stats(self) -> SessionStats: ...

    async def release(self) -> None: ...


@runtime_checkable
class SessionRepository(Protocol):
    """Catalog and lifecycle boundary for durable Sessions."""

    async def create(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
        parent_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionStorage: ...

    async def open(self, metadata: SessionMetadata) -> SessionStorage: ...

    async def list(self) -> list[SessionMetadata]: ...

    async def delete(self, metadata: SessionMetadata) -> None: ...

    async def fork(
        self,
        source: SessionMetadata,
        *,
        scope: ForkScope = "branch",
        entry_id: str | None = None,
        position: Literal["before", "at"] = "at",
        session_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionStorage: ...

    async def close(self) -> None: ...


@runtime_checkable
class SessionSearch(Protocol):
    def search(
        self,
        text: str,
        options: SessionSearchOptions | None = None,
    ) -> AsyncIterator[SessionSearchHit]: ...


__all__ = [
    "BranchBounds",
    "EntryOrder",
    "EntryCursor",
    "EntryQuery",
    "ForkScope",
    "JsonValue",
    "LanePointer",
    "NewSessionEntry",
    "NewSessionRecord",
    "RecordQuery",
    "SessionBackendError",
    "SessionLogItem",
    "SessionMetadata",
    "SessionRepository",
    "SessionSearch",
    "SessionSearchHit",
    "SessionSearchOptions",
    "SessionStats",
    "SessionStorage",
    "StoredSessionEntry",
    "StoredSessionRecord",
]
