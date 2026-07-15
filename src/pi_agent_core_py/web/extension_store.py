"""P1-C1: Extension 配置持久化层——Skills + MCP servers + MCP disabled tools。

**设计**（用户原指令 §4 + 6 个修正点）：

1. **:memory: 共享 connection**——`:memory:` 模式下两个独立 aiosqlite.connect 是不同
   数据库；ExtensionSQLiteStore 接受 session_store 的 connection 注入，确保
   session 表和 extension 表共享同一内存数据库。文件型 db_path 可独立 connection。

2. **逐行隔离**——`list_uploaded_skill_rows()` 返回原始 rows；
   `decode_uploaded_skill(row)` 独立 decode。单行损坏 JSON 不阻塞其他行 restore。

3. **Schema version**——`web_extension_schema_meta(version)` 为未来 alter column /
   数据回填留入口。当前 version=1。

4. **MCP env 安全**——只持久化 env_keys（list[str]），**绝不**持久化 env value。
   value 从 os.environ 恢复（C4 实现）。

5. **专用错误**——ExtensionStoreError / ValidationError / ConflictError，不泄露
   SQL / 绝对路径 / secret。

6. **MCP tool 结构化 key**——持久化用 (server_name, raw_tool_name) tuple，
   不依赖 full_name.split("__")。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite


def _now_ms() -> int:
    """毫秒时间戳——用于 revision id 生成。"""
    return int(datetime.now(UTC).timestamp() * 1000)


# ============================================================================
# 专用错误
# ============================================================================


class ExtensionStoreError(Exception):
    """Extension store 基础错误——安全摘要，不含 SQL / secret / 绝对路径。"""


class ExtensionStoreValidationError(ExtensionStoreError):
    """字段校验失败——如 command 为空、args 非 list、name 含非法字符。"""


class ExtensionStoreConflictError(ExtensionStoreError):
    """冲突——如重名 skill / server。"""


# ============================================================================
# P1-D2-3 revision 错误体系——安全错误码，不含 content / prompt / SQL / 绝对路径
# ============================================================================


class RevisionError(ExtensionStoreError):
    """Revision 相关错误基类。"""


class RevisionNotFoundError(RevisionError):
    """revision_id 不存在（finalize / mark_error / mark_aborted 路径）。"""


class RevisionTargetNotFoundError(RevisionError):
    """assistant_message_id 不在 messages 表，或与 session_id 不匹配。"""


class RevisionTargetNotAssistantError(RevisionError):
    """目标 message 存在但 role 不是 assistant。"""


class RevisionTargetNotLatestError(RevisionError):
    """目标 message 不是当前 session 的最新 assistant。"""


class RevisionAlreadyRunningError(RevisionError):
    """该 assistant_message_id 已有一个 status='running' 的 revision。"""


class RevisionRequestConflictError(RevisionError):
    """request_id 已被其它 revision 占用，或与 revision row 不匹配。"""


class RevisionBaseContentChangedError(RevisionError):
    """finalize 时 base_content_sha256 与当前 messages.content_json 不匹配。

    上层应在独立 transaction 中决定是否把 revision 标 error——本异常已经触发
    finalize transaction 的 rollback，messages 保持现状。
    """


class RevisionStateTransitionError(RevisionError):
    """非法状态转换——如 completed → error、superseded → completed。"""


# ============================================================================
# 持久化数据模型
# ============================================================================


@dataclass
class PersistedSkill:
    """上传 Skill 的持久化视图——与 runtime Skill 模型字段对齐。"""

    name: str
    skill_json: str  # canonical Skill 模型 model_dump_json
    raw_markdown: str  # 原始 SKILL.md 内容
    enabled: bool = True
    source_kind: str = "upload"
    content_sha256: str = ""
    last_restore_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class PersistedMCPServer:
    """MCP server 持久化视图——**不含 env value**，只存 env_keys。"""

    name: str
    transport: str = "stdio"
    command: str = ""
    args_json: str = "[]"  # JSON-encoded list[str]
    desired_enabled: bool = False
    env_keys_json: str = "[]"  # JSON-encoded list[str]——**绝不**含 value
    last_restore_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class PersistedDisabledTool:
    """MCP tool disabled 状态——结构化 key (server_name, tool_name)。"""

    server_name: str
    tool_name: str  # raw tool name（不含 mcp__server__ 前缀）
    disabled_at: str = ""


@dataclass
class SkillRowResult:
    """逐行隔离的 Skill 读取结果——单行损坏不阻塞 list。"""

    name: str
    skill: PersistedSkill | None = None
    error: str | None = None


@dataclass
class MCPServerRowResult:
    """逐行隔离的 MCP server 读取结果。"""

    name: str
    server: PersistedMCPServer | None = None
    error: str | None = None


@dataclass(frozen=True)
class PersistedMessageRevision:
    """P1-D2-3：message revision 的持久化视图（不可变）。

    对应 `web_message_revisions` 表的一行。`content_json` 在 running/aborted/
    interrupted 状态下为 None；completed/superseded 状态下为完整 AssistantMessage
    canonical JSON。
    """

    id: str
    session_id: str
    assistant_message_id: str
    revision_number: int
    request_id: str | None
    status: str
    base_content_sha256: str
    content_json: str | None
    created_at: str
    completed_at: str | None
    error_summary: str | None


# ============================================================================
# ExtensionSQLiteStore
# ============================================================================


SCHEMA_VERSION = 2


# ============================================================================
# DDL 常量——拆成单独语句 list 而非多语句字符串，便于在显式 transaction 内
# 用 execute() 逐条执行（executescript 会隐式 commit，破坏 BEGIN IMMEDIATE）
# ============================================================================

_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS web_extension_schema_meta (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
)
"""

