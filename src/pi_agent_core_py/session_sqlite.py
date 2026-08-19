"""SQLiteSessionStore —— sqlite append-only session tree 存储。

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

异常体系：
- `SQLiteSessionError` 基类
- `SessionNotFoundError` —— rename/delete/append/list 对不存在 session
- `SessionSerializationError` —— JSON 反序列化失败 / 未知 type

不实现：
- durable operation replay（后续独立实现）
- 自动 compaction（由 harness.compact_* 触发；sqlite 仅持久化）
- 跨进程共享（单进程场景；多进程需要单独协调）
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel

from .messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    SummaryMessage,
    ToolResultMessage,
    UserMessage,
)
from .snapshot import RequestSnapshot

# ============================================================================
# 异常
# ============================================================================


class SQLiteSessionError(Exception):
    """sqlite session 模块基类异常。"""


class SessionNotFoundError(SQLiteSessionError):
    """rename/delete/append/list 对不存在 session 抛。"""


class SessionSerializationError(SQLiteSessionError):
    """JSON 反序列化失败 / 未知 message type 抛。"""


class SessionLaneNotFoundError(SQLiteSessionError):
    """请求的 lane 不存在。"""


class SessionLaneExistsError(SQLiteSessionError):
    """创建已存在的 lane。"""


class SessionEntryNotFoundError(SQLiteSessionError):
    """请求的 entry 不存在或不属于目标 Session。"""


class SessionBranchError(SQLiteSessionError):
    """branch/fork 目标不在 source lane 当前路径上。"""


# ============================================================================
# Pydantic 数据模型（用于对外返回，类型稳定）
# ============================================================================


class SQLiteSession(BaseModel):
    """session 元信息（不含 messages / snapshots）。"""

    id: str
    title: str
    created_at: int
    updated_at: int
    metadata: dict[str, Any] = {}
    active_lane: str = "main"


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
    message: AgentMessage
    created_at: int
    label: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class SQLiteSessionLane(BaseModel):
    """命名 lane 及其当前 leaf。"""

    session_id: str
    name: str
    leaf_entry_id: str | None
    created_at: int
    updated_at: int
    is_active: bool = False


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

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._closed: bool = False
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """打开连接 + 建 schema + 设 PRAGMA。幂等。

        若已 init 过则 no-op。重复调安全。
        """
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
            await db.execute("PRAGMA journal_mode=WAL")
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

    async def _exec_schema(self) -> None:
        assert self._db is not None
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id              TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                metadata_json   TEXT NOT NULL DEFAULT '{}',
                active_lane     TEXT NOT NULL DEFAULT 'main'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                idx             INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content_json    TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                UNIQUE (session_id, idx)
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id              TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                turn_id         TEXT,
                content_json    TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS session_entries (
                id              TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                parent_id       TEXT,
                message_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content_json    TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES session_entries(id),
                UNIQUE (session_id, seq)
            );

            CREATE TABLE IF NOT EXISTS session_lanes (
                session_id      TEXT NOT NULL,
                name            TEXT NOT NULL,
                leaf_entry_id   TEXT,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                PRIMARY KEY (session_id, name),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (leaf_entry_id) REFERENCES session_entries(id)
            );

            CREATE TABLE IF NOT EXISTS session_facts (
                id              TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                seq             INTEGER NOT NULL,
                kind            TEXT NOT NULL,
                entry_id        TEXT NOT NULL,
                value_json      TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (entry_id) REFERENCES session_entries(id),
                UNIQUE (session_id, seq)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session_idx
                ON messages(session_id, idx);

            CREATE INDEX IF NOT EXISTS idx_snapshots_session_created
                ON snapshots(session_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_sessions_updated
                ON sessions(updated_at);

            CREATE INDEX IF NOT EXISTS idx_session_entries_parent
                ON session_entries(session_id, parent_id);

            CREATE INDEX IF NOT EXISTS idx_session_entries_message
                ON session_entries(session_id, message_id);

            CREATE INDEX IF NOT EXISTS idx_session_facts_entry
                ON session_facts(session_id, entry_id, kind, seq);
            """
        )
        await self._migrate_session_tree()

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

    async def close(self) -> None:
        """关闭连接。幂等。"""
        if self._closed or self._db is None:
            self._closed = True
            return
        try:
            await self._db.close()
        finally:
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
        cur = await db.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise SessionNotFoundError(f"session {session_id!r} 不存在")

    @staticmethod
    def _row_to_session(row: aiosqlite.Row) -> SQLiteSession:
        try:
            metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        except json.JSONDecodeError:
            metadata = {}
        return SQLiteSession(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=metadata,
            active_lane=row["active_lane"],
        )

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
            "SELECT session_id, name, leaf_entry_id, created_at, updated_at "
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
            "created_at, depth) AS ("
            "SELECT id, session_id, seq, parent_id, message_id, role, content_json, "
            "created_at, 0 FROM session_entries "
            "WHERE session_id = ? AND id = ? "
            "UNION ALL "
            "SELECT parent.id, parent.session_id, parent.seq, parent.parent_id, "
            "parent.message_id, parent.role, parent.content_json, parent.created_at, "
            "path.depth + 1 FROM session_entries AS parent "
            "JOIN path ON parent.id = path.parent_id "
            "WHERE parent.session_id = ?"
            ") SELECT id, session_id, seq, parent_id, message_id, role, "
            "content_json, created_at FROM path ORDER BY depth DESC",
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
    ) -> tuple[str, int]:
        db = self._require_db()
        entry_id = _gen_id("entry")
        seq = await self._next_entry_seq(session_id)
        await db.execute(
            "INSERT INTO session_entries "
            "(id, session_id, seq, parent_id, message_id, role, content_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry_id, session_id, seq, parent_id, message_id, role,
                content_json, created_at,
            ),
        )
        return entry_id, seq

    async def _materialize_path(
        self, session_id: str, path_rows: list[aiosqlite.Row],
    ) -> None:
        """在当前事务中把 entry path 重建为 legacy ``messages`` 投影。"""
        db = self._require_db()
        await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        for idx, row in enumerate(path_rows):
            await db.execute(
                "INSERT INTO messages "
                "(id, session_id, idx, role, content_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["message_id"], session_id, idx, row["role"],
                    row["content_json"], row["created_at"],
                ),
            )

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
                seq=row["seq"],
                parent_id=row["parent_id"],
                message_id=row["message_id"],
                role=row["role"],
                message=_deserialize_message(row["content_json"]),
                created_at=row["created_at"],
                label=labels.get(row["id"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # session CRUD
    # ------------------------------------------------------------------

    async def create_session(
        self,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SQLiteSession:
        """创建 session；返回 SQLiteSession。"""
        db = self._require_db()
        now = _now_ms()
        session_id = _gen_id("sess")
        title = title or "default"
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO sessions "
                    "(id, title, created_at, updated_at, metadata_json, active_lane) "
                    "VALUES (?, ?, ?, ?, ?, 'main')",
                    (session_id, title, now, now, meta_json),
                )
                await db.execute(
                    "INSERT INTO session_lanes "
                    "(session_id, name, leaf_entry_id, created_at, updated_at) "
                    "VALUES (?, 'main', NULL, ?, ?)",
                    (session_id, now, now),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return SQLiteSession(
            id=session_id, title=title, created_at=now, updated_at=now,
            metadata=dict(metadata or {}), active_lane="main",
        )

    async def list_sessions(self) -> list[SQLiteSession]:
        """列出所有 session，按 updated_at 降序。"""
        db = self._require_db()
        cur = await db.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_session(r) for r in rows]

    async def get_session(self, session_id: str) -> SQLiteSession | None:
        """取单个 session；不存在返回 None。"""
        db = self._require_db()
        cur = await db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return self._row_to_session(row) if row is not None else None

    async def rename_session(
        self, session_id: str, title: str,
    ) -> SQLiteSession:
        """重命名 session；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        await self._require_session(session_id)
        now = _now_ms()
        await db.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, session_id),
        )
        await db.commit()
        # 返回最新数据
        result = await self.get_session(session_id)
        assert result is not None  # _require_session 已保证
        result.title = title
        result.updated_at = now
        return result

    async def delete_session(self, session_id: str) -> None:
        """删除 session + 级联删除 messages / snapshots；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        await self._require_session(session_id)
        # FK ON DELETE CASCADE 会清 messages / snapshots
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()

    async def touch_session(self, session_id: str) -> None:
        """刷新 updated_at；不存在抛 SessionNotFoundError。"""
        db = self._require_db()
        await self._require_session(session_id)
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (_now_ms(), session_id),
        )
        await db.commit()

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

    async def get_active_leaf(
        self, session_id: str, lane: str | None = None,
    ) -> str | None:
        """返回 lane 的 active leaf；``lane=None`` 使用 Session active lane。"""
        await self._require_session(session_id)
        lane_name = lane or await self._active_lane_name(session_id)
        row = await self._lane_row(session_id, lane_name)
        value = row["leaf_entry_id"]
        return str(value) if value is not None else None

    async def list_entries(
        self, session_id: str, lane: str | None = None,
    ) -> list[SQLiteSessionEntry]:
        """返回 lane 当前 root-to-leaf 路径。"""
        await self._require_session(session_id)
        lane_name = lane or await self._active_lane_name(session_id)
        lane_row = await self._lane_row(session_id, lane_name)
        rows = await self._entry_path_rows(session_id, lane_row["leaf_entry_id"])
        return await self._rows_to_entries(session_id, rows)

    async def list_all_entries(self, session_id: str) -> list[SQLiteSessionEntry]:
        """按 append seq 返回完整树，包括不再活跃的旧分支。"""
        db = self._require_db()
        await self._require_session(session_id)
        cur = await db.execute(
            "SELECT id, session_id, seq, parent_id, message_id, role, "
            "content_json, created_at FROM session_entries "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        rows = list(await cur.fetchall())
        await cur.close()
        return await self._rows_to_entries(session_id, rows)

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
            except Exception:
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
                if lane_name == active_lane:
                    await self._materialize_path(session_id, new_path)
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except Exception:
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
            except Exception:
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
            except Exception:
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
                cur = await db.execute(
                    "SELECT id, session_id, seq, parent_id, message_id, role, "
                    "content_json, created_at FROM session_entries "
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
                await db.execute(
                    "INSERT INTO session_facts "
                    "(id, session_id, seq, kind, entry_id, value_json, created_at) "
                    "VALUES (?, ?, ?, 'label', ?, ?, ?)",
                    (
                        _gen_id("fact"), session_id, fact_seq, entry_id,
                        json.dumps(label, ensure_ascii=False), now,
                    ),
                )
                await db.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        result = (await self._rows_to_entries(session_id, [entry_row]))[0]
        return result

    # ------------------------------------------------------------------
    # message CRUD
    # ------------------------------------------------------------------

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
            except Exception:
                await db.rollback()
                raise

    # ------------------------------------------------------------------
    # snapshot CRUD
    # ------------------------------------------------------------------

    async def append_snapshot(
        self, session_id: str, snapshot: RequestSnapshot,
    ) -> SQLiteStoredSnapshot:
        """追加 snapshot；不存在 session 抛 SessionNotFoundError。"""
        db = self._require_db()
        await self._require_session(session_id)
        snap_id = _gen_id("snap")
        now = _now_ms()
        content_json = _serialize_snapshot(snapshot)
        await db.execute(
            "INSERT INTO snapshots (id, session_id, turn_id, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (snap_id, session_id, snapshot.request_id, content_json, now),
        )
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        await db.commit()
        return SQLiteStoredSnapshot(
            id=snap_id, session_id=session_id,
            turn_id=snapshot.request_id,
            snapshot=snapshot, created_at=now,
        )

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


__all__ = [
    # 异常
    "SQLiteSessionError",
    "SessionNotFoundError",
    "SessionSerializationError",
    "SessionLaneNotFoundError",
    "SessionLaneExistsError",
    "SessionEntryNotFoundError",
    "SessionBranchError",
    # 数据模型
    "SQLiteSession",
    "SQLiteStoredMessage",
    "SQLiteStoredSnapshot",
    "SQLiteSessionEntry",
    "SQLiteSessionLane",
    # 主类
    "SQLiteSessionStore",
]
