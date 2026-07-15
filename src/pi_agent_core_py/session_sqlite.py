"""SQLiteSessionStore —— sqlite 线性 session 存储（P0-1）。

设计要点：
- 单文件 sqlite + WAL 模式；单写者（aiosqlite 单连接）
- **独立于** `session.py` / `session_sync.py` / harness 内置 session 体系
- Web 层用本模块管理多会话；旧 JSONL Tree 路径不动
- message 序列化为 `{type, data}` JSON；反序列化回强类型 Pydantic 对象
  （不会退化成 dict）
- 并发 `append_message` 用 `BEGIN IMMEDIATE` + `SELECT MAX(idx)+1` 保证 idx 单调
- snapshot 表存 TurnSnapshot 完整 dict（`to_dict()` / `model_validate()`）

异常体系：
- `SQLiteSessionError` 基类
- `SessionNotFoundError` —— rename/delete/append/list 对不存在 session
- `SessionSerializationError` —— JSON 反序列化失败 / 未知 type

不实现：
- 旧 fork / branch_summary 兼容（用 `session.py` 的旧路径）
- 自动 compaction（由 harness.compact_* 触发；sqlite 仅持久化）
- 跨进程共享（单进程场景；多进程需要单独协调）
"""
from __future__ import annotations

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
from .snapshot import TurnSnapshot

# ============================================================================
# 异常
# ============================================================================


class SQLiteSessionError(Exception):
    """sqlite session 模块基类异常。"""


class SessionNotFoundError(SQLiteSessionError):
    """rename/delete/append/list 对不存在 session 抛。"""


class SessionSerializationError(SQLiteSessionError):
    """JSON 反序列化失败 / 未知 message type 抛。"""


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
    snapshot: TurnSnapshot
    created_at: int


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


def _serialize_snapshot(snapshot: TurnSnapshot) -> str:
    """TurnSnapshot → JSON 字符串（走 to_dict，已是 JSON-safe）。"""
    return json.dumps(snapshot.to_dict(), ensure_ascii=False)


def _deserialize_snapshot(content_json: str) -> TurnSnapshot:
    """JSON 字符串 → TurnSnapshot。"""
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
        return TurnSnapshot.model_validate(payload)
    except Exception as e:
        raise SessionSerializationError(
            f"TurnSnapshot 反序列化失败：{type(e).__name__}: {e}"
        ) from e


# ============================================================================
# SQLiteSessionStore
# ============================================================================