# P1-C1 三张表——fresh v2 与既有 v1 共用（CREATE IF NOT EXISTS 幂等）
_C1_TABLE_DDL_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS web_uploaded_skills (
        name                TEXT PRIMARY KEY,
        skill_json          TEXT NOT NULL,
        raw_markdown        TEXT NOT NULL,
        enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        source_kind         TEXT NOT NULL DEFAULT 'upload'
                            CHECK (source_kind IN ('upload')),
        content_sha256      TEXT NOT NULL DEFAULT '',
        last_restore_error  TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_mcp_servers (
        name                TEXT PRIMARY KEY,
        transport           TEXT NOT NULL DEFAULT 'stdio'
                            CHECK (transport IN ('stdio')),
        command             TEXT NOT NULL,
        args_json           TEXT NOT NULL DEFAULT '[]',
        desired_enabled     INTEGER NOT NULL DEFAULT 0
                            CHECK (desired_enabled IN (0, 1)),
        env_keys_json       TEXT NOT NULL DEFAULT '[]',
        last_restore_error  TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_mcp_disabled_tools (
        server_name         TEXT NOT NULL,
        tool_name           TEXT NOT NULL,
        disabled_at         TEXT NOT NULL,
        PRIMARY KEY (server_name, tool_name),
        FOREIGN KEY (server_name)
            REFERENCES web_mcp_servers(name)
            ON DELETE CASCADE
    )
    """,
]

# P1-D2 revisions 表——fresh v2 与 v1→v2 migration 共用
# DDL 补充（审核要求）：
#   - revision_number CHECK >= 0
#   - status IN ('completed','superseded') → content_json NOT NULL
#   - 不加 running→NULL CHECK（留未来 checkpoint 灵活性）
_REVISIONS_DDL_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS web_message_revisions (
        id                    TEXT PRIMARY KEY,
        session_id            TEXT NOT NULL,
        assistant_message_id  TEXT NOT NULL,
        revision_number       INTEGER NOT NULL CHECK (revision_number >= 0),
        request_id            TEXT,
        status                TEXT NOT NULL CHECK (
            status IN (
                'running',
                'completed',
                'superseded',
                'error',
                'aborted',
                'interrupted'
            )
        ),
        base_content_sha256   TEXT NOT NULL,
        content_json          TEXT,
        created_at            TEXT NOT NULL,
        completed_at          TEXT,
        error_summary         TEXT,
        UNIQUE (assistant_message_id, revision_number),
        FOREIGN KEY (session_id)
            REFERENCES sessions(id)
            ON DELETE CASCADE,
        CHECK (
            status NOT IN ('completed', 'superseded')
            OR content_json IS NOT NULL
        )
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_request
        ON web_message_revisions(request_id)
        WHERE request_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_running
        ON web_message_revisions(assistant_message_id)
        WHERE status = 'running'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_active
        ON web_message_revisions(assistant_message_id)
        WHERE status = 'completed'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_web_message_revisions_history
        ON web_message_revisions(
            session_id,
            assistant_message_id,
            revision_number DESC
        )
    """,
]


# fresh-v2 schema validation 必须存在的表 + 索引
_REQUIRED_TABLES = (
    "web_uploaded_skills",
    "web_mcp_servers",
    "web_mcp_disabled_tools",
    "web_message_revisions",
)
_REQUIRED_INDEXES = (
    "uq_web_message_revision_request",
    "uq_web_message_revision_running",
    "uq_web_message_revision_active",
    "idx_web_message_revisions_history",
)


class ExtensionSQLiteStore:
    """Extension 配置 SQLite 持久化层。

    用法：
        # 共享 session_store connection（:memory: 模式必须）
        ext_store = ExtensionSQLiteStore(db_path, connection=session_store.connection)
        await ext_store.init()

        # 文件型独立 connection
        ext_store = ExtensionSQLiteStore("/path/to/db.sqlite")
        await ext_store.init()

    **不变量**：
    - init() 幂等（已 init 则 no-op）
    - close() 幂等；injected connection 不 close（由调用方负责）
    - 所有写操作在事务内
    - env value 永不写入（只 env_keys）
    - 单行损坏不阻塞 list/restore
    """

    def __init__(
        self,
        db_path: str,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> None:
        self._db_path = db_path
        self._injected_connection = connection
        # P1-C1 不变量 1: Connection ownership——只 close 自己 open 的 connection；
        # injected connection 由调用方（session_store）负责 close
        self._owns_connection = connection is None
        self._db: aiosqlite.Connection | None = None
        self._closed = False

    def _require_db(self) -> aiosqlite.Connection:
        """统一检查 connection 可用——关闭后抛 ExtensionStoreError 而非 AssertionError。"""
        if self._db is None:
            raise ExtensionStoreError("store not initialized or already closed")
        return self._db

    async def init(self) -> None:
        """打开 / 接受 connection + 按 schema version 初始化。幂等。

        初始化顺序（审核要求——避免在检查 version 前意外修改未来版本 DB）：

            1. open / accept connection（含 PRAGMA）
            2. 只确保 schema_meta 表存在（CREATE IF NOT EXISTS）
            3. 读 schema version
            4. 按 version 分支：
               - None      → 全新 DB，单 transaction 建 v2 全部 schema
               - 1         → migrate v1→v2（单 transaction）
               - 2         → 只 validate，不重建
               - > 2       → 在任何 DDL 前 raise（防止 downgrade 损坏）
               - 其它      → raise
        """
        if self._db is not None:
            return
        if self._injected_connection is not None:
            self._db = self._injected_connection
        else:
            db_path = self._db_path
            if db_path != ":memory:":
                from pathlib import Path

                parent = Path(db_path).parent
                if str(parent) and not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA busy_timeout=5000")

        # 先确保 schema_meta 表存在（独立 executescript——此时还没读 version）
        await self._db.executescript(_SCHEMA_META_DDL)
        await self._db.commit()

        version = await self.get_schema_version()

        if version is None:
            await self._initialize_fresh_v2_schema()
        elif version == 1:
            await self._migrate_v1_to_v2()
        elif version == SCHEMA_VERSION:
            await self._validate_v2_schema()
        elif version > SCHEMA_VERSION:
            # 关键：在任何 DDL 前失败——防止把未来版本的 DB 当成旧版重建
            raise ExtensionStoreError(
                "Extension database schema is newer than this application."
            )
        else:
            raise ExtensionStoreError(
                f"Unsupported extension database schema version: {version}"
            )

    # ------------------------------------------------------------------
    # schema 初始化 / migration 内部方法
    # ------------------------------------------------------------------

    async def _initialize_fresh_v2_schema(self) -> None:
        """全新 DB——单 BEGIN IMMEDIATE transaction 建 C1 三表 + revisions 表 + 索引
        + 插入 schema_meta(id=1, version=2)。

        任一步失败 → ROLLBACK（schema_meta 表仍在但 version 字段未填——下次 init
        会重新进入此分支重试；幂等）。
        """
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            for stmt in _C1_TABLE_DDL_STATEMENTS:
                await db.execute(stmt)
            for stmt in _REVISIONS_DDL_STATEMENTS:
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO web_extension_schema_meta (id, version) VALUES (1, ?)",
                (SCHEMA_VERSION,),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _migrate_v1_to_v2(self) -> None:
        """v1→v2 migration——单 BEGIN IMMEDIATE transaction 加 revisions 表 + 4 索引
        + UPDATE schema_meta version 1→2。

        事务内重新校验 version=1（防并发 init）；UPDATE rowcount 必须=1。
        幂等：CREATE TABLE/INDEX IF NOT EXISTS。

        任一步失败 → ROLLBACK；version 仍为 1；revisions 表/索引不留残留。
        """
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            # 事务内再校验 version（防并发 init race）
            cursor = await db.execute(
                "SELECT version FROM web_extension_schema_meta WHERE id = 1"
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["version"] != 1:
                raise ExtensionStoreError(
                    "schema version changed unexpectedly during migration"
                )
            for stmt in _REVISIONS_DDL_STATEMENTS:
                await db.execute(stmt)
            cursor = await db.execute(
                "UPDATE web_extension_schema_meta SET version = ? "
                "WHERE id = 1 AND version = 1",
                (SCHEMA_VERSION,),
            )
            if cursor.rowcount != 1:
                raise ExtensionStoreError(
                    "migration rowcount mismatch——version unchanged"
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _validate_v2_schema(self) -> None:
        """version=2 已就绪——只读校验关键表/索引存在，不重建。

        若 version=2 但 schema 不完整，报告 corruption（不静默重建——审核要求）。
        """
        db = self._require_db()
        for table in _REQUIRED_TABLES:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (table,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ExtensionStoreError(
                    "schema corruption: table missing despite version=2 "
                    f"(table={table!r})"
                )
        for index in _REQUIRED_INDEXES:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name = ?",
                (index,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ExtensionStoreError(
                    "schema corruption: index missing despite version=2 "
                    f"(index={index!r})"
                )

    async def close(self) -> None:
        """关闭连接。幂等。

        **Connection ownership 规则**（不变量 1）：
        - 自己 open 的 connection（_owns_connection=True）→ close() 负责关闭
        - injected connection（_owns_connection=False）→ 只清自身引用，不 close；
          调用方（session_store）负责 close
        """
        if self._closed or self._db is None:
            self._closed = True
            return
        try:
            if self._owns_connection:
                await self._db.close()
        finally:
            self._db = None
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _safe_db_error(e: Exception) -> str:
        """把 DB 异常转成安全摘要——不含 SQL / 路径 / secret。"""
        return f"{type(e).__name__}: DB operation failed"

    # ==================================================================
    # Skills
    # ==================================================================

    async def upsert_uploaded_skill(
        self,
        *,
        name: str,
        skill_json: str,
        raw_markdown: str,
        enabled: bool = True,
        source_kind: str = "upload",
        content_sha256: str = "",
    ) -> None:
        """插入或更新上传 Skill。"""
        self._require_db()
        if not name or not name.strip():
            raise ExtensionStoreValidationError("skill name is required")
        if not skill_json:
            raise ExtensionStoreValidationError("skill_json is required")
        if not raw_markdown:
            raise ExtensionStoreValidationError("raw_markdown is required")
        now = self._now_iso()
        try:
            await self._db.execute(
                """
                INSERT INTO web_uploaded_skills
                    (name, skill_json, raw_markdown, enabled, source_kind,
                     content_sha256, last_restore_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    skill_json = excluded.skill_json,
                    raw_markdown = excluded.raw_markdown,
                    enabled = excluded.enabled,
                    source_kind = excluded.source_kind,
                    content_sha256 = excluded.content_sha256,
                    last_restore_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    name,
                    skill_json,
                    raw_markdown,
                    1 if enabled else 0,
                    source_kind,
                    content_sha256,
                    now,
                    now,
                ),
            )
            await self._db.commit()
        except ExtensionStoreValidationError:
            raise
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def get_uploaded_skill(self, name: str) -> PersistedSkill | None:
        """单条查询——返回 None 如果不存在。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "SELECT * FROM web_uploaded_skills WHERE name = ?",
                (name,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_skill(row)
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def list_uploaded_skill_rows(self) -> list[aiosqlite.Row]:
        """返回所有原始 rows——调用方逐行 decode 隔离损坏。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "SELECT * FROM web_uploaded_skills ORDER BY created_at ASC"
            )
            return list(await cursor.fetchall())
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    @staticmethod
    def decode_uploaded_skill(row: aiosqlite.Row) -> SkillRowResult:
        """逐行 decode——单行损坏返回 error，不抛异常。

        **JSON 验证在此处**（不在 _row_to_skill）——get_uploaded_skill 返回原始数据，
        decode_uploaded_skill 用于 restore 时验证。
        """
        try:
            json.loads(row["skill_json"])  # 验证 skill_json 合法
            return SkillRowResult(
                name=row["name"],
                skill=ExtensionSQLiteStore._row_to_skill(row),
                error=None,
            )
        except Exception as e:
            return SkillRowResult(
                name=str(row["name"] if "name" in row.keys() else "<unknown>"),
                skill=None,
                error=f"{type(e).__name__}: {e}"[:200],
            )

    @staticmethod
    def _row_to_skill(row: aiosqlite.Row) -> PersistedSkill:
        """字段映射——不验证 JSON（由 decode_uploaded_skill 验证）。"""
        return PersistedSkill(
            name=row["name"],
            skill_json=row["skill_json"],
            raw_markdown=row["raw_markdown"],
            enabled=bool(row["enabled"]),
            source_kind=row["source_kind"],
            content_sha256=row["content_sha256"],
            last_restore_error=row["last_restore_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def set_skill_enabled(self, name: str, enabled: bool) -> None:
        """更新 enabled 状态。不存在抛 ConflictError。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "UPDATE web_uploaded_skills SET enabled = ?, updated_at = ? "
                "WHERE name = ?",
                (1 if enabled else 0, self._now_iso(), name),
            )
            if cursor.rowcount == 0:
                raise ExtensionStoreConflictError(
                    f"skill {name!r} not persisted"
                )
            await self._db.commit()
        except ExtensionStoreConflictError:
            raise
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def set_skill_restore_error(
        self, name: str, error: str | None
    ) -> None:
        """记录 / 清除 restore error。"""
        self._require_db()
        try:
            await self._db.execute(
                "UPDATE web_uploaded_skills SET last_restore_error = ?, "
                "updated_at = ? WHERE name = ?",
                (error, self._now_iso(), name),
            )
            await self._db.commit()
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def delete_uploaded_skill(self, name: str) -> bool:
        """删除上传 Skill row。返回 True 如果删除了，False 如果不存在。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "DELETE FROM web_uploaded_skills WHERE name = ?", (name,)
            )
            await self._db.commit()
            return cursor.rowcount > 0
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    # ==================================================================
    # MCP servers
    # ==================================================================

    async def upsert_mcp_server(
        self,
        *,
        name: str,
        transport: str = "stdio",
        command: str,
        args: list[str],
        desired_enabled: bool = False,
        env_keys: list[str],
    ) -> None:
        """插入 / 更新 MCP server config——**只存 env_keys，不存 value**。"""
        self._require_db()
        if not name or not name.strip():
            raise ExtensionStoreValidationError("mcp server name is required")
        if not command or not command.strip():
            raise ExtensionStoreValidationError("mcp server command is required")
        if not isinstance(args, list):
            raise ExtensionStoreValidationError("args must be a list of strings")
        if not isinstance(env_keys, list):
            raise ExtensionStoreValidationError(
                "env_keys must be a list of strings"
            )
        args_json = json.dumps(args)
        env_keys_json = json.dumps(sorted(env_keys))
        now = self._now_iso()
        try:
            await self._db.execute(
                """
                INSERT INTO web_mcp_servers
                    (name, transport, command, args_json, desired_enabled,
                     env_keys_json, last_restore_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    transport = excluded.transport,
                    command = excluded.command,
                    args_json = excluded.args_json,
                    env_keys_json = excluded.env_keys_json,
                    updated_at = excluded.updated_at
                """,
                (
                    name,
                    transport,
                    command,
                    args_json,
                    1 if desired_enabled else 0,
                    env_keys_json,
                    now,
                    now,
                ),
            )
            await self._db.commit()
        except ExtensionStoreValidationError:
            raise
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def get_mcp_server(self, name: str) -> PersistedMCPServer | None:
        self._require_db()
        try:
            cursor = await self._db.execute(
                "SELECT * FROM web_mcp_servers WHERE name = ?", (name,)
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_mcp_server(row)
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def list_mcp_server_rows(self) -> list[aiosqlite.Row]:
        """返回所有原始 rows——逐行 decode 隔离损坏。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "SELECT * FROM web_mcp_servers ORDER BY created_at ASC"
            )
            return list(await cursor.fetchall())
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    @staticmethod
    def decode_mcp_server(row: aiosqlite.Row) -> MCPServerRowResult:
        try:
            return MCPServerRowResult(
                name=row["name"],
                server=ExtensionSQLiteStore._row_to_mcp_server(row),
                error=None,
            )
        except Exception as e:
            return MCPServerRowResult(
                name=str(row["name"] if "name" in row.keys() else "<unknown>"),
                server=None,
                error=f"{type(e).__name__}: {e}"[:200],
            )

    @staticmethod
    def _row_to_mcp_server(row: aiosqlite.Row) -> PersistedMCPServer:
        # 验证 args_json / env_keys_json 是合法 JSON——损坏抛异常被 decode catch
        json.loads(row["args_json"])
        json.loads(row["env_keys_json"])
        return PersistedMCPServer(
            name=row["name"],
            transport=row["transport"],
            command=row["command"],
            args_json=row["args_json"],
            desired_enabled=bool(row["desired_enabled"]),
            env_keys_json=row["env_keys_json"],
            last_restore_error=row["last_restore_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def set_mcp_server_enabled(
        self, name: str, desired_enabled: bool
    ) -> None:
        """更新 desired_enabled 状态。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "UPDATE web_mcp_servers SET desired_enabled = ?, updated_at = ? "
                "WHERE name = ?",
                (1 if desired_enabled else 0, self._now_iso(), name),
            )
            if cursor.rowcount == 0:
                raise ExtensionStoreConflictError(
                    f"mcp server {name!r} not persisted"
                )
            await self._db.commit()
        except ExtensionStoreConflictError:
            raise
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def set_mcp_restore_error(
        self, name: str, error: str | None
    ) -> None:
        self._require_db()
        try:
            await self._db.execute(
                "UPDATE web_mcp_servers SET last_restore_error = ?, "
                "updated_at = ? WHERE name = ?",
                (error, self._now_iso(), name),
            )
            await self._db.commit()
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def delete_mcp_server(self, name: str) -> bool:
        """删除 server + cascade disabled tools（FK ON DELETE CASCADE）。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "DELETE FROM web_mcp_servers WHERE name = ?", (name,)
            )
            await self._db.commit()
            return cursor.rowcount > 0
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    # ==================================================================
    # MCP disabled tools（结构化 key: server_name + raw tool_name）
    # ==================================================================

    async def disable_mcp_tool(
        self, server_name: str, tool_name: str
    ) -> None:
        """记录 disabled tool——结构化 key，不依赖 full_name.split。"""
        self._require_db()
        if not server_name or not tool_name:
            raise ExtensionStoreValidationError(
                "server_name and tool_name are required"
            )
        try:
            await self._db.execute(
                """
                INSERT OR IGNORE INTO web_mcp_disabled_tools
                    (server_name, tool_name, disabled_at)
                VALUES (?, ?, ?)
                """,
                (server_name, tool_name, self._now_iso()),
            )
            await self._db.commit()
        except ExtensionStoreValidationError:
            raise
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def enable_mcp_tool(
        self, server_name: str, tool_name: str
    ) -> None:
        """移除 disabled tool 记录。"""
        self._require_db()
        try:
            await self._db.execute(
                "DELETE FROM web_mcp_disabled_tools "
                "WHERE server_name = ? AND tool_name = ?",
                (server_name, tool_name),
            )
            await self._db.commit()
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def list_disabled_mcp_tools(
        self, server_name: str | None = None
    ) -> list[PersistedDisabledTool]:
        """列出 disabled tools——可按 server 过滤。"""
        self._require_db()
        try:
            if server_name is None:
                cursor = await self._db.execute(
                    "SELECT * FROM web_mcp_disabled_tools "
                    "ORDER BY disabled_at ASC"
                )
            else:
                cursor = await self._db.execute(
                    "SELECT * FROM web_mcp_disabled_tools "
                    "WHERE server_name = ? ORDER BY disabled_at ASC",
                    (server_name,),
                )
            rows = await cursor.fetchall()
            return [
                PersistedDisabledTool(
                    server_name=r["server_name"],
                    tool_name=r["tool_name"],
                    disabled_at=r["disabled_at"],
                )
                for r in rows
            ]
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    # ==================================================================
    # Schema meta
    # ==================================================================

    async def get_schema_version(self) -> int | None:
        """读取 schema version——未来 migration 入口。"""
        self._require_db()
        try:
            cursor = await self._db.execute(
                "SELECT version FROM web_extension_schema_meta LIMIT 1"
            )
            row = await cursor.fetchone()
            return row["version"] if row is not None else None
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    # ==================================================================
    # P1-D2-3 Message Revisions
    # ==================================================================

    @staticmethod
    def _gen_revision_id() -> str:
        """生成 revision row id——前缀 rev + ms 时间戳 + uuid 短码。"""
        return f"rev-{_now_ms()}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _row_to_revision(row: aiosqlite.Row) -> PersistedMessageRevision:
        """raw row → frozen dataclass。"""
        return PersistedMessageRevision(
            id=row["id"],
            session_id=row["session_id"],
            assistant_message_id=row["assistant_message_id"],
            revision_number=row["revision_number"],
            request_id=row["request_id"],
            status=row["status"],
            base_content_sha256=row["base_content_sha256"],
            content_json=row["content_json"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            error_summary=row["error_summary"],
        )

    @staticmethod
    def _sha256_of_content_json(content_json: str) -> str:
        """直接对原始字符串算 sha256——**禁止** json.loads/dumps 重序列化。

        审核要求：hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        """
        return hashlib.sha256(content_json.encode("utf-8")).hexdigest()

    @staticmethod
    def _truncate_error_summary(s: str | None) -> str | None:
        """安全截断 error_summary——最多 500 字符；None 透传。"""
        if s is None:
            return None
        return s[:500]

    async def _verify_target_assistant_latest(
        self,
        db: aiosqlite.Connection,
        *,
        session_id: str,
        assistant_message_id: str,
    ) -> aiosqlite.Row:
        """共享前置校验：message 存在 + session 匹配 + role=assistant + 最新 assistant。

        返回 message row（含 content_json）。任一校验失败抛对应 RevisionError。
        """
        cursor = await db.execute(
            "SELECT id, session_id, role, content_json "
            "FROM messages WHERE id = ?",
            (assistant_message_id,),
        )
        msg = await cursor.fetchone()
        await cursor.close()
        if msg is None or msg["session_id"] != session_id:
            raise RevisionTargetNotFoundError(
                "target assistant message not found in this session"
            )
        if msg["role"] != "assistant":
            raise RevisionTargetNotAssistantError(
                "target message role is not assistant"
            )

        cursor = await db.execute(
            "SELECT id FROM messages "
            "WHERE session_id = ? AND role = 'assistant' "
            "ORDER BY idx DESC LIMIT 1",
            (session_id,),
        )
        latest = await cursor.fetchone()
        await cursor.close()
        if latest is None or latest["id"] != assistant_message_id:
            raise RevisionTargetNotLatestError(
                "target is not the latest assistant message in this session"
            )
        return msg

    async def create_running_revision(
        self,
        *,
        session_id: str,
        assistant_message_id: str,
        request_id: str,
    ) -> PersistedMessageRevision:
        """为指定 assistant message 创建 status='running' 的 revision row。

        单 BEGIN IMMEDIATE transaction 内完成所有校验 + INSERT，避免两个并发
        request 算出相同 revision_number。

        revision_number 从 1 开始（MAX+1）；revision_number=0 只能由 finalize
        在第一次成功时创建。base_content_sha256 直接对 DB 中原始 content_json
        字符串算 SHA-256。
        """
        db = self._require_db()
        now = self._now_iso()
        try:
            await db.execute("BEGIN IMMEDIATE")

            msg = await self._verify_target_assistant_latest(
                db,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
            )

            # 校验：该 assistant 没有其它 running revision
            cursor = await db.execute(
                "SELECT id FROM web_message_revisions "
                "WHERE assistant_message_id = ? AND status = 'running'",
                (assistant_message_id,),
            )
            if await cursor.fetchone() is not None:
                await cursor.close()
                raise RevisionAlreadyRunningError(
                    "another running revision already exists for this assistant"
                )
            await cursor.close()

            # 校验：request_id 未被其它 revision 占用
            cursor = await db.execute(
                "SELECT id FROM web_message_revisions WHERE request_id = ?",
                (request_id,),
            )
            if await cursor.fetchone() is not None:
                await cursor.close()
                raise RevisionRequestConflictError(
                    "request_id already associated with another revision"
                )
            await cursor.close()

            # 算 base_content_sha256（直接对原始字符串）
            base_sha = self._sha256_of_content_json(msg["content_json"])

            # 算下一 revision_number（MAX+1，无历史时 = 1）
            cursor = await db.execute(
                "SELECT MAX(revision_number) AS max_rev "
                "FROM web_message_revisions "
                "WHERE assistant_message_id = ?",
                (assistant_message_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            max_rev = row["max_rev"] if row is not None else None
            next_rev = (max_rev + 1) if max_rev is not None else 1

            revision_id = self._gen_revision_id()
            await db.execute(
                """
                INSERT INTO web_message_revisions
                    (id, session_id, assistant_message_id, revision_number,
                     request_id, status, base_content_sha256, content_json,
                     created_at, completed_at, error_summary)
                VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, ?, NULL, NULL)
                """,
                (
                    revision_id, session_id, assistant_message_id, next_rev,
                    request_id, base_sha, now,
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        result = await self.get_revision(revision_id)
        assert result is not None, "just-inserted revision missing"
        return result

    async def get_revision(
        self, revision_id: str,
    ) -> PersistedMessageRevision | None:
        """单条查询——不存在返回 None。"""
        db = self._require_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM web_message_revisions WHERE id = ?",
                (revision_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return self._row_to_revision(row) if row is not None else None
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def get_revision_by_request_id(
        self, request_id: str,
    ) -> PersistedMessageRevision | None:
        """按 request_id 查 revision——不存在返回 None。

        依赖 uq_web_message_revision_request 索引保证一个 request_id 至多一条 revision。
        """
        db = self._require_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM web_message_revisions WHERE request_id = ?",
                (request_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return self._row_to_revision(row) if row is not None else None
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def list_revisions(
        self,
        *,
        session_id: str,
        assistant_message_id: str,
        limit: int = 20,
        before_revision_number: int | None = None,
    ) -> list[PersistedMessageRevision]:
        """分页列出某 assistant 的 revisions，按 revision_number DESC 排序。

        - limit 默认 20，clamp 到 [1, 100]
        - before_revision_number=None → 不限；否则只返回 revision_number < 它的
        """
        db = self._require_db()
        clamped_limit = max(1, min(100, limit))
        try:
            if before_revision_number is None:
                cursor = await db.execute(
                    "SELECT * FROM web_message_revisions "
                    "WHERE session_id = ? AND assistant_message_id = ? "
                    "ORDER BY revision_number DESC LIMIT ?",
                    (session_id, assistant_message_id, clamped_limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM web_message_revisions "
                    "WHERE session_id = ? AND assistant_message_id = ? "
                    "AND revision_number < ? "
                    "ORDER BY revision_number DESC LIMIT ?",
                    (
                        session_id, assistant_message_id,
                        before_revision_number, clamped_limit,
                    ),
                )
            rows = await cursor.fetchall()
            await cursor.close()
            return [self._row_to_revision(r) for r in rows]
        except Exception as e:
            raise ExtensionStoreError(self._safe_db_error(e)) from None

    async def finalize_revision(
        self,
        *,
        revision_id: str,
        request_id: str,
        candidate_content_json: str,
    ) -> PersistedMessageRevision:
        """Finalize running revision → completed，原子切换 active assistant content。

        **D2-3 核心**——单 BEGIN IMMEDIATE transaction 内 10 步，事务中**不**做任何
        Agent 调用 / 网络 / 文件 I/O / sleep / 不可控 await。

        步骤：
            1. 读目标 revision
            2. 校验 revision_id / request_id / status（幂等 + reject 路径）
            3. 读目标 messages row
            4. 校验 session / role / 最新 assistant
            5. 校验 base_content_sha256 == sha256(当前 content_json)
            6. 首次成功（无 revision 0）→ INSERT revision 0 (WHERE NOT EXISTS)
            7. UPDATE 旧 completed → superseded
            8. UPDATE 本 running → completed + 写 candidate
            9. UPDATE messages.content_json（保留 id / idx / created_at）
            10. commit

        步骤 7 必须先于 8——否则违反 uq_web_message_revision_active。

        失败处理：
            - RevisionBaseContentChangedError：rollback，messages 保持现状，
              上层在**独立** transaction 中决定是否 mark_revision_error
            - 任何 SQL 失败：rollback，version 不变
        """
        db = self._require_db()
        now = self._now_iso()
        try:
            await db.execute("BEGIN IMMEDIATE")

            # 1. 读目标 revision
            cursor = await db.execute(
                "SELECT * FROM web_message_revisions WHERE id = ?",
                (revision_id,),
            )
            revision = await cursor.fetchone()
            await cursor.close()
            if revision is None:
                raise RevisionNotFoundError("revision not found")

            # 2. 校验 request_id
            if revision["request_id"] != request_id:
                raise RevisionRequestConflictError(
                    "request_id does not match this revision"
                )

            status = revision["status"]

            # 幂等：已 completed + request_id 匹配 → 直接返回，不创建 revision 0
            # 不改 completed_at，不动 messages
            if status == "completed":
                await db.commit()
                return self._row_to_revision(revision)

            # 非终态合法转换之外都拒绝：superseded / error / aborted / interrupted
            if status != "running":
                raise RevisionStateTransitionError(
                    f"cannot finalize revision in status {status!r}"
                )

            assistant_id = revision["assistant_message_id"]
            session_id = revision["session_id"]

            # 3-4. 校验 messages row + 最新 assistant
            msg = await self._verify_target_assistant_latest(
                db,
                session_id=session_id,
                assistant_message_id=assistant_id,
            )

            # 5. base_content_sha256 一致性校验
            current_sha = self._sha256_of_content_json(msg["content_json"])
            if current_sha != revision["base_content_sha256"]:
                raise RevisionBaseContentChangedError(
                    "messages.content_json has changed since this revision was created"
                )

            # 6. 首次成功：INSERT revision 0（WHERE NOT EXISTS 保证幂等）
            old_content_json = msg["content_json"]
            old_content_sha = revision["base_content_sha256"]
            revision_zero_id = self._gen_revision_id()
            await db.execute(
                """
                INSERT INTO web_message_revisions
                    (id, session_id, assistant_message_id, revision_number,
                     request_id, status, base_content_sha256, content_json,
                     created_at, completed_at, error_summary)
                SELECT ?, ?, ?, 0, NULL, 'superseded', ?, ?,
                       ?, ?, NULL
                WHERE NOT EXISTS (
                    SELECT 1 FROM web_message_revisions
                    WHERE assistant_message_id = ?
                      AND revision_number = 0
                )
                """,
                (
                    revision_zero_id, session_id, assistant_id,
                    old_content_sha, old_content_json,
                    revision["created_at"], now,
                    assistant_id,
                ),
            )

            # 7. 旧 completed → superseded（必须先于步骤 8）
            await db.execute(
                """
                UPDATE web_message_revisions
                SET status = 'superseded', completed_at = ?
                WHERE assistant_message_id = ?
                  AND status = 'completed'
                  AND id != ?
                """,
                (now, assistant_id, revision_id),
            )

            # 8. 本 running → completed + 写 candidate
            cursor = await db.execute(
                """
                UPDATE web_message_revisions
                SET status = 'completed',
                    content_json = ?,
                    completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (candidate_content_json, now, revision_id),
            )
            if cursor.rowcount != 1:
                raise RevisionStateTransitionError(
                    "revision is no longer running (concurrent state change)"
                )

            # 9. UPDATE messages.content_json（id / idx / created_at 保持不变）
            cursor = await db.execute(
                """
                UPDATE messages
                SET content_json = ?
                WHERE id = ? AND session_id = ? AND role = 'assistant'
                """,
                (candidate_content_json, assistant_id, session_id),
            )
            if cursor.rowcount != 1:
                raise ExtensionStoreError(
                    "finalize failed: target message row missing or not assistant"
                )

            # 推 session.updated_at——注意 sessions 表用 INTEGER（ms timestamp），
            # 不要与 revision 表的 TEXT (ISO) 混淆
            await db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_now_ms(), session_id),
            )

            await db.commit()
        except Exception:
            await db.rollback()
            raise

        result = await self.get_revision(revision_id)
        assert result is not None, "finalized revision missing"
        return result

    async def mark_revision_error(
        self,
        *,
        revision_id: str,
        request_id: str,
        error_summary: str,
    ) -> PersistedMessageRevision:
        """running → error。条件 UPDATE WHERE status='running' + rowcount 检查。

        幂等：已是 error → 返回现有；其它终态 → RevisionStateTransitionError。
        error_summary 安全截断到 500 字符。
        """
        return await self._transition_terminal(
            revision_id=revision_id,
            request_id=request_id,
            new_status="error",
            error_summary=self._truncate_error_summary(error_summary),
        )

    async def mark_revision_aborted(
        self,
        *,
        revision_id: str,
        request_id: str,
    ) -> PersistedMessageRevision:
        """running → aborted。条件 UPDATE WHERE status='running' + rowcount 检查。"""
        return await self._transition_terminal(
            revision_id=revision_id,
            request_id=request_id,
            new_status="aborted",
            error_summary=None,
        )

    async def _transition_terminal(
        self,
        *,
        revision_id: str,
        request_id: str,
        new_status: str,
        error_summary: str | None,
    ) -> PersistedMessageRevision:
        """共享 terminal 转换逻辑——error / aborted。

        幂等：已是目标状态 → 返回现有（不改 completed_at / error_summary）
        非法：其它终态 → RevisionStateTransitionError
        合法：running → new_status，条件 UPDATE
        """
        db = self._require_db()
        now = self._now_iso()
        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                "SELECT * FROM web_message_revisions WHERE id = ?",
                (revision_id,),
            )
            revision = await cursor.fetchone()
            await cursor.close()
            if revision is None:
                raise RevisionNotFoundError("revision not found")

            if revision["request_id"] != request_id:
                raise RevisionRequestConflictError(
                    "request_id does not match this revision"
                )

            current_status = revision["status"]

            # 幂等：已是目标状态
            if current_status == new_status:
                await db.commit()
                return self._row_to_revision(revision)

            # 非法：其它终态（completed / superseded / 其它 error 或 aborted）
            if current_status != "running":
                raise RevisionStateTransitionError(
                    f"cannot transition revision from {current_status!r} "
                    f"to {new_status!r}"
                )

            cursor = await db.execute(
                """
                UPDATE web_message_revisions
                SET status = ?, completed_at = ?, error_summary = ?
                WHERE id = ? AND request_id = ? AND status = 'running'
                """,
                (new_status, now, error_summary, revision_id, request_id),
            )
            if cursor.rowcount != 1:
                raise RevisionStateTransitionError(
                    "revision state changed during transition (concurrent write)"
                )

            await db.commit()
        except Exception:
            await db.rollback()
            raise

        result = await self.get_revision(revision_id)
        assert result is not None, "transitioned revision missing"
        return result

    async def mark_running_revisions_interrupted(
        self,
        *,
        completed_at: str,
    ) -> int:
        """Startup sweep——所有 status='running' 改为 'interrupted'。

        单 BEGIN IMMEDIATE transaction UPDATE。返回受影响行数；重复执行返回 0。
        **不**修改 messages / completed / superseded / error / aborted；
        **不**恢复 request registry；**不**写 candidate content。

        接入 lifespan 留到 D2-6；本方法 D2-3 完成。
        """
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE web_message_revisions
                SET status = 'interrupted',
                    completed_at = ?,
                    error_summary = 'Generation interrupted by server restart'
                WHERE status = 'running'
                """,
                (completed_at,),
            )
            affected = cursor.rowcount
            await db.commit()
            return affected
        except Exception:
            await db.rollback()
            raise


__all__ = [
    "ExtensionStoreError",
    "ExtensionStoreValidationError",
    "ExtensionStoreConflictError",
    "RevisionError",
    "RevisionNotFoundError",
    "RevisionTargetNotFoundError",
    "RevisionTargetNotAssistantError",
    "RevisionTargetNotLatestError",
    "RevisionAlreadyRunningError",
    "RevisionRequestConflictError",
    "RevisionBaseContentChangedError",
    "RevisionStateTransitionError",
    "PersistedSkill",
    "PersistedMCPServer",
    "PersistedDisabledTool",
    "SkillRowResult",
    "MCPServerRowResult",
    "PersistedMessageRevision",
    "ExtensionSQLiteStore",
    "SCHEMA_VERSION",
]
