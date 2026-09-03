"""SQLite repository and compatibility store for durable Agent sessions.

设计要点：
- 单文件 sqlite + WAL 模式；单写者（aiosqlite 单连接）
- **独立于** `session.py` / `session_sync.py` / harness 内置 session 体系
- Web 层用本模块管理多会话；旧 JSONL Tree 路径不动
- immutable ``session_entries`` 组成 parent tree；lane 持久化 active leaf
- 旧 ``messages`` 表保留为 active lane 的兼容投影
- message 序列化为 `{type, data}` JSON；反序列化回强类型 Pydantic 对象
  （不会退化成 dict）
- 并发 `append_message` 用 `BEGIN IMMEDIATE` + `SELECT MAX(idx)+1` 保证 idx 单调
- snapshot 表存 TurnSnapshot 完整 dict（`to_dict()` / `model_validate()`）
- lane operation intent/result 使用 append-only records；需要改变 lane 的 finish
  与 leaf move 在同一个 SQLite 事务提交

异常体系：
- `SQLiteSessionError` 基类
- `SessionNotFoundError` —— rename/delete/append/list 对不存在 session
- `SessionSerializationError` —— JSON 反序列化失败 / 未知 type

不实现：
- provider / tool 调用的通用自动重放（目前仅提供 durable operation 原语）
- 自动 compaction（由 harness.compact_* 触发；sqlite 仅持久化）
- 跨进程共享（单进程场景；多进程需要单独协调）
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, Field

from ...agent.harness.session.types import (
    BranchBounds,
    EntryQuery,
    ForkScope,
    LanePointer,
    NewSessionEntry,
    NewSessionRecord,
    RecordQuery,
    SessionBackendError,
    SessionLogItem,
    SessionMetadata,
    SessionStats,
    StoredSessionEntry,
    StoredSessionRecord,
)
from ...agent.harness.snapshot import RequestSnapshot
from ...agent.messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    SummaryMessage,
    ToolResultMessage,
    UserMessage,
)
from .branch_cache import append_entry_to_branch_cache, rebuild_branch_cache
from .database import ReentrantAsyncLock, database_for, serialized_operation
from .migrations import apply_migrations
from .storage.entries import id_exists_in_entries
from .storage.records import id_exists_in_records
from .storage.session_sequences import allocate_sequence, create_sequence
from .storage.session_stats import add_usage, create_stats, increment_messages, read_stats
from .storage.sessions import session_exists
from .storage.writer_leases import (
    WriterLease,
    WriterLeaseError,
    claim_writer_lease,
    release_writer_lease,
    renew_writer_lease,
)

# ============================================================================
# 异常
# ============================================================================


class SQLiteSessionError(SessionBackendError):
    """sqlite session 模块基类异常。"""

    error_code = "storage"

    def __init__(self, message: str) -> None:
        super().__init__(self.error_code, message)


class SessionNotFoundError(SQLiteSessionError):
    """rename/delete/append/list 对不存在 session 抛。"""

    error_code = "not_found"


class SessionSerializationError(SQLiteSessionError):
    """JSON 反序列化失败 / 未知 message type 抛。"""


class SessionLaneNotFoundError(SQLiteSessionError):
    """请求的 lane 不存在。"""

    error_code = "invalid_lane"


class SessionLaneExistsError(SQLiteSessionError):
    """创建已存在的 lane。"""

    error_code = "already_exists"


class SessionEntryNotFoundError(SQLiteSessionError):
    """请求的 entry 不存在或不属于目标 Session。"""

    error_code = "not_found"


class SessionBranchError(SQLiteSessionError):
    """branch/fork 目标不在 source lane 当前路径上。"""


class SessionOperationConflictError(SQLiteSessionError):
    """lane 已有未完成 operation，或恢复时 source leaf 已发生变化。"""


# ============================================================================
# Pydantic 数据模型（用于对外返回，类型稳定）
# ============================================================================


class SQLiteSession(BaseModel):
    """session 元信息（不含 messages / snapshots）。"""

    id: str
    title: str
    created_at: int
    updated_at: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    active_lane: str = "main"
    parent_session_id: str | None = None


class SQLiteStoredMessage(BaseModel):
    """单条消息记录（含 idx / role 元信息）。"""

    id: str
    session_id: str
    idx: int
    role: str
    message: AgentMessage
    created_at: int

    model_config = {"arbitrary_types_allowed": True}


class SQLiteStoredSnapshot(BaseModel):
    """单条 snapshot 记录。"""

    id: str
    session_id: str
    turn_id: str | None
    snapshot: RequestSnapshot
    created_at: int


class SQLiteSessionEntry(BaseModel):
    """append-only tree entry；``seq`` 在单个 Session 内单调递增。"""

    id: str
    session_id: str
    seq: int
    parent_id: str | None
    message_id: str
    role: str
    message: AgentMessage | None = None
    created_at: int
    label: str | None = None
    entry_type: str = "message"
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class SQLiteSessionLane(BaseModel):
    """命名 lane 及其当前 leaf。"""

    session_id: str
    name: str
    leaf_entry_id: str | None
    created_at: int
    updated_at: int
    is_active: bool = False


OperationOutcome = Literal[
    "completed", "aborted", "failed", "declined", "conflict"
]


class SQLiteOperationRecord(BaseModel):
    """append-only durable operation record。"""

    id: str
    operation_id: str
    session_id: str
    seq: int
    record_type: str
    payload: dict[str, Any]
    created_at: int


class SQLiteSessionOperation(BaseModel):
    """durable lane operation 及其 append-only record reduction。"""

    id: str
    session_id: str
    lane: str
    kind: str
    dedupe_key: str
    source_leaf_id: str | None
    payload: dict[str, Any]
    created_at: int
    records: tuple[SQLiteOperationRecord, ...] = ()
    outcome: OperationOutcome | None = None

    @property
    def is_open(self) -> bool:
        return self.outcome is None

    @property
    def effect_committed(self) -> bool:
        return any(
            record.record_type == "effect_committed" for record in self.records
        )


# ============================================================================
# 序列化辅助
# ============================================================================


#: message type → Pydantic 类映射；CustomMessage 也支持（虽不在 Message union，
#: 避免 Web 路径下 stray CustomMessage 被丢）
_MESSAGE_TYPE_MAP: dict[str, type[BaseModel]] = {
    "UserMessage": UserMessage,
    "AssistantMessage": AssistantMessage,
    "ToolResultMessage": ToolResultMessage,
    "SummaryMessage": SummaryMessage,
    "CustomMessage": CustomMessage,
}

#: 反向：Pydantic 类 → type 字符串
_MESSAGE_TYPE_REVERSE: dict[type[BaseModel], str] = {
    v: k for k, v in _MESSAGE_TYPE_MAP.items()
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _gen_id(prefix: str) -> str:
    """生成带时间戳 + uuid 后缀的 id。"""
    return f"{prefix}-{_now_ms()}-{uuid.uuid4().hex[:8]}"


def _serialize_message(message: AgentMessage) -> str:
    """AgentMessage → `{type, data}` JSON 字符串。"""
    cls = type(message)
    type_name = _MESSAGE_TYPE_REVERSE.get(cls)
    if type_name is None:
        raise SessionSerializationError(
            f"未注册的 message 类型 {cls.__name__}；"
            f"当前支持 {list(_MESSAGE_TYPE_MAP.keys())}"
        )
    payload = {
        "type": type_name,
        "data": message.model_dump(mode="json"),
    }
    return json.dumps(payload, ensure_ascii=False)


def _deserialize_message(content_json: str) -> AgentMessage:
    """`{type, data}` JSON 字符串 → 强类型 AgentMessage。

    未知 type 抛 SessionSerializationError；不静默返回 dict。
    """
    try:
        payload = json.loads(content_json)
    except json.JSONDecodeError as e:
        raise SessionSerializationError(
            f"message JSON 解析失败：{e.msg}"
        ) from e
    if not isinstance(payload, dict) or "type" not in payload or "data" not in payload:
        raise SessionSerializationError(
            f"message JSON 结构不合法：缺 type / data 字段；got={payload!r}"
        )
    type_name = payload["type"]
    cls = _MESSAGE_TYPE_MAP.get(type_name)
    if cls is None:
        raise SessionSerializationError(
            f"未知 message type {type_name!r}；当前支持 {list(_MESSAGE_TYPE_MAP.keys())}"
        )
    try:
        return cls.model_validate(payload["data"])  # type: ignore[return-value]
    except Exception as e:
        raise SessionSerializationError(
            f"{type_name} 反序列化失败：{type(e).__name__}: {e}"
        ) from e


def _serialize_snapshot(snapshot: RequestSnapshot) -> str:
    """RequestSnapshot → JSON 字符串（走 to_dict，已是 JSON-safe）。"""
    return json.dumps(snapshot.to_dict(), ensure_ascii=False)


def _deserialize_snapshot(content_json: str) -> RequestSnapshot:
    """JSON 字符串 → RequestSnapshot。"""
    try:
        payload = json.loads(content_json)
    except json.JSONDecodeError as e:
        raise SessionSerializationError(
            f"snapshot JSON 解析失败：{e.msg}"
        ) from e
    if not isinstance(payload, dict):
        raise SessionSerializationError(
            f"snapshot JSON 不是 object：{type(payload).__name__}"
        )
    try:
        return RequestSnapshot.model_validate(payload)
    except Exception as e:
        raise SessionSerializationError(
            f"RequestSnapshot 反序列化失败：{type(e).__name__}: {e}"
        ) from e


def _serialize_operation_payload(payload: dict[str, Any] | None) -> str:
    """Serialize a secret-safe operation payload as one JSON object."""
    try:
        return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as e:
        raise SessionSerializationError(
            f"operation payload 不是 JSON-safe object：{type(e).__name__}: {e}"
        ) from e


def _deserialize_operation_payload(content_json: str) -> dict[str, Any]:
    try:
        value = json.loads(content_json)
    except json.JSONDecodeError as e:
        raise SessionSerializationError(
            f"operation payload JSON 解析失败：{e.msg}"
        ) from e
    if not isinstance(value, dict):
        raise SessionSerializationError("operation payload 必须是 JSON object")
    return value


# ============================================================================
# SQLiteSessionStore
# ============================================================================


class SQLiteSessionStore:
    """sqlite append-only session tree 存储。

    生命周期：
        store = SQLiteSessionStore(db_path="./data/sessions.db")
        await store.init()              # 建表 / PRAGMA
        session = await store.create_session(title="demo")
        await store.append_message(session.id, user_msg)
        msgs = await store.list_messages(session.id)  # -> list[AgentMessage]
        await store.close()             # 幂等

    并发：aiosqlite 内部单线程串行；同一连接上所有 SQL 排队执行。
    BEGIN IMMEDIATE + SELECT MAX(idx) + INSERT 在同一事务里——其它并发
    append 会被 BEGIN IMMEDIATE 阻塞到当前事务 COMMIT，保证 idx 不冲突。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        writer_lease_ttl_ms: int = 30_000,
        writer_heartbeat_interval_ms: int | None = None,
    ):
        if writer_lease_ttl_ms <= 0:
            raise ValueError("writer_lease_ttl_ms must be positive")
        interval = writer_heartbeat_interval_ms or max(1, writer_lease_ttl_ms // 3)
        if interval <= 0 or interval >= writer_lease_ttl_ms:
            raise ValueError(
                "writer_heartbeat_interval_ms must be positive and less than TTL"
            )
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._closed: bool = False
        self._init_lock = asyncio.Lock()
        self._operation_lock = ReentrantAsyncLock()
        self._write_lock = self._operation_lock
        self._writer_lease_ttl_ms = writer_lease_ttl_ms
        self._writer_heartbeat_interval_ms = interval
        self._writer_leases: dict[str, WriterLease] = {}
        self._writer_ref_counts: dict[str, int] = {}
        self._lost_writer_sessions: set[str] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """打开连接 + 建 schema + 设 PRAGMA。幂等。

        若已 init 过则 no-op。重复调安全。
        """
        async with self._init_lock:
            await self._init_unlocked()

    async def _init_unlocked(self) -> None:
        if self._db is not None:
            return
        # 父目录若不存在则创建（不抛错；db_path 可能是 :memory:）
        db_path = self._db_path
        if db_path != ":memory:":
            parent = Path(db_path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        db = await aiosqlite.connect(db_path)
        self._db = db
        try:
            db.row_factory = aiosqlite.Row
            coordinator = database_for(db)
            self._operation_lock = coordinator.operation_lock
            self._write_lock = self._operation_lock
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=FULL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=5000")
            await self._exec_schema()
            await db.commit()
        except BaseException:
            # Reset first so a close failure cannot leave a half-initialized
            # connection exposed through ``connection`` / ``_require_db``.
            self._db = None
            await db.close()
            raise
        self._closed = False
        self._closing = False

    async def _ensure_writer_lease(self, session_id: str) -> WriterLease:
        db = self._require_db()
        await self._require_session(session_id)
        if session_id in self._lost_writer_sessions:
            raise WriterLeaseError(f"writer lease lost for session {session_id!r}")
        now = _now_ms()
        lease = self._writer_leases.get(session_id)
        if lease is not None:
            if await renew_writer_lease(
                db,
                session_id,
                lease,
                now_ms=now,
                ttl_ms=self._writer_lease_ttl_ms,
            ):
                return lease
        try:
            lease = await claim_writer_lease(
                db,
                session_id,
                now_ms=now,
                ttl_ms=self._writer_lease_ttl_ms,
                owner_id=lease.owner_id if lease is not None else None,
            )
        except WriterLeaseError:
            # A fresh open rejected by another live writer may be retried after
            # that writer releases.  Only an owner that previously held a
            # fence becomes permanently stale after losing it.
            if lease is not None:
                self._lost_writer_sessions.add(session_id)
            raise
        self._writer_leases[session_id] = lease
        self._start_heartbeat()
        return lease

    def _start_heartbeat(self) -> None:
        if self._closing or self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="sqlite-session-writer-heartbeat"
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(self._writer_heartbeat_interval_ms / 1000)
                async with self._operation_lock:
                    db = self._require_db()
                    try:
                        await db.execute("BEGIN IMMEDIATE")
                        now = _now_ms()
                        for session_id, lease in tuple(self._writer_leases.items()):
                            renewed = await renew_writer_lease(
                                db,
                                session_id,
                                lease,
                                now_ms=now,
                                ttl_ms=self._writer_lease_ttl_ms,
                            )
                            if not renewed:
                                self._lost_writer_sessions.add(session_id)
                                self._writer_leases.pop(session_id, None)
                        await db.commit()
                    except BaseException:
                        await db.rollback()
                        raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # Every write verifies its fence transactionally.  A transient
            # heartbeat failure therefore does not grant stale write access.
            if not self._closing:
                self._heartbeat_task = None
                self._start_heartbeat()

    async def claim_session_writer(self, session_id: str) -> None:
        """Acquire or share this Store's writer lease for one Session."""

        async with self._operation_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._require_session(session_id)
                await self._ensure_writer_lease(session_id)
                self._writer_ref_counts[session_id] = (
                    self._writer_ref_counts.get(session_id, 0) + 1
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def release_session_writer(self, session_id: str) -> None:
        """Release one opened handle, dropping the lease after its final user."""

        async with self._operation_lock:
            count = self._writer_ref_counts.get(session_id, 0)
            if count > 1:
                self._writer_ref_counts[session_id] = count - 1
                return
            self._writer_ref_counts.pop(session_id, None)
            lease = self._writer_leases.pop(session_id, None)
            self._lost_writer_sessions.discard(session_id)
            if lease is None or self._db is None:
                return
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await release_writer_lease(self._db, session_id, lease)
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise

    async def _exec_schema(self) -> None:
        assert self._db is not None
        await apply_migrations(
            self._db,
            legacy_tree_migration=self._migrate_session_tree,
        )

    async def _migrate_session_tree(self) -> None:
        """幂等迁移旧线性库，并为每个 Session 建立 ``main`` lane。"""
        db = self._require_db()

        cur = await db.execute("PRAGMA table_info(sessions)")
        session_columns = {row["name"] for row in await cur.fetchall()}
        await cur.close()
        if "active_lane" not in session_columns:
            await db.execute(
                "ALTER TABLE sessions ADD COLUMN active_lane "
                "TEXT NOT NULL DEFAULT 'main'"
            )

        cur = await db.execute(
            "SELECT id, created_at, updated_at FROM sessions ORDER BY created_at, id"
        )
        sessions = list(await cur.fetchall())
        await cur.close()
        for session in sessions:
            session_id = session["id"]
            cur = await db.execute(
                "SELECT 1 FROM session_entries WHERE session_id = ? LIMIT 1",
                (session_id,),
            )
            has_entries = await cur.fetchone() is not None
            await cur.close()

            leaf_id: str | None = None
            if not has_entries:
                cur = await db.execute(
                    "SELECT id, role, content_json, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY idx",
                    (session_id,),
                )
                message_rows = list(await cur.fetchall())
                await cur.close()
                parent_id: str | None = None
                for seq, message_row in enumerate(message_rows):
                    entry_id = _gen_id("entry")
                    await db.execute(
                        "INSERT INTO session_entries "
                        "(id, session_id, seq, parent_id, message_id, role, "
                        "content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry_id,
                            session_id,
                            seq,
                            parent_id,
                            message_row["id"],
                            message_row["role"],
                            message_row["content_json"],
                            message_row["created_at"],
                        ),
                    )
                    parent_id = entry_id
                leaf_id = parent_id
            else:
                cur = await db.execute(
                    "SELECT id FROM session_entries WHERE session_id = ? "
                    "ORDER BY seq DESC LIMIT 1",
                    (session_id,),
                )
                last_entry = await cur.fetchone()
                await cur.close()
                leaf_id = last_entry["id"] if last_entry is not None else None

            await db.execute(
                "INSERT OR IGNORE INTO session_lanes "
                "(session_id, name, leaf_entry_id, created_at, updated_at) "
                "VALUES (?, 'main', ?, ?, ?)",
                (
                    session_id,
                    leaf_id,
                    session["created_at"],
                    session["updated_at"],
                ),
            )

    @serialized_operation
    async def close(self) -> None:
        """关闭连接。幂等。"""
        if self._closed or self._db is None:
            self._closed = True
            return
        self._closing = True
        heartbeat = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        try:
            if self._db.in_transaction:
                await self._db.rollback()
            await self._db.execute("BEGIN IMMEDIATE")
            for session_id, lease in tuple(self._writer_leases.items()):
                await release_writer_lease(self._db, session_id, lease)
            await self._db.commit()
        finally:
            try:
                await self._db.close()
            finally:
                self._writer_leases.clear()
                self._writer_ref_counts.clear()
                self._lost_writer_sessions.clear()
                self._db = None
                self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def connection(self) -> aiosqlite.Connection | None:
        """P1-C1: 暴露内部 connection 供 ExtensionSQLiteStore 共享——
        :memory: 模式下两个独立 connect(':memory:') 是不同数据库，
        必须共享同一 connection 才能让 session 表和 extension 表共存。"""
        return self._db

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise SQLiteSessionError("store 未 init；先 await store.init()")
        return self._db

    async def _require_session(self, session_id: str) -> None:
        """检查 session 存在；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        if not await session_exists(db, session_id):
            raise SessionNotFoundError(f"session {session_id!r} 不存在")

    @staticmethod
    def _row_to_session(row: aiosqlite.Row) -> SQLiteSession:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except json.JSONDecodeError as error:
            raise SessionSerializationError(
                f"session {row['id']!r} metadata JSON 解析失败"
            ) from error
        if not isinstance(metadata, dict):
            raise SessionSerializationError(
                f"session {row['id']!r} metadata 必须是 JSON object"
            )
        return SQLiteSession(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=metadata,
            active_lane=row["active_lane"],
            parent_session_id=row["parent_session_id"],
        )

    async def _append_log_item(
        self,
        session_id: str,
        *,
        kind: str,
        item_id: str | None,
        timestamp: int,
        payload: dict[str, Any],
    ) -> int:
        db = self._require_db()
        seq = await allocate_sequence(db, session_id)
        await db.execute(
            "INSERT INTO session_log "
            "(session_id, seq, kind, item_id, timestamp, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                seq,
                kind,
                item_id,
                timestamp,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        return seq

    async def _append_name_fact(
        self, session_id: str, name: str | None, *, timestamp: int
    ) -> int:
        db = self._require_db()
        value_json = json.dumps(name, ensure_ascii=False)
        seq = await self._append_log_item(
            session_id,
            kind="fact",
            item_id=None,
            timestamp=timestamp,
            payload={"fact": "name", "value": name},
        )
        await db.execute(
            "INSERT INTO session_global_facts "
            "(session_id, seq, kind, key, value_json, timestamp) "
            "VALUES (?, ?, 'name', NULL, ?, ?)",
            (session_id, seq, value_json, timestamp),
        )
        return seq

    async def _append_lane_move(
        self,
        session_id: str,
        lane: str,
        leaf_id: str | None,
        *,
        timestamp: int,
    ) -> int:
        db = self._require_db()
        seq = await self._append_log_item(
            session_id,
            kind="lane",
            item_id=None,
            timestamp=timestamp,
            payload={"lane": lane, "leaf_id": leaf_id},
        )
        await db.execute(
            "INSERT INTO session_lane_moves "
            "(session_id, seq, lane, leaf_id, timestamp) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, lane, leaf_id, timestamp),
        )
        return seq

    @staticmethod
    def _validate_lane_name(name: str) -> str:
        normalized = name.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("lane name must contain 1..64 non-space characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("lane name must not contain control characters")
        return normalized

    async def _active_lane_name(self, session_id: str) -> str:
        db = self._require_db()
        cur = await db.execute(
            "SELECT active_lane FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise SessionNotFoundError(f"session {session_id!r} 不存在")
        return str(row["active_lane"])

    async def _lane_row(
        self, session_id: str, lane: str,
    ) -> aiosqlite.Row:
        db = self._require_db()
        cur = await db.execute(
            "SELECT session_id, name, leaf_entry_id, open_operation_id, "
            "created_at, updated_at "
            "FROM session_lanes WHERE session_id = ? AND name = ?",
            (session_id, lane),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise SessionLaneNotFoundError(
                f"session {session_id!r} lane {lane!r} 不存在"
            )
        return row

    async def _entry_path_rows(
        self, session_id: str, leaf_entry_id: str | None,
    ) -> list[aiosqlite.Row]:
        if leaf_entry_id is None:
            return []
        db = self._require_db()
        cur = await db.execute(
            "WITH RECURSIVE path("
            "id, session_id, seq, parent_id, message_id, role, content_json, "
            "created_at, entry_type, payload_json, global_seq, depth) AS ("
            "SELECT id, session_id, seq, parent_id, message_id, role, content_json, "
            "created_at, entry_type, payload_json, global_seq, 0 FROM session_entries "
            "WHERE session_id = ? AND id = ? "
            "UNION ALL "
            "SELECT parent.id, parent.session_id, parent.seq, parent.parent_id, "
            "parent.message_id, parent.role, parent.content_json, parent.created_at, "
            "parent.entry_type, parent.payload_json, parent.global_seq, "
            "path.depth + 1 FROM session_entries AS parent "
            "JOIN path ON parent.id = path.parent_id "
            "WHERE parent.session_id = ?"
            ") SELECT id, session_id, seq, parent_id, message_id, role, "
            "content_json, created_at, entry_type, payload_json, global_seq "
            "FROM path ORDER BY depth DESC",
            (session_id, leaf_entry_id, session_id),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        if not rows or rows[-1]["id"] != leaf_entry_id:
            raise SessionEntryNotFoundError(
                f"entry {leaf_entry_id!r} 不属于 session {session_id!r}"
            )
        return rows

    async def _next_entry_seq(self, session_id: str) -> int:
        db = self._require_db()
        cur = await db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
            "FROM session_entries WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row["next_seq"]) if row is not None else 0

    async def _insert_entry(
        self,
        *,
        session_id: str,
        parent_id: str | None,
        message_id: str,
        role: str,
        content_json: str,
        created_at: int,
        entry_type: str = "message",
        payload: dict[str, Any] | None = None,
        entry_id: str | None = None,
    ) -> tuple[str, int]:
        db = self._require_db()
        resolved_entry_id = entry_id or _gen_id("entry")
        local_seq = await self._next_entry_seq(session_id)
        if payload is None:
            loaded_payload = json.loads(content_json)
            payload = loaded_payload if isinstance(loaded_payload, dict) else {}
        global_seq = await self._append_log_item(
            session_id,
            kind="entry",
            item_id=resolved_entry_id,
            timestamp=created_at,
            payload={
                "id": resolved_entry_id,
                "type": entry_type,
                "parent_id": parent_id,
                "message_id": message_id,
                "role": role,
                "payload": payload,
            },
        )
        await db.execute(
            "INSERT INTO session_entries "
            "(id, session_id, seq, parent_id, message_id, role, content_json, "
            "created_at, entry_type, payload_json, global_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolved_entry_id,
                session_id,
                local_seq,
                parent_id,
                message_id,
                role,
                content_json,
                created_at,
                entry_type,
                json.dumps(payload, ensure_ascii=False),
                global_seq,
            ),
        )
        await append_entry_to_branch_cache(
            db,
            session_id=session_id,
            entry_id=resolved_entry_id,
            entry_seq=global_seq,
            entry_type=entry_type,
            parent_id=parent_id,
        )
        if entry_type == "message":
            await increment_messages(db, session_id)
            message = _deserialize_message(content_json)
            if isinstance(message, AssistantMessage):
                usage = message.usage
                await add_usage(
                    db,
                    session_id,
                    cached_tokens=usage.cache_read,
                    uncached_tokens=usage.input + usage.cache_write,
                    total_tokens=usage.total_tokens,
                    cost_total=usage.cost.total if usage.cost is not None else 0,
                )
        return resolved_entry_id, global_seq

    async def _materialize_path(
        self, session_id: str, path_rows: list[aiosqlite.Row],
    ) -> None:
        """在当前事务中把 entry path 重建为 legacy ``messages`` 投影。"""
        db = self._require_db()
        await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        idx = 0
        for row in path_rows:
            if str(row["entry_type"]) != "message":
                continue
            await db.execute(
                "INSERT INTO messages "
                "(id, session_id, idx, role, content_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["message_id"], session_id, idx, row["role"],
                    row["content_json"], row["created_at"],
                ),
            )
            idx += 1

    async def _latest_labels(
        self, session_id: str, entry_ids: list[str],
    ) -> dict[str, str | None]:
        if not entry_ids:
            return {}
        db = self._require_db()
        placeholders = ",".join("?" for _ in entry_ids)
        cur = await db.execute(
            "SELECT f.entry_id, f.value_json FROM session_facts AS f "
            "JOIN (SELECT entry_id, MAX(seq) AS max_seq FROM session_facts "
            f"WHERE session_id = ? AND kind = 'label' AND entry_id IN ({placeholders}) "
            "GROUP BY entry_id) AS latest "
            "ON latest.entry_id = f.entry_id AND latest.max_seq = f.seq "
            "WHERE f.session_id = ? AND f.kind = 'label'",
            (session_id, *entry_ids, session_id),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        return {row["entry_id"]: json.loads(row["value_json"]) for row in rows}

    async def _rows_to_entries(
        self, session_id: str, rows: list[aiosqlite.Row],
    ) -> list[SQLiteSessionEntry]:
        labels = await self._latest_labels(
            session_id, [str(row["id"]) for row in rows]
        )
        return [
            SQLiteSessionEntry(
                id=row["id"],
                session_id=row["session_id"],
                seq=(
                    int(row["global_seq"])
                    if row["global_seq"] is not None
                    else int(row["seq"])
                ),
                parent_id=row["parent_id"],
                message_id=row["message_id"],
                role=row["role"],
                message=(
                    _deserialize_message(row["content_json"])
                    if row["entry_type"] == "message"
                    else None
                ),
                created_at=row["created_at"],
                label=labels.get(row["id"]),
                entry_type=row["entry_type"],
                payload=(
                    json.loads(row["payload_json"])
                    if row["payload_json"]
                    else {}
                ),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # session CRUD
    # ------------------------------------------------------------------

    @serialized_operation
    async def create_session(
        self,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> SQLiteSession:
        """创建 session；返回 SQLiteSession。"""
        db = self._require_db()
        now = _now_ms()
        resolved_session_id = session_id or _gen_id("sess")
        explicit_title = title is not None
        title = title if title is not None else "default"
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO sessions "
                    "(id, title, created_at, updated_at, metadata_json, active_lane, "
                    "parent_session_id) VALUES (?, ?, ?, ?, ?, 'main', ?)",
                    (
                        resolved_session_id,
                        title,
                        now,
                        now,
                        meta_json,
                        parent_session_id,
                    ),
                )
                await self._ensure_writer_lease(resolved_session_id)
                await db.execute(
                    "INSERT INTO session_lanes "
                    "(session_id, name, leaf_entry_id, created_at, updated_at) "
                    "VALUES (?, 'main', NULL, ?, ?)",
                    (resolved_session_id, now, now),
                )
                await create_sequence(db, resolved_session_id)
                await create_stats(db, resolved_session_id)
                if explicit_title:
                    await self._append_name_fact(
                        resolved_session_id, title, timestamp=now
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                self._writer_leases.pop(resolved_session_id, None)
                self._writer_ref_counts.pop(resolved_session_id, None)
                self._lost_writer_sessions.discard(resolved_session_id)
                raise
        return SQLiteSession(
            id=resolved_session_id,
            title=title,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
            active_lane="main",
            parent_session_id=parent_session_id,
        )

    @serialized_operation
    async def list_sessions(self) -> list[SQLiteSession]:
        """列出所有 session，按 updated_at 降序。"""
        db = self._require_db()
        cur = await db.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_session(r) for r in rows]

    @serialized_operation
    async def fork_session(
        self,
        source_session_id: str,
        *,
        scope: ForkScope = "branch",
        entry_id: str | None = None,
        position: Literal["before", "at"] = "at",
        session_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SQLiteSession:
        """Atomically fork a branch or complete tree into a new Session."""

        db = self._require_db()
        now = _now_ms()
        target_session_id = session_id or _gen_id("sess")
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._require_session(source_session_id)
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE id = ?", (source_session_id,)
            )
            source_row = await cursor.fetchone()
            await cursor.close()
            if source_row is None:
                raise SessionNotFoundError(
                    f"session {source_session_id!r} 不存在"
                )
            source_metadata = self._row_to_session(source_row)
            source_name = await self.get_session_name(source_session_id)
            if scope == "tree":
                cursor = await db.execute(
                    "SELECT id, parent_id, message_id, role, content_json, created_at, "
                    "entry_type, payload_json FROM session_entries "
                    "WHERE session_id = ? ORDER BY seq",
                    (source_session_id,),
                )
                source_entries = list(await cursor.fetchall())
                await cursor.close()
                cursor = await db.execute(
                    "SELECT name, leaf_entry_id, created_at, updated_at "
                    "FROM session_lanes WHERE session_id = ? ORDER BY created_at, name",
                    (source_session_id,),
                )
                source_lanes = list(await cursor.fetchall())
                await cursor.close()
                target_active_lane = source_metadata.active_lane
            else:
                active = await self._lane_row(
                    source_session_id, source_metadata.active_lane
                )
                selected = entry_id or active["leaf_entry_id"]
                if selected is not None and position == "before":
                    cursor = await db.execute(
                        "SELECT parent_id FROM session_entries "
                        "WHERE session_id = ? AND id = ?",
                        (source_session_id, selected),
                    )
                    selected_row = await cursor.fetchone()
                    await cursor.close()
                    if selected_row is None:
                        raise SessionEntryNotFoundError(
                            f"entry {selected!r} 不属于 source session"
                        )
                    selected = selected_row["parent_id"]
                source_entries = await self._entry_path_rows(
                    source_session_id, selected
                )
                source_lanes = []
                target_active_lane = "main"

            target_title = title if title is not None else source_metadata.title
            target_name = title if title is not None else source_name
            target_metadata = (
                dict(source_metadata.metadata) if metadata is None else dict(metadata)
            )
            await db.execute(
                "INSERT INTO sessions "
                "(id, title, created_at, updated_at, metadata_json, active_lane, "
                "parent_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    target_session_id,
                    target_title,
                    now,
                    now,
                    json.dumps(target_metadata, ensure_ascii=False),
                    target_active_lane,
                    source_session_id,
                ),
            )
            await self._ensure_writer_lease(target_session_id)
            await create_sequence(db, target_session_id)
            await create_stats(db, target_session_id)
            if target_name is not None:
                await self._append_name_fact(
                    target_session_id, target_name, timestamp=now
                )
            await db.execute(
                "INSERT INTO session_lanes "
                "(session_id, name, leaf_entry_id, created_at, updated_at) "
                "VALUES (?, 'main', NULL, ?, ?)",
                (target_session_id, now, now),
            )

            entry_mapping: dict[str, str] = {}
            message_mapping: dict[str, str] = {}
            for row in source_entries:
                old_id = str(row["id"])
                new_entry_id = _gen_id("entry")
                old_parent = row["parent_id"]
                parent_id = (
                    entry_mapping[str(old_parent)] if old_parent is not None else None
                )
                entry_type = str(row["entry_type"])
                payload = (
                    json.loads(row["payload_json"])
                    if row["payload_json"]
                    else {}
                )
                old_message_id = str(row["message_id"])
                new_message_id = message_mapping.setdefault(
                    old_message_id, _gen_id("msg")
                )
                await self._insert_entry(
                    session_id=target_session_id,
                    parent_id=parent_id,
                    message_id=new_message_id,
                    role=str(row["role"]),
                    content_json=str(row["content_json"]),
                    created_at=int(row["created_at"]),
                    entry_type=entry_type,
                    payload=payload if isinstance(payload, dict) else {},
                    entry_id=new_entry_id,
                )
                entry_mapping[old_id] = new_entry_id

            if scope == "tree":
                await db.execute(
                    "DELETE FROM session_lanes WHERE session_id = ?",
                    (target_session_id,),
                )
                for lane in source_lanes:
                    old_leaf = lane["leaf_entry_id"]
                    new_leaf = (
                        entry_mapping[str(old_leaf)] if old_leaf is not None else None
                    )
                    await db.execute(
                        "INSERT INTO session_lanes "
                        "(session_id, name, leaf_entry_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            target_session_id,
                            lane["name"],
                            new_leaf,
                            now,
                            now,
                        ),
                    )
            else:
                leaf = entry_mapping[str(source_entries[-1]["id"])] if source_entries else None
                await db.execute(
                    "UPDATE session_lanes SET leaf_entry_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND name = 'main'",
                    (leaf, now, target_session_id),
                )

            cursor = await db.execute(
                "SELECT entry_id, value_json FROM session_facts AS f "
                "WHERE session_id = ? AND kind = 'label' AND seq = ("
                "SELECT MAX(seq) FROM session_facts AS latest "
                "WHERE latest.session_id = f.session_id "
                "AND latest.entry_id = f.entry_id AND latest.kind = 'label')",
                (source_session_id,),
            )
            source_labels = list(await cursor.fetchall())
            await cursor.close()
            local_fact_seq = 0
            for label in source_labels:
                mapped = entry_mapping.get(str(label["entry_id"]))
                if mapped is None:
                    continue
                value = json.loads(label["value_json"])
                global_seq = await self._append_log_item(
                    target_session_id,
                    kind="fact",
                    item_id=None,
                    timestamp=now,
                    payload={"fact": "label", "target_id": mapped, "value": value},
                )
                await db.execute(
                    "INSERT INTO session_facts "
                    "(id, session_id, seq, kind, entry_id, value_json, created_at, "
                    "global_seq) VALUES (?, ?, ?, 'label', ?, ?, ?, ?)",
                    (
                        _gen_id("fact"),
                        target_session_id,
                        local_fact_seq,
                        mapped,
                        label["value_json"],
                        now,
                        global_seq,
                    ),
                )
                await db.execute(
                    "INSERT INTO session_global_facts "
                    "(session_id, seq, kind, key, value_json, timestamp) "
                    "VALUES (?, ?, 'label', ?, ?, ?)",
                    (
                        target_session_id,
                        global_seq,
                        mapped,
                        label["value_json"],
                        now,
                    ),
                )
                local_fact_seq += 1

            lane = await self._lane_row(target_session_id, target_active_lane)
            active_path = await self._entry_path_rows(
                target_session_id, lane["leaf_entry_id"]
            )
            await self._materialize_path(target_session_id, active_path)
            await db.commit()
        except BaseException:
            await db.rollback()
            self._writer_leases.pop(target_session_id, None)
            self._writer_ref_counts.pop(target_session_id, None)
            self._lost_writer_sessions.discard(target_session_id)
            raise
        result = await self.get_session(target_session_id)
        assert result is not None
        return result

    @serialized_operation
    async def get_session(self, session_id: str) -> SQLiteSession | None:
        """取单个 session；不存在返回 None。"""
        db = self._require_db()
        cur = await db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return self._row_to_session(row) if row is not None else None

    @serialized_operation
    async def rename_session(
        self, session_id: str, title: str,
    ) -> SQLiteSession:
        """重命名 session；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        now = _now_ms()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._require_session(session_id)
            await self._ensure_writer_lease(session_id)
            await db.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, session_id),
            )
            await self._append_name_fact(session_id, title, timestamp=now)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        # 返回最新数据
        result = await self.get_session(session_id)
        assert result is not None  # _require_session 已保证
        result.title = title
        result.updated_at = now
        return result

    @serialized_operation
    async def set_session_name(self, session_id: str, name: str | None) -> None:
        db = self._require_db()
        now = _now_ms()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._ensure_writer_lease(session_id)
            if name is not None:
                await db.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (name, now, session_id),
                )
            else:
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
            await self._append_name_fact(session_id, name, timestamp=now)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    @serialized_operation
    async def delete_session(self, session_id: str) -> None:
        """删除 session + 级联删除 messages / snapshots；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._require_session(session_id)
            await self._ensure_writer_lease(session_id)
            # FK ON DELETE CASCADE 会清 messages / snapshots 及 repository views。
            await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        self._writer_leases.pop(session_id, None)
        self._writer_ref_counts.pop(session_id, None)
        self._lost_writer_sessions.discard(session_id)

    @serialized_operation
    async def touch_session(self, session_id: str) -> None:
        """刷新 updated_at；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._require_session(session_id)
            await self._ensure_writer_lease(session_id)
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_now_ms(), session_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    @serialized_operation
    async def ensure_default_session(
        self, title: str = "default",
    ) -> SQLiteSession:
        """若 sessions 表空则创建 default；返回 default session。"""
        existing = await self.list_sessions()
        if existing:
            return existing[0]
        return await self.create_session(title=title)

    # ------------------------------------------------------------------
    # append-only entry tree / lane API
    # ------------------------------------------------------------------

    @serialized_operation
    async def create_lane(
        self, session_id: str, lane: str, at: str | None
    ) -> SQLiteSessionLane:
        db = self._require_db()
        lane_name = self._validate_lane_name(lane)
        now = _now_ms()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._ensure_writer_lease(session_id)
            if at is not None and await self.get_typed_entry(session_id, at) is None:
                raise SessionEntryNotFoundError(
                    f"entry {at!r} 不属于 session {session_id!r}"
                )
            try:
                await db.execute(
                    "INSERT INTO session_lanes "
                    "(session_id, name, leaf_entry_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, lane_name, at, now, now),
                )
            except aiosqlite.IntegrityError as error:
                raise SessionLaneExistsError(
                    f"session {session_id!r} lane {lane_name!r} 已存在"
                ) from error
            await self._append_lane_move(
                session_id, lane_name, at, timestamp=now
            )
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return SQLiteSessionLane(
            session_id=session_id,
            name=lane_name,
            leaf_entry_id=at,
            created_at=now,
            updated_at=now,
            is_active=False,
        )

    @serialized_operation
    async def move_lane(
        self, session_id: str, lane: str, to: str | None
    ) -> SQLiteSessionLane:
        db = self._require_db()
        lane_name = self._validate_lane_name(lane)
        now = _now_ms()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._ensure_writer_lease(session_id)
            lane_row = await self._lane_row(session_id, lane_name)
            if to is not None and await self.get_typed_entry(session_id, to) is None:
                raise SessionEntryNotFoundError(
                    f"entry {to!r} 不属于 session {session_id!r}"
                )
            await db.execute(
                "UPDATE session_lanes SET leaf_entry_id = ?, updated_at = ? "
                "WHERE session_id = ? AND name = ?",
                (to, now, session_id, lane_name),
            )
            await self._append_lane_move(
                session_id, lane_name, to, timestamp=now
            )
            active_lane = await self._active_lane_name(session_id)
            if lane_name == active_lane:
                path = await self._entry_path_rows(session_id, to)
                await self._materialize_path(session_id, path)
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return SQLiteSessionLane(
            session_id=session_id,
            name=lane_name,
            leaf_entry_id=to,
            created_at=lane_row["created_at"],
            updated_at=now,
            is_active=lane_name == active_lane,
        )

    @serialized_operation
    async def list_lanes(self, session_id: str) -> list[SQLiteSessionLane]:
        """列出 lane；active lane 排在最前，其余按创建时间。"""
        db = self._require_db()
        active_lane = await self._active_lane_name(session_id)
        cur = await db.execute(
            "SELECT session_id, name, leaf_entry_id, created_at, updated_at "
            "FROM session_lanes WHERE session_id = ? "
            "ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, created_at, name",
            (session_id, active_lane),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        return [
            SQLiteSessionLane(
                session_id=row["session_id"],
                name=row["name"],
                leaf_entry_id=row["leaf_entry_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                is_active=row["name"] == active_lane,
            )
            for row in rows
        ]

    @serialized_operation
    async def get_active_leaf(
        self, session_id: str, lane: str | None = None,
    ) -> str | None:
        """返回 lane 的 active leaf；``lane=None`` 使用 Session active lane。"""
        await self._require_session(session_id)
        lane_name = lane or await self._active_lane_name(session_id)
        row = await self._lane_row(session_id, lane_name)
        value = row["leaf_entry_id"]
        return str(value) if value is not None else None

    @serialized_operation
    async def list_entries(
        self, session_id: str, lane: str | None = None,
    ) -> list[SQLiteSessionEntry]:
        """返回 lane 当前 root-to-leaf 路径。"""
        await self._require_session(session_id)
        lane_name = lane or await self._active_lane_name(session_id)
        lane_row = await self._lane_row(session_id, lane_name)
        rows = await self._entry_path_rows(session_id, lane_row["leaf_entry_id"])
        return await self._rows_to_entries(session_id, rows)

    @serialized_operation
    async def list_all_entries(self, session_id: str) -> list[SQLiteSessionEntry]:
        """按 append seq 返回完整树，包括不再活跃的旧分支。"""
        db = self._require_db()
        await self._require_session(session_id)
        cur = await db.execute(
            "SELECT id, session_id, seq, parent_id, message_id, role, "
            "content_json, created_at, entry_type, payload_json, global_seq "
            "FROM session_entries "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        return await self._rows_to_entries(session_id, rows)

    @staticmethod
    def _row_to_stored_entry(row: aiosqlite.Row) -> StoredSessionEntry:
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except json.JSONDecodeError as error:
            raise SessionSerializationError(
                f"entry {row['id']!r} payload JSON 解析失败"
            ) from error
        if not isinstance(payload, dict):
            raise SessionSerializationError(
                f"entry {row['id']!r} payload 必须是 JSON object"
            )
        if row["global_seq"] is None:
            raise SessionSerializationError(
                f"entry {row['id']!r} 缺少 global sequence"
            )
        return StoredSessionEntry(
            id=str(row["id"]),
            type=str(row["entry_type"]),
            payload=payload,
            terminate=bool(payload.get("terminate", False)),
            seq=int(row["global_seq"]),
            parent_id=(str(row["parent_id"]) if row["parent_id"] is not None else None),
            timestamp=int(row["created_at"]),
        )

    @serialized_operation
    async def get_typed_entry(
        self, session_id: str, entry_id: str
    ) -> StoredSessionEntry | None:
        db = self._require_db()
        await self._require_session(session_id)
        cursor = await db.execute(
            "SELECT id, parent_id, entry_type, payload_json, global_seq, created_at "
            "FROM session_entries WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_stored_entry(row) if row is not None else None

    @staticmethod
    def _entry_query_sql(
        query: EntryQuery,
        *,
        prefix: str = "",
    ) -> tuple[list[str], list[object]]:
        predicates: list[str] = []
        params: list[object] = []
        if query.type is not None:
            predicates.append(f"{prefix}entry_type = ?")
            params.append(query.type)
        if query.custom_type is not None:
            predicates.append(
                f"json_extract({prefix}payload_json, '$.custom_type') = ?"
            )
            params.append(query.custom_type)
        if query.cursor is not None:
            operator = ">" if query.order == "oldest_first" else "<"
            predicates.append(f"{prefix}global_seq {operator} ?")
            params.append(query.cursor.after_seq)
        return predicates, params

    @serialized_operation
    async def find_typed_entries(
        self, session_id: str, query: EntryQuery | None = None
    ) -> list[StoredSessionEntry]:
        db = self._require_db()
        await self._require_session(session_id)
        resolved = query or EntryQuery()
        if resolved.limit is not None and resolved.limit <= 0:
            raise ValueError("entry query limit must be positive")
        if resolved.cursor is not None and resolved.cursor.after_seq < 0:
            raise ValueError("entry query cursor must not be negative")
        predicates, params = self._entry_query_sql(resolved)
        where = " AND ".join(["session_id = ?", *predicates])
        direction = "ASC" if resolved.order == "oldest_first" else "DESC"
        sql = (
            "SELECT id, parent_id, entry_type, payload_json, global_seq, created_at "
            f"FROM session_entries WHERE {where} ORDER BY global_seq {direction}"
        )
        values: list[object] = [session_id, *params]
        if resolved.limit is not None:
            sql += " LIMIT ?"
            values.append(resolved.limit)
        cursor = await db.execute(sql, values)
        rows = list(await cursor.fetchall())
        await cursor.close()
        return [self._row_to_stored_entry(row) for row in rows]

    @serialized_operation
    async def find_typed_entries_on_branch(
        self,
        session_id: str,
        query: EntryQuery,
        bounds: BranchBounds,
    ) -> list[StoredSessionEntry]:
        db = self._require_db()
        await self._require_session(session_id)
        start = bounds.start or await self.get_active_leaf(session_id)
        if start is None:
            return []
        if query.limit is not None and query.limit <= 0:
            raise ValueError("entry query limit must be positive")
        if query.cursor is not None and query.cursor.after_seq < 0:
            raise ValueError("entry query cursor must not be negative")
        cursor = await db.execute(
            "SELECT branch_id, entry_seq FROM branch_entries "
            "WHERE session_id = ? AND entry_id = ? ORDER BY branch_id LIMIT 1",
            (session_id, start),
        )
        branch = await cursor.fetchone()
        await cursor.close()
        if branch is None:
            raise SessionBranchError(
                f"entry {start!r} is missing from the branch cache; repair required"
            )
        lower_bound: int | None = None
        if bounds.stop_at_id is not None:
            cursor = await db.execute(
                "SELECT entry_seq FROM branch_entries WHERE session_id = ? "
                "AND branch_id = ? AND entry_id = ? AND entry_seq <= ?",
                (
                    session_id,
                    branch["branch_id"],
                    bounds.stop_at_id,
                    branch["entry_seq"],
                ),
            )
            stop = await cursor.fetchone()
            await cursor.close()
            if stop is None:
                raise SessionBranchError(
                    f"stop entry {bounds.stop_at_id!r} is not on the selected branch"
                )
            lower_bound = int(stop["entry_seq"])
        elif bounds.stop_at_type is not None:
            cursor = await db.execute(
                "SELECT MAX(entry_seq) AS entry_seq FROM branch_entries "
                "WHERE session_id = ? AND branch_id = ? AND entry_type = ? "
                "AND entry_seq <= ?",
                (
                    session_id,
                    branch["branch_id"],
                    bounds.stop_at_type,
                    branch["entry_seq"],
                ),
            )
            stop = await cursor.fetchone()
            await cursor.close()
            if stop is not None and stop["entry_seq"] is not None:
                lower_bound = int(stop["entry_seq"])

        predicates, params = self._entry_query_sql(query, prefix="e.")
        predicates.extend(["b.session_id = ?", "b.branch_id = ?", "b.entry_seq <= ?"])
        values: list[object] = [
            *params,
            session_id,
            branch["branch_id"],
            branch["entry_seq"],
        ]
        if lower_bound is not None:
            predicates.append("b.entry_seq >= ?")
            values.append(lower_bound)
        direction = "ASC" if query.order == "oldest_first" else "DESC"
        sql = (
            "SELECT e.id, e.parent_id, e.entry_type, e.payload_json, "
            "e.global_seq, e.created_at FROM branch_entries AS b "
            "JOIN session_entries AS e ON e.session_id = b.session_id "
            "AND e.id = b.entry_id WHERE "
            + " AND ".join(predicates)
            + f" ORDER BY b.entry_seq {direction}"
        )
        if query.limit is not None:
            sql += " LIMIT ?"
            values.append(query.limit)
        cursor = await db.execute(sql, values)
        rows = list(await cursor.fetchall())
        await cursor.close()
        return [self._row_to_stored_entry(row) for row in rows]

    @serialized_operation
    async def append_typed_entry(
        self,
        session_id: str,
        entry: NewSessionEntry,
        *,
        lane: str = "main",
    ) -> StoredSessionEntry:
        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                lane_row = await self._lane_row(session_id, lane)
                if await id_exists_in_entries(
                    db, entry.id
                ) or await id_exists_in_records(db, session_id, entry.id):
                    raise SQLiteSessionError(f"entry id already exists: {entry.id}")
                payload = dict(entry.payload)
                if entry.terminate:
                    payload["terminate"] = True
                entry_id, seq = await self._insert_entry(
                    session_id=session_id,
                    parent_id=lane_row["leaf_entry_id"],
                    message_id=entry.id,
                    role=entry.type,
                    content_json=json.dumps(payload, ensure_ascii=False),
                    created_at=now,
                    entry_type=entry.type,
                    payload=payload,
                    entry_id=entry.id,
                )
                await db.execute(
                    "UPDATE session_lanes SET leaf_entry_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND name = ?",
                    (entry_id, now, session_id, lane),
                )
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return StoredSessionEntry(
            id=entry_id,
            type=entry.type,
            payload=payload,
            terminate=entry.terminate,
            seq=seq,
            parent_id=lane_row["leaf_entry_id"],
            timestamp=now,
        )

    @serialized_operation
    async def append_entry(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        lane: str | None = None,
    ) -> SQLiteSessionEntry:
        """向 lane leaf 追加 immutable entry，并原子移动 leaf。"""
        db = self._require_db()
        role = getattr(message, "role", "custom") or "custom"
        content_json = _serialize_message(message)
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                active_lane = await self._active_lane_name(session_id)
                lane_name = lane or active_lane
                lane_row = await self._lane_row(session_id, lane_name)
                message_id = _gen_id("msg")
                entry_id, seq = await self._insert_entry(
                    session_id=session_id,
                    parent_id=lane_row["leaf_entry_id"],
                    message_id=message_id,
                    role=role,
                    content_json=content_json,
                    created_at=now,
                )
                await db.execute(
                    "UPDATE session_lanes SET leaf_entry_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND name = ?",
                    (entry_id, now, session_id, lane_name),
                )
                if lane_name == active_lane:
                    cur = await db.execute(
                        "SELECT COALESCE(MAX(idx), -1) + 1 AS next_idx "
                        "FROM messages WHERE session_id = ?",
                        (session_id,),
                    )
                    idx_row = await cur.fetchone()
                    await cur.close()
                    next_idx = int(idx_row["next_idx"]) if idx_row is not None else 0
                    await db.execute(
                        "INSERT INTO messages "
                        "(id, session_id, idx, role, content_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            message_id, session_id, next_idx, role,
                            content_json, now,
                        ),
                    )
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return SQLiteSessionEntry(
            id=entry_id,
            session_id=session_id,
            seq=seq,
            parent_id=lane_row["leaf_entry_id"],
            message_id=message_id,
            role=role,
            message=message,
            created_at=now,
        )

    @serialized_operation
    async def branch(
        self,
        session_id: str,
        entry_id: str | None,
        *,
        lane: str | None = None,
    ) -> SQLiteSessionLane:
        """把 lane leaf 移到当前路径上的祖先（``None`` 表示根）。"""
        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                active_lane = await self._active_lane_name(session_id)
                lane_name = lane or active_lane
                lane_row = await self._lane_row(session_id, lane_name)
                current_path = await self._entry_path_rows(
                    session_id, lane_row["leaf_entry_id"]
                )
                path_ids = [str(row["id"]) for row in current_path]
                if entry_id is not None and entry_id not in path_ids:
                    raise SessionBranchError(
                        f"entry {entry_id!r} 不在 lane {lane_name!r} 当前路径上"
                    )
                new_path = (
                    current_path[: path_ids.index(entry_id) + 1]
                    if entry_id is not None
                    else []
                )
                await db.execute(
                    "UPDATE session_lanes SET leaf_entry_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND name = ?",
                    (entry_id, now, session_id, lane_name),
                )
                await self._append_lane_move(
                    session_id, lane_name, entry_id, timestamp=now
                )
                if lane_name == active_lane:
                    await self._materialize_path(session_id, new_path)
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return SQLiteSessionLane(
            session_id=session_id,
            name=lane_name,
            leaf_entry_id=entry_id,
            created_at=lane_row["created_at"],
            updated_at=now,
            is_active=lane_name == active_lane,
        )

    @serialized_operation
    async def fork(
        self,
        session_id: str,
        name: str,
        *,
        at_entry_id: str | None,
        source_lane: str | None = None,
        activate: bool = False,
    ) -> SQLiteSessionLane:
        """从 source lane 路径上的 entry 创建新 lane；原 lane 不移动。"""
        db = self._require_db()
        lane_name = self._validate_lane_name(name)
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                active_lane = await self._active_lane_name(session_id)
                source_name = source_lane or active_lane
                source = await self._lane_row(session_id, source_name)
                source_path = await self._entry_path_rows(
                    session_id, source["leaf_entry_id"]
                )
                path_ids = [str(row["id"]) for row in source_path]
                if at_entry_id is not None and at_entry_id not in path_ids:
                    raise SessionBranchError(
                        f"entry {at_entry_id!r} 不在 lane {source_name!r} 当前路径上"
                    )
                try:
                    await db.execute(
                        "INSERT INTO session_lanes "
                        "(session_id, name, leaf_entry_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, lane_name, at_entry_id, now, now),
                    )
                    await self._append_lane_move(
                        session_id, lane_name, at_entry_id, timestamp=now
                    )
                except aiosqlite.IntegrityError as error:
                    raise SessionLaneExistsError(
                        f"session {session_id!r} lane {lane_name!r} 已存在"
                    ) from error
                if activate:
                    new_path = (
                        source_path[: path_ids.index(at_entry_id) + 1]
                        if at_entry_id is not None
                        else []
                    )
                    await db.execute(
                        "UPDATE sessions SET active_lane = ?, updated_at = ? "
                        "WHERE id = ?",
                        (lane_name, now, session_id),
                    )
                    await self._materialize_path(session_id, new_path)
                else:
                    await db.execute(
                        "UPDATE sessions SET updated_at = ? WHERE id = ?",
                        (now, session_id),
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return SQLiteSessionLane(
            session_id=session_id,
            name=lane_name,
            leaf_entry_id=at_entry_id,
            created_at=now,
            updated_at=now,
            is_active=activate,
        )

    @serialized_operation
    async def set_active_lane(
        self, session_id: str, lane: str,
    ) -> SQLiteSessionLane:
        """切换 Session active lane，并原子重建 legacy messages 投影。"""
        db = self._require_db()
        lane_name = self._validate_lane_name(lane)
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                lane_row = await self._lane_row(session_id, lane_name)
                path = await self._entry_path_rows(
                    session_id, lane_row["leaf_entry_id"]
                )
                await self._materialize_path(session_id, path)
                await db.execute(
                    "UPDATE sessions SET active_lane = ?, updated_at = ? WHERE id = ?",
                    (lane_name, now, session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return SQLiteSessionLane(
            session_id=session_id,
            name=lane_name,
            leaf_entry_id=lane_row["leaf_entry_id"],
            created_at=lane_row["created_at"],
            updated_at=lane_row["updated_at"],
            is_active=True,
        )

    @serialized_operation
    async def set_label(
        self, session_id: str, entry_id: str, label: str | None,
    ) -> SQLiteSessionEntry:
        """追加 label fact；``None`` 清除当前 label，但不删除 fact 历史。"""
        db = self._require_db()
        if label is not None:
            label = label.strip()
            if not label or len(label) > 256:
                raise ValueError("label must contain 1..256 non-space characters")
            if any(ord(char) < 32 or ord(char) == 127 for char in label):
                raise ValueError("label must not contain control characters")
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                cur = await db.execute(
                    "SELECT id, session_id, seq, parent_id, message_id, role, "
                    "content_json, created_at, entry_type, payload_json, global_seq "
                    "FROM session_entries "
                    "WHERE session_id = ? AND id = ?",
                    (session_id, entry_id),
                )
                entry_row = await cur.fetchone()
                await cur.close()
                if entry_row is None:
                    raise SessionEntryNotFoundError(
                        f"entry {entry_id!r} 不属于 session {session_id!r}"
                    )
                cur = await db.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
                    "FROM session_facts WHERE session_id = ?",
                    (session_id,),
                )
                seq_row = await cur.fetchone()
                await cur.close()
                fact_seq = int(seq_row["next_seq"]) if seq_row is not None else 0
                global_seq = await self._append_log_item(
                    session_id,
                    kind="fact",
                    item_id=None,
                    timestamp=now,
                    payload={"fact": "label", "target_id": entry_id, "value": label},
                )
                await db.execute(
                    "INSERT INTO session_facts "
                    "(id, session_id, seq, kind, entry_id, value_json, created_at, "
                    "global_seq) VALUES (?, ?, ?, 'label', ?, ?, ?, ?)",
                    (
                        _gen_id("fact"), session_id, fact_seq, entry_id,
                        json.dumps(label, ensure_ascii=False), now, global_seq,
                    ),
                )
                await db.execute(
                    "INSERT INTO session_global_facts "
                    "(session_id, seq, kind, key, value_json, timestamp) "
                    "VALUES (?, ?, 'label', ?, ?, ?)",
                    (
                        session_id,
                        global_seq,
                        entry_id,
                        json.dumps(label, ensure_ascii=False),
                        now,
                    ),
                )
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = (await self._rows_to_entries(session_id, [entry_row]))[0]
        return result

    # ------------------------------------------------------------------
    # message CRUD
    # ------------------------------------------------------------------

    @serialized_operation
    async def append_message(
        self, session_id: str, message: AgentMessage,
    ) -> SQLiteStoredMessage:
        """兼容入口：向 active lane 追加 entry，并返回 messages 投影 row。"""
        db = self._require_db()
        entry = await self.append_entry(session_id, message)
        cur = await db.execute(
            "SELECT idx FROM messages WHERE session_id = ? AND id = ?",
            (session_id, entry.message_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:  # pragma: no cover - transaction invariant
            raise SQLiteSessionError("active lane append did not update messages projection")
        return SQLiteStoredMessage(
            id=entry.message_id,
            session_id=session_id,
            idx=row["idx"],
            role=entry.role,
            message=message,
            created_at=entry.created_at,
        )

    @serialized_operation
    async def list_messages(self, session_id: str) -> list[AgentMessage]:
        """列出 session 所有消息，按 idx 升序；返回强类型对象。

        不存在 session 抛 SessionNotFoundError。
        """
        db = self._require_db()
        await self._require_session(session_id)
        cur = await db.execute(
            "SELECT content_json FROM messages WHERE session_id = ? "
            "ORDER BY idx ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_deserialize_message(r["content_json"]) for r in rows]

    @serialized_operation
    async def list_persisted_messages(
        self, session_id: str
    ) -> list[SQLiteStoredMessage]:
        """D2-6：列出 messages 含 DB row 元数据（id / idx / created_at）。

        与 `list_messages` 不同——返回 `SQLiteStoredMessage` 而非裸 AgentMessage，
        让 Web DTO 能暴露稳定 message_id 给前端（regenerate 路径必需）。

        不存在 session 抛 SessionNotFoundError。
        """
        db = self._require_db()
        await self._require_session(session_id)
        cur = await db.execute(
            "SELECT id, session_id, idx, role, content_json, created_at "
            "FROM messages WHERE session_id = ? ORDER BY idx ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            SQLiteStoredMessage(
                id=r["id"],
                session_id=r["session_id"],
                idx=r["idx"],
                role=r["role"],
                message=_deserialize_message(r["content_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    @serialized_operation
    async def replace_messages(
        self, session_id: str, messages: list[AgentMessage],
    ) -> None:
        """同步 active lane，旧 suffix 作为 immutable branch 保留。

        精确相同前缀复用 entry；首个内容差异之后只 INSERT 新 entry 并移动
        lane leaf。legacy ``messages`` 投影仍遵循 D2 message-id 契约：同 role
        内容修订保留 id / created_at，role mismatch 后使用新 id。
        """
        db = self._require_db()
        now = _now_ms()
        new_messages = list(messages)
        serialized = [
            (
                getattr(message, "role", "custom") or "custom",
                _serialize_message(message),
            )
            for message in new_messages
        ]

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                active_lane = await self._active_lane_name(session_id)
                lane_row = await self._lane_row(session_id, active_lane)
                active_path = await self._entry_path_rows(
                    session_id, lane_row["leaf_entry_id"]
                )
                cur = await db.execute(
                    "SELECT id, idx, role, content_json, created_at "
                    "FROM messages WHERE session_id = ? ORDER BY idx",
                    (session_id,),
                )
                old_rows = list(await cur.fetchall())
                await cur.close()

                tree_prefix = 0
                while tree_prefix < min(len(active_path), len(serialized)):
                    entry = active_path[tree_prefix]
                    role, content_json = serialized[tree_prefix]
                    if entry["role"] != role or entry["content_json"] != content_json:
                        break
                    tree_prefix += 1

                role_mismatch = min(len(old_rows), len(serialized))
                for index in range(role_mismatch):
                    if old_rows[index]["role"] != serialized[index][0]:
                        role_mismatch = index
                        break

                message_ids: list[str] = []
                created_times: list[int] = []
                for index in range(len(serialized)):
                    if index < role_mismatch and index < len(old_rows):
                        message_ids.append(str(old_rows[index]["id"]))
                        created_times.append(int(old_rows[index]["created_at"]))
                    else:
                        message_ids.append(_gen_id("msg"))
                        created_times.append(now)

                parent_id = (
                    str(active_path[tree_prefix - 1]["id"])
                    if tree_prefix > 0
                    else None
                )
                leaf_id = parent_id
                for index in range(tree_prefix, len(serialized)):
                    role, content_json = serialized[index]
                    leaf_id, _ = await self._insert_entry(
                        session_id=session_id,
                        parent_id=leaf_id,
                        message_id=message_ids[index],
                        role=role,
                        content_json=content_json,
                        created_at=created_times[index],
                    )

                await db.execute(
                    "UPDATE session_lanes SET leaf_entry_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND name = ?",
                    (leaf_id, now, session_id, active_lane),
                )
                new_path = await self._entry_path_rows(session_id, leaf_id)
                await self._materialize_path(session_id, new_path)
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    def _row_to_stored_record(row: aiosqlite.Row) -> StoredSessionRecord:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise SessionSerializationError(
                f"record {row['id']!r} payload JSON 解析失败"
            ) from error
        if not isinstance(payload, dict):
            raise SessionSerializationError(
                f"record {row['id']!r} payload 必须是 JSON object"
            )
        return StoredSessionRecord(
            id=str(row["id"]),
            lane=str(row["lane"]),
            type=str(row["type"]),
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            operation_kind=(
                str(row["operation_kind"])
                if row["operation_kind"] is not None
                else None
            ),
            payload=payload,
            seq=int(row["seq"]),
            timestamp=int(row["timestamp"]),
        )

    @serialized_operation
    async def append_typed_record(
        self, session_id: str, record: NewSessionRecord
    ) -> StoredSessionRecord:
        db = self._require_db()
        now = _now_ms()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._ensure_writer_lease(session_id)
            lane = await self._lane_row(session_id, record.lane)
            if await id_exists_in_entries(
                db, record.id
            ) or await id_exists_in_records(db, session_id, record.id):
                raise SQLiteSessionError(f"record id already exists: {record.id}")
            open_operation_id = lane["open_operation_id"]
            if record.type == "operation_started":
                if open_operation_id is not None:
                    raise SessionOperationConflictError(
                        f"lane {record.lane!r} already has an open operation"
                    )
                await db.execute(
                    "UPDATE session_lanes SET open_operation_id = ? "
                    "WHERE session_id = ? AND name = ?",
                    (record.id, session_id, record.lane),
                )
            elif record.type == "operation_finished":
                if record.run_id is None or open_operation_id != record.run_id:
                    raise SessionOperationConflictError(
                        f"lane {record.lane!r} operation does not match"
                    )
                await db.execute(
                    "UPDATE session_lanes SET open_operation_id = NULL "
                    "WHERE session_id = ? AND name = ?",
                    (session_id, record.lane),
                )
            seq = await self._append_log_item(
                session_id,
                kind="record",
                item_id=record.id,
                timestamp=now,
                payload=record.model_dump(mode="json"),
            )
            payload_json = json.dumps(record.payload, ensure_ascii=False)
            await db.execute(
                "INSERT INTO session_records "
                "(session_id, seq, id, lane, run_id, type, operation_kind, "
                "timestamp, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    record.id,
                    record.lane,
                    record.run_id,
                    record.type,
                    record.operation_kind,
                    now,
                    payload_json,
                ),
            )
            if record.type == "usage":
                usage = record.payload.get("usage", record.payload)
                if isinstance(usage, dict):
                    cost = usage.get("cost")
                    cost_total = (
                        float(cost.get("total", 0) or 0)
                        if isinstance(cost, dict)
                        else 0
                    )
                    await add_usage(
                        db,
                        session_id,
                        cached_tokens=float(usage.get("cache_read", 0) or 0),
                        uncached_tokens=float(usage.get("input", 0) or 0)
                        + float(usage.get("cache_write", 0) or 0),
                        total_tokens=float(usage.get("total_tokens", 0) or 0),
                        cost_total=cost_total,
                    )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return StoredSessionRecord(
            **record.model_dump(), seq=seq, timestamp=now
        )

    @serialized_operation
    async def find_typed_records(
        self, session_id: str, query: RecordQuery | None = None
    ) -> list[StoredSessionRecord]:
        db = self._require_db()
        await self._require_session(session_id)
        resolved = query or RecordQuery()
        if resolved.limit is not None and resolved.limit <= 0:
            raise ValueError("record query limit must be positive")
        if resolved.after_seq is not None and resolved.after_seq < 0:
            raise ValueError("record query after_seq must not be negative")
        if (
            resolved.operation_kind is not None
            and resolved.type != "operation_started"
        ):
            raise ValueError(
                "record query operation_kind requires type='operation_started'"
            )
        predicates = ["session_id = ?"]
        values: list[object] = [session_id]
        for column, value in (
            ("lane", resolved.lane),
            ("type", resolved.type),
            ("run_id", resolved.run_id),
            ("operation_kind", resolved.operation_kind),
        ):
            if value is not None:
                predicates.append(f"{column} = ?")
                values.append(value)
        if resolved.after_seq is not None:
            predicates.append("seq > ?")
            values.append(resolved.after_seq)
        direction = "ASC" if resolved.order == "oldest_first" else "DESC"
        sql = (
            "SELECT id, seq, lane, run_id, type, operation_kind, timestamp, "
            "payload_json FROM session_records WHERE "
            + " AND ".join(predicates)
            + f" ORDER BY seq {direction}"
        )
        if resolved.limit is not None:
            sql += " LIMIT ?"
            values.append(resolved.limit)
        cursor = await db.execute(sql, values)
        rows = list(await cursor.fetchall())
        await cursor.close()
        return [self._row_to_stored_record(row) for row in rows]

    @serialized_operation
    async def find_open_typed_operations(
        self, session_id: str, lane: str, *, limit: int = 2
    ) -> list[StoredSessionRecord]:
        if limit <= 0:
            raise ValueError("open operation query limit must be positive")
        db = self._require_db()
        await self._require_session(session_id)
        cursor = await db.execute(
            "SELECT r.id, r.seq, r.lane, r.run_id, r.type, r.operation_kind, "
            "r.timestamp, r.payload_json FROM session_lanes AS l "
            "JOIN session_records AS r ON r.session_id = l.session_id "
            "AND r.id = l.open_operation_id WHERE l.session_id = ? "
            "AND l.name = ? AND r.type = 'operation_started' "
            "ORDER BY r.seq DESC LIMIT ?",
            (session_id, lane, limit),
        )
        rows = list(await cursor.fetchall())
        await cursor.close()
        return [self._row_to_stored_record(row) for row in rows]

    @serialized_operation
    async def get_session_log(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[SessionLogItem]:
        db = self._require_db()
        await self._require_session(session_id)
        if limit is not None and limit <= 0:
            raise ValueError("session log limit must be positive")
        if after_seq is not None and after_seq < 0:
            raise ValueError("session log after_seq must not be negative")
        predicates = ["session_id = ?"]
        values: list[object] = [session_id]
        if after_seq is not None:
            predicates.append("seq > ?")
            values.append(after_seq)
        sql = (
            "SELECT seq, kind, payload_json FROM session_log WHERE "
            + " AND ".join(predicates)
            + " ORDER BY seq ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        cursor = await db.execute(sql, values)
        rows = list(await cursor.fetchall())
        await cursor.close()
        result: list[SessionLogItem] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as error:
                raise SessionSerializationError(
                    f"session log seq {row['seq']} payload JSON 解析失败"
                ) from error
            if not isinstance(payload, dict):
                raise SessionSerializationError(
                    f"session log seq {row['seq']} payload 必须是 JSON object"
                )
            result.append(
                SessionLogItem(seq=row["seq"], kind=row["kind"], payload=payload)
            )
        return result

    @serialized_operation
    async def get_session_stats(self, session_id: str) -> SessionStats:
        await self._require_session(session_id)
        return await read_stats(self._require_db(), session_id)

    @serialized_operation
    async def get_session_name(self, session_id: str) -> str | None:
        db = self._require_db()
        await self._require_session(session_id)
        cursor = await db.execute(
            "SELECT value_json FROM session_global_facts WHERE session_id = ? "
            "AND kind = 'name' AND key IS NULL ORDER BY seq DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row["value_json"] is None:
            return None
        value = json.loads(row["value_json"])
        if value is not None and not isinstance(value, str):
            raise SessionSerializationError("session name fact must be a string or null")
        return value

    @serialized_operation
    async def get_entry_label(self, session_id: str, entry_id: str) -> str | None:
        await self._require_session(session_id)
        labels = await self._latest_labels(session_id, [entry_id])
        if entry_id not in labels:
            if await self.get_typed_entry(session_id, entry_id) is None:
                raise SessionEntryNotFoundError(
                    f"entry {entry_id!r} 不属于 session {session_id!r}"
                )
            return None
        return labels[entry_id]

    @serialized_operation
    async def repair_branch_cache(self, session_id: str) -> None:
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._ensure_writer_lease(session_id)
            await rebuild_branch_cache(db, session_id)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    # ------------------------------------------------------------------
    # durable lane operations
    # ------------------------------------------------------------------

    async def _next_operation_seq(self, session_id: str) -> int:
        db = self._require_db()
        cur = await db.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq "
            "FROM session_operation_records WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row["next_seq"]) if row is not None else 0

    async def _insert_operation_record(
        self,
        *,
        operation_id: str,
        session_id: str,
        record_type: str,
        payload: dict[str, Any] | None = None,
        created_at: int | None = None,
    ) -> SQLiteOperationRecord:
        """Insert one record inside the caller's active transaction."""
        db = self._require_db()
        now = _now_ms() if created_at is None else created_at
        local_seq = await self._next_operation_seq(session_id)
        record_id = _gen_id("oprec")
        payload_json = _serialize_operation_payload(payload)
        cur = await db.execute(
            "SELECT lane, kind FROM session_operations "
            "WHERE id = ? AND session_id = ?",
            (operation_id, session_id),
        )
        operation_row = await cur.fetchone()
        await cur.close()
        if operation_row is None:
            raise SessionNotFoundError(f"operation {operation_id!r} 不存在")
        global_seq = await self._append_log_item(
            session_id,
            kind="record",
            item_id=record_id,
            timestamp=now,
            payload={
                "id": record_id,
                "type": record_type,
                "lane": operation_row["lane"],
                "run_id": operation_id,
                "operation_kind": operation_row["kind"],
                "payload": dict(payload or {}),
            },
        )
        await db.execute(
            "INSERT INTO session_operation_records "
            "(id, operation_id, session_id, seq, record_type, payload_json, "
            "created_at, global_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                operation_id,
                session_id,
                local_seq,
                record_type,
                payload_json,
                now,
                global_seq,
            ),
        )
        await db.execute(
            "INSERT INTO session_records "
            "(session_id, seq, id, lane, run_id, type, operation_kind, "
            "timestamp, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                global_seq,
                record_id,
                operation_row["lane"],
                operation_id,
                record_type,
                operation_row["kind"],
                now,
                payload_json,
            ),
        )
        return SQLiteOperationRecord(
            id=record_id,
            operation_id=operation_id,
            session_id=session_id,
            seq=global_seq,
            record_type=record_type,
            payload=dict(payload or {}),
            created_at=now,
        )

    async def _operation_from_row(
        self, row: aiosqlite.Row,
    ) -> SQLiteSessionOperation:
        db = self._require_db()
        cur = await db.execute(
            "SELECT id, operation_id, session_id, seq, record_type, "
            "payload_json, created_at, global_seq FROM session_operation_records "
            "WHERE operation_id = ? ORDER BY seq",
            (row["id"],),
        )
        record_rows = list(await cur.fetchall())
        await cur.close()
        records = tuple(
            SQLiteOperationRecord(
                id=record["id"],
                operation_id=record["operation_id"],
                session_id=record["session_id"],
                seq=(
                    int(record["global_seq"])
                    if record["global_seq"] is not None
                    else int(record["seq"])
                ),
                record_type=record["record_type"],
                payload=_deserialize_operation_payload(record["payload_json"]),
                created_at=record["created_at"],
            )
            for record in record_rows
        )
        outcome: OperationOutcome | None = None
        for record in records:
            if record.record_type != "operation_finished":
                continue
            value = record.payload.get("outcome")
            if value not in {
                "completed", "aborted", "failed", "declined", "conflict"
            }:
                raise SessionSerializationError(
                    f"operation {row['id']!r} has invalid outcome {value!r}"
                )
            outcome = value
        return SQLiteSessionOperation(
            id=row["id"],
            session_id=row["session_id"],
            lane=row["lane"],
            kind=row["kind"],
            dedupe_key=row["dedupe_key"],
            source_leaf_id=row["source_leaf_id"],
            payload=_deserialize_operation_payload(row["payload_json"]),
            created_at=row["created_at"],
            records=records,
            outcome=outcome,
        )

    @serialized_operation
    async def get_operation(
        self, operation_id: str,
    ) -> SQLiteSessionOperation | None:
        db = self._require_db()
        cur = await db.execute(
            "SELECT id, session_id, lane, kind, dedupe_key, source_leaf_id, "
            "payload_json, created_at FROM session_operations WHERE id = ?",
            (operation_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return await self._operation_from_row(row) if row is not None else None

    @serialized_operation
    async def list_open_operations(
        self,
        *,
        kind: str | None = None,
        session_id: str | None = None,
    ) -> list[SQLiteSessionOperation]:
        """Return operations without an ``operation_finished`` record."""
        db = self._require_db()
        clauses = [
            "NOT EXISTS (SELECT 1 FROM session_operation_records AS r "
            "WHERE r.operation_id = o.id AND r.record_type = "
            "'operation_finished')"
        ]
        params: list[Any] = []
        if kind is not None:
            clauses.append("o.kind = ?")
            params.append(kind)
        if session_id is not None:
            clauses.append("o.session_id = ?")
            params.append(session_id)
        cur = await db.execute(
            "SELECT o.id, o.session_id, o.lane, o.kind, o.dedupe_key, "
            "o.source_leaf_id, o.payload_json, o.created_at "
            "FROM session_operations AS o WHERE "
            + " AND ".join(clauses)
            + " ORDER BY o.created_at, o.id",
            tuple(params),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        return [await self._operation_from_row(row) for row in rows]

    @serialized_operation
    async def get_latest_operation(
        self,
        *,
        kind: str,
        session_id: str,
        lane: str | None = None,
    ) -> SQLiteSessionOperation | None:
        """Return the newest operation intent for one Session lane, including finished ones."""
        db = self._require_db()
        lane_name = lane or await self._active_lane_name(session_id)
        cur = await db.execute(
            "SELECT id, session_id, lane, kind, dedupe_key, source_leaf_id, "
            "payload_json, created_at FROM session_operations "
            "WHERE session_id = ? AND lane = ? AND kind = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id, lane_name, kind),
        )
        row = await cur.fetchone()
        await cur.close()
        return await self._operation_from_row(row) if row is not None else None

    @serialized_operation
    async def start_operation(
        self,
        session_id: str,
        *,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
        lane: str | None = None,
        operation_id: str | None = None,
    ) -> SQLiteSessionOperation:
        """Persist an operation intent before any external effect starts.

        One lane may have only one open operation. A retry with the same
        ``kind`` and ``dedupe_key`` resumes that operation instead of creating
        a second intent.
        """
        db = self._require_db()
        if not kind.strip() or not dedupe_key:
            raise ValueError("operation kind and dedupe_key must be non-empty")
        payload_json = _serialize_operation_payload(payload)
        created_id = operation_id or _gen_id("op")
        now = _now_ms()
        reused_id: str | None = None
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_writer_lease(session_id)
                lane_name = lane or await self._active_lane_name(session_id)
                lane_row = await self._lane_row(session_id, lane_name)
                cur = await db.execute(
                    "SELECT o.id, o.kind, o.dedupe_key, o.source_leaf_id "
                    "FROM session_operations AS o "
                    "WHERE o.session_id = ? AND o.lane = ? AND NOT EXISTS "
                    "(SELECT 1 FROM session_operation_records AS r "
                    "WHERE r.operation_id = o.id AND r.record_type = "
                    "'operation_finished') ORDER BY o.created_at LIMIT 1",
                    (session_id, lane_name),
                )
                open_row = await cur.fetchone()
                await cur.close()
                if open_row is not None:
                    if (
                        open_row["kind"] == kind
                        and open_row["dedupe_key"] == dedupe_key
                        and open_row["source_leaf_id"]
                        == lane_row["leaf_entry_id"]
                    ):
                        reused_id = str(open_row["id"])
                        await db.commit()
                    else:
                        raise SessionOperationConflictError(
                            f"session {session_id!r} lane {lane_name!r} already "
                            f"has open operation {open_row['id']!r}"
                        )
                else:
                    await db.execute(
                        "INSERT INTO session_operations "
                        "(id, session_id, lane, kind, dedupe_key, "
                        "source_leaf_id, payload_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            created_id,
                            session_id,
                            lane_name,
                            kind,
                            dedupe_key,
                            lane_row["leaf_entry_id"],
                            payload_json,
                            now,
                        ),
                    )
                    await self._insert_operation_record(
                        operation_id=created_id,
                        session_id=session_id,
                        record_type="operation_started",
                        payload=payload,
                        created_at=now,
                    )
                    await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = await self.get_operation(reused_id or created_id)
        assert result is not None
        return result

    @serialized_operation
    async def mark_operation_effect_committed(
        self,
        operation_id: str,
        payload: dict[str, Any] | None = None,
    ) -> SQLiteOperationRecord:
        """Append the external-effect evidence once; repeated calls are safe."""
        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                operation = await self.get_operation(operation_id)
                if operation is None:
                    raise SessionNotFoundError(
                        f"operation {operation_id!r} 不存在"
                    )
                await self._ensure_writer_lease(operation.session_id)
                existing = next(
                    (
                        record for record in operation.records
                        if record.record_type == "effect_committed"
                    ),
                    None,
                )
                if existing is not None:
                    await db.commit()
                    return existing
                if not operation.is_open:
                    raise SessionOperationConflictError(
                        f"operation {operation_id!r} is already finished"
                    )
                record = await self._insert_operation_record(
                    operation_id=operation.id,
                    session_id=operation.session_id,
                    record_type="effect_committed",
                    payload=payload,
                )
                await db.commit()
                return record
            except BaseException:
                await db.rollback()
                raise

    @serialized_operation
    async def finish_operation(
        self,
        operation_id: str,
        *,
        outcome: OperationOutcome,
        payload: dict[str, Any] | None = None,
    ) -> SQLiteSessionOperation:
        """Append one terminal record without changing the conversation tree."""
        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                operation = await self.get_operation(operation_id)
                if operation is None:
                    raise SessionNotFoundError(
                        f"operation {operation_id!r} 不存在"
                    )
                await self._ensure_writer_lease(operation.session_id)
                if operation.outcome is not None:
                    await db.commit()
                    return operation
                finish_payload = dict(payload or {})
                finish_payload["outcome"] = outcome
                await self._insert_operation_record(
                    operation_id=operation.id,
                    session_id=operation.session_id,
                    record_type="operation_finished",
                    payload=finish_payload,
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = await self.get_operation(operation_id)
        assert result is not None
        return result

    @serialized_operation
    async def complete_operation_and_reset_lane(
        self, operation_id: str,
    ) -> SQLiteSessionOperation:
        """Atomically clear the operation lane and append completed.

        The lane must still point at the immutable source leaf captured by
        ``operation_started``. A later append therefore turns recovery into an
        explicit conflict instead of deleting newer messages.
        """
        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                operation = await self.get_operation(operation_id)
                if operation is None:
                    raise SessionNotFoundError(
                        f"operation {operation_id!r} 不存在"
                    )
                await self._ensure_writer_lease(operation.session_id)
                if operation.outcome is not None:
                    if operation.outcome == "completed":
                        await db.commit()
                        return operation
                    raise SessionOperationConflictError(
                        f"operation {operation_id!r} finished as "
                        f"{operation.outcome!r}"
                    )
                if not operation.effect_committed:
                    raise SessionOperationConflictError(
                        f"operation {operation_id!r} has no committed effect"
                    )
                lane_row = await self._lane_row(
                    operation.session_id, operation.lane
                )
                current_leaf = lane_row["leaf_entry_id"]
                if current_leaf != operation.source_leaf_id:
                    raise SessionOperationConflictError(
                        f"operation {operation_id!r} source leaf changed"
                    )
                await db.execute(
                    "UPDATE session_lanes SET leaf_entry_id = NULL, "
                    "updated_at = ? WHERE session_id = ? AND name = ?",
                    (now, operation.session_id, operation.lane),
                )
                await self._append_lane_move(
                    operation.session_id, operation.lane, None, timestamp=now
                )
                active_lane = await self._active_lane_name(operation.session_id)
                if active_lane == operation.lane:
                    await self._materialize_path(operation.session_id, [])
                await self._insert_operation_record(
                    operation_id=operation.id,
                    session_id=operation.session_id,
                    record_type="operation_finished",
                    payload={"outcome": "completed"},
                    created_at=now,
                )
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, operation.session_id),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = await self.get_operation(operation_id)
        assert result is not None
        return result

    # ------------------------------------------------------------------
    # snapshot CRUD
    # ------------------------------------------------------------------

    @serialized_operation
    async def append_snapshot(
        self, session_id: str, snapshot: RequestSnapshot,
    ) -> SQLiteStoredSnapshot:
        """追加 snapshot；不存在 session 抛 SessionNotFoundError。"""
        db = self._require_db()
        snap_id = _gen_id("snap")
        now = _now_ms()
        content_json = _serialize_snapshot(snapshot)
        try:
            await db.execute("BEGIN IMMEDIATE")
            await self._require_session(session_id)
            await self._ensure_writer_lease(session_id)
            await db.execute(
                "INSERT INTO snapshots "
                "(id, session_id, turn_id, content_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (snap_id, session_id, snapshot.request_id, content_json, now),
            )
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return SQLiteStoredSnapshot(
            id=snap_id, session_id=session_id,
            turn_id=snapshot.request_id,
            snapshot=snapshot, created_at=now,
        )

    @serialized_operation
    async def list_snapshots(self, session_id: str) -> list[RequestSnapshot]:
        """列出 session 所有 snapshots，按 created_at 升序；返回强类型对象。

        不存在 session 抛 SessionNotFoundError。
        """
        db = self._require_db()
        await self._require_session(session_id)
        cur = await db.execute(
            "SELECT content_json FROM snapshots WHERE session_id = ? "
            "ORDER BY created_at ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_deserialize_snapshot(r["content_json"]) for r in rows]


class SQLiteSessionStorage:
    """An opened Session handle with shared, fenced writer ownership."""

    def __init__(
        self,
        store: SQLiteSessionStore,
        session_id: str,
        *,
        on_release: Callable[[SQLiteSessionStorage], None] | None = None,
    ) -> None:
        self._store = store
        self.session_id = session_id
        self._on_release = on_release
        self._released = False

    def _require_open(self) -> None:
        if self._released:
            raise SQLiteSessionError(f"session storage {self.session_id!r} is closed")

    async def get_metadata(self) -> SessionMetadata:
        self._require_open()
        session = await self._store.get_session(self.session_id)
        if session is None:
            raise SessionNotFoundError(f"session {self.session_id!r} 不存在")
        return SessionMetadata(
            id=session.id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            parent_session_id=session.parent_session_id,
            title=session.title,
            metadata=session.metadata,
        )

    async def get_lanes(self) -> list[LanePointer]:
        self._require_open()
        lanes = await self._store.list_lanes(self.session_id)
        return [LanePointer(lane=lane.name, leaf_id=lane.leaf_entry_id) for lane in lanes]

    async def create_lane(self, lane: str, at: str | None) -> None:
        self._require_open()
        await self._store.create_lane(self.session_id, lane, at)

    async def move_lane(self, lane: str, to: str | None) -> None:
        self._require_open()
        await self._store.move_lane(self.session_id, lane, to)

    async def append_entry(
        self, entry: NewSessionEntry, lane: str
    ) -> StoredSessionEntry:
        self._require_open()
        return await self._store.append_typed_entry(
            self.session_id, entry, lane=lane
        )

    async def append_record(
        self, record: NewSessionRecord
    ) -> StoredSessionRecord:
        self._require_open()
        return await self._store.append_typed_record(self.session_id, record)

    async def get_entry(self, entry_id: str) -> StoredSessionEntry | None:
        self._require_open()
        return await self._store.get_typed_entry(self.session_id, entry_id)

    async def find_entries(
        self, query: EntryQuery | None = None
    ) -> list[StoredSessionEntry]:
        self._require_open()
        return await self._store.find_typed_entries(self.session_id, query)

    async def find_entries_on_branch(
        self, query: EntryQuery, bounds: BranchBounds
    ) -> list[StoredSessionEntry]:
        self._require_open()
        return await self._store.find_typed_entries_on_branch(
            self.session_id, query, bounds
        )

    async def find_records(
        self, query: RecordQuery | None = None
    ) -> list[StoredSessionRecord]:
        self._require_open()
        return await self._store.find_typed_records(self.session_id, query)

    async def find_open_operations(
        self, lane: str, *, limit: int = 2
    ) -> list[StoredSessionRecord]:
        self._require_open()
        return await self._store.find_open_typed_operations(
            self.session_id, lane, limit=limit
        )

    async def get_log(
        self, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[SessionLogItem]:
        self._require_open()
        return await self._store.get_session_log(
            self.session_id, after_seq=after_seq, limit=limit
        )

    async def get_name(self) -> str | None:
        self._require_open()
        return await self._store.get_session_name(self.session_id)

    async def set_name(self, name: str | None) -> None:
        self._require_open()
        await self._store.set_session_name(self.session_id, name)

    async def get_label(self, entry_id: str) -> str | None:
        self._require_open()
        return await self._store.get_entry_label(self.session_id, entry_id)

    async def set_label(self, entry_id: str, label: str | None) -> None:
        self._require_open()
        await self._store.set_label(self.session_id, entry_id, label)

    async def get_stats(self) -> SessionStats:
        self._require_open()
        return await self._store.get_session_stats(self.session_id)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self._store.release_session_writer(self.session_id)
        finally:
            if self._on_release is not None:
                self._on_release(self)


class SQLiteSessionRepository:
    """Session catalog that owns one shared SQLite connection."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        writer_lease_ttl_ms: int = 30_000,
        writer_heartbeat_interval_ms: int | None = None,
        store: SQLiteSessionStore | None = None,
    ) -> None:
        self.store = store or SQLiteSessionStore(
            db_path,
            writer_lease_ttl_ms=writer_lease_ttl_ms,
            writer_heartbeat_interval_ms=writer_heartbeat_interval_ms,
        )
        self._owns_store = store is None
        self._active_storages: set[SQLiteSessionStorage] = set()
        self._initialized = store is not None and store.connection is not None
        self._closed = False

    async def init(self) -> None:
        if self._closed:
            raise SQLiteSessionError("repository is closed")
        if not self._initialized:
            await self.store.init()
            self._initialized = True

    async def _ready(self) -> None:
        await self.init()

    def _storage(self, session_id: str) -> SQLiteSessionStorage:
        storage = SQLiteSessionStorage(
            self.store,
            session_id,
            on_release=self._active_storages.discard,
        )
        self._active_storages.add(storage)
        return storage

    async def create(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
        parent_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SQLiteSessionStorage:
        await self._ready()
        session = await self.store.create_session(
            title=title,
            metadata=metadata,
            session_id=session_id,
            parent_session_id=parent_session_id,
        )
        await self.store.claim_session_writer(session.id)
        return self._storage(session.id)

    async def open(self, metadata: SessionMetadata) -> SQLiteSessionStorage:
        await self._ready()
        session = await self.store.get_session(metadata.id)
        if session is None:
            raise SessionNotFoundError(f"session {metadata.id!r} 不存在")
        await self.store.claim_session_writer(metadata.id)
        return self._storage(metadata.id)

    async def list(self) -> list[SessionMetadata]:
        await self._ready()
        sessions = await self.store.list_sessions()
        return [
            SessionMetadata(
                id=session.id,
                created_at=session.created_at,
                updated_at=session.updated_at,
                parent_session_id=session.parent_session_id,
                title=session.title,
                metadata=session.metadata,
            )
            for session in sessions
        ]

    async def delete(self, metadata: SessionMetadata) -> None:
        await self._ready()
        for storage in tuple(self._active_storages):
            if storage.session_id == metadata.id:
                await storage.release()
        await self.store.delete_session(metadata.id)

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
    ) -> SQLiteSessionStorage:
        await self._ready()
        session = await self.store.fork_session(
            source.id,
            scope=scope,
            entry_id=entry_id,
            position=position,
            session_id=session_id,
            title=title,
            metadata=metadata,
        )
        await self.store.claim_session_writer(session.id)
        return self._storage(session.id)

    async def repair_branch_cache(self, metadata: SessionMetadata) -> None:
        await self._ready()
        for storage in tuple(self._active_storages):
            if storage.session_id == metadata.id:
                await storage.release()
        await self.store.repair_branch_cache(metadata.id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for storage in tuple(self._active_storages):
            try:
                await storage.release()
            except Exception as error:
                errors.append(error)
        if self._owns_store:
            try:
                await self.store.close()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("SQLite Session repository close failed", errors)

    async def __aenter__(self) -> SQLiteSessionRepository:
        await self.init()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()


__all__ = [
    # 异常
    "SQLiteSessionError",
    "SessionNotFoundError",
    "SessionSerializationError",
    "SessionLaneNotFoundError",
    "SessionLaneExistsError",
    "SessionEntryNotFoundError",
    "SessionBranchError",
    "SessionOperationConflictError",
    "WriterLeaseError",
    # 数据模型
    "SQLiteSession",
    "SQLiteStoredMessage",
    "SQLiteStoredSnapshot",
    "SQLiteSessionEntry",
    "SQLiteSessionLane",
    "SQLiteOperationRecord",
    "SQLiteSessionOperation",
    "OperationOutcome",
    # 主类
    "SQLiteSessionRepository",
    "SQLiteSessionStorage",
    "SQLiteSessionStore",
]