class SQLiteSessionStore:
    """sqlite 线性 session 存储。

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

        self._db = await aiosqlite.connect(db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._exec_schema()
        await self._db.commit()

    async def _exec_schema(self) -> None:
        assert self._db is not None
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id              TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                metadata_json   TEXT NOT NULL DEFAULT '{}'
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

            CREATE INDEX IF NOT EXISTS idx_messages_session_idx
                ON messages(session_id, idx);

            CREATE INDEX IF NOT EXISTS idx_snapshots_session_created
                ON snapshots(session_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_sessions_updated
                ON sessions(updated_at);
            """
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
        )

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
        await db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, title, now, now, meta_json),
        )
        await db.commit()
        return SQLiteSession(
            id=session_id, title=title, created_at=now, updated_at=now,
            metadata=dict(metadata or {}),
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
    # message CRUD
    # ------------------------------------------------------------------

    async def append_message(
        self, session_id: str, message: AgentMessage,
    ) -> SQLiteStoredMessage:
        """追加单条 message；返回 SQLiteStoredMessage。

        并发安全策略：
        - 单条 SQL 用子查询原子计算 idx：
            `INSERT ... VALUES (?, ?, (SELECT COALESCE(MAX(idx), -1) + 1
                                       FROM messages WHERE session_id = ?), ...)`
        - aiosqlite 单连接串行所有 SQL；sqlite3 WAL 模式下 INSERT 自动包事务
        - messages 表 UNIQUE(session_id, idx) 约束兜底——理论冲突也会失败而非插重复

        不存在 session 抛 SessionNotFoundError。
        """
        db = self._require_db()
        await self._require_session(session_id)
        msg_id = _gen_id("msg")
        now = _now_ms()
        role = getattr(message, "role", "custom") or "custom"
        content_json = _serialize_message(message)

        # 子查询在 INSERT 内部计算 idx——sqlite 单 SQL 原子；外部无需 BEGIN IMMEDIATE
        cur = await db.execute(
            "INSERT INTO messages (id, session_id, idx, role, content_json, created_at) "
            "VALUES (?, ?, "
            "(SELECT COALESCE(MAX(idx), -1) + 1 FROM messages WHERE session_id = ?), "
            "?, ?, ?) "
            "RETURNING idx",
            (msg_id, session_id, session_id, role, content_json, now),
        )
        row = await cur.fetchone()
        await cur.close()
        next_idx = row["idx"] if row else 0
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
        await db.commit()

        return SQLiteStoredMessage(
            id=msg_id, session_id=session_id, idx=next_idx,
            role=role, message=message, created_at=now,
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
        """覆盖式同步 session 的 messages——diff-based，保留历史 row id + created_at。

        算法（D2-1）：
        1. 读取当前 DB 中所有 messages 按 idx 升序（含 id / created_at）
        2. 共同前缀 [0, min(old, new))：
           - role + content_json 都相同：不动（id + created_at 保持）
           - role 相同但 content_json 不同：UPDATE content_json
             （id + created_at 保持——审计/revision FK 稳定）
           - role 不同：从此处 truncate（first_mismatch = i）
        3. 共同前缀之外 [first_mismatch, ...)：
           - 旧 row 全部 DELETE
           - 新 message 全部 INSERT（生成新 id + created_at = now）
        4. session.updated_at 总是更新到 now
        5. 任一步失败 → transaction ROLLBACK（含已 UPDATE 的共同前缀）

        用例：harness.run_prompt 后用 final_messages 覆盖（追加尾部 / 内容修订）；
        保证未来 D2 revision 表的 assistant_message_id FK 永远指向稳定的 row id。

        不存在 session 抛 SessionNotFoundError。
        """
        db = self._require_db()
        await self._require_session(session_id)
        now = _now_ms()
        new_messages = list(messages)
        new_count = len(new_messages)

        try:
            cur = await db.execute(
                "SELECT id, idx, role, content_json, created_at "
                "FROM messages WHERE session_id = ? "
                "ORDER BY idx ASC",
                (session_id,),
            )
            old_rows = await cur.fetchall()
            await cur.close()
            old_count = len(old_rows)
            common_len = min(old_count, new_count)

            # 共同前缀：UPDATE content（同 role 不同内容）或 detect role-mismatch
            first_mismatch = common_len
            for i in range(common_len):
                old = old_rows[i]
                new_msg = new_messages[i]
                new_role = getattr(new_msg, "role", "custom") or "custom"
                new_content_json = _serialize_message(new_msg)

                if old["role"] != new_role:
                    first_mismatch = i
                    break
                if old["content_json"] != new_content_json:
                    await db.execute(
                        "UPDATE messages SET content_json = ? WHERE id = ?",
                        (new_content_json, old["id"]),
                    )

            # 删除 [first_mismatch, old_count) 的旧 row
            if first_mismatch < old_count:
                truncate_from_idx = old_rows[first_mismatch]["idx"]

                # P1-D2-3 orphan cleanup：删除 messages 前，先清理可能存在的
                # web_message_revisions（assistant_message_id 无跨表 FK，必须显式
                # 处理；session delete 走 session_id CASCADE，但单条 message 删除
                # 不会触发）。同一 transaction 内保证原子性。
                #
                # 兼容旧 schema：检查表是否存在（从 v2 起表必然存在）。
                cur_check = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'web_message_revisions'"
                )
                rev_table_exists = await cur_check.fetchone() is not None
                await cur_check.close()

                if rev_table_exists:
                    cur_del = await db.execute(
                        "SELECT id FROM messages "
                        "WHERE session_id = ? AND idx >= ? AND role = 'assistant'",
                        (session_id, truncate_from_idx),
                    )
                    assistant_ids = [
                        r["id"] for r in await cur_del.fetchall()
                    ]
                    await cur_del.close()

                    if assistant_ids:
                        placeholders = ",".join("?" * len(assistant_ids))
                        await db.execute(
                            f"DELETE FROM web_message_revisions "
                            f"WHERE assistant_message_id IN ({placeholders})",
                            assistant_ids,
                        )

                await db.execute(
                    "DELETE FROM messages "
                    "WHERE session_id = ? AND idx >= ?",
                    (session_id, truncate_from_idx),
                )

            # 插入 [first_mismatch, new_count) 的新 row
            for j in range(first_mismatch, new_count):
                msg = new_messages[j]
                msg_id = _gen_id("msg")
                role = getattr(msg, "role", "custom") or "custom"
                content_json = _serialize_message(msg)
                await db.execute(
                    "INSERT INTO messages "
                    "(id, session_id, idx, role, content_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (msg_id, session_id, j, role, content_json, now),
                )

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
        self, session_id: str, snapshot: TurnSnapshot,
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

    async def list_snapshots(self, session_id: str) -> list[TurnSnapshot]:
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
    # 数据模型
    "SQLiteSession",
    "SQLiteStoredMessage",
    "SQLiteStoredSnapshot",
    # 主类
    "SQLiteSessionStore",
]
