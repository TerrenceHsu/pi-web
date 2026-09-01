"""Harness session memory and store contracts.

RequestSnapshot 记录"一次请求发生了什么"；SessionMemory 记录"一个会话到目前为止
积累了什么"。两者职责分离：

```text
RequestSnapshot ：一次请求的不可变快照（内含 TurnSnapshot 切片）
SessionMemory   ：跨多次请求的累计状态——messages + snapshots + metadata
```

Step 12 引入：

- `SessionState`            ——会话状态（Pydantic BaseModel，可序列化）
- `SessionMemory`           ——会话对象（包装 SessionState，提供 append / restore / clear / 序列化）
- `SessionStore`            ——持久化抽象（save / load / exists）
- `InMemorySessionStore`    ——内存实现（用于测试 / 临时会话）
- `JsonFileSessionStore`    ——append-only JSON journal（torn-tail recovery）
- `serialize_message`       ——AgentMessage → dict
- `serialize_messages`      ——list[AgentMessage] → list[dict]
- `deserialize_message`     ——dict → AgentMessage
- `deserialize_messages`    ——list[dict] → list[AgentMessage]
- `deserialize_snapshot`    ——dict → TurnSnapshot

AgentHarness 集成：

- `harness.session`          ——当前附加的 SessionMemory | None
- `harness.attach_session()` ——附加 + 可选恢复 Agent messages
- `harness.detach_session()` ——解附加，返回原 session
- `harness.save_session()`   ——便利方法，存到 SessionStore
- `harness.load_session()`   ——便利方法，从 SessionStore 加载并附加
- `_finish_snapshot` 自动 `session.append_snapshot(snapshot)`

Step 12 **不做**：Skills / Compaction / Vector Memory / Long-term user memory /
Branch Summary / 自动摘要 / 自动压缩上下文。
"""
from __future__ import annotations

import abc
import asyncio
import copy
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ...messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    SummaryMessage,
    ToolResultMessage,
    UserMessage,
)
from ..snapshot import RequestSnapshot

# ============================================================================
# 时间戳 / ID 生成
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _gen_session_id() -> str:
    """生成 session id：`sess-{毫秒时间戳}-{uuid 后 8 位}`。"""
    suffix = uuid.uuid4().hex[:8]
    return f"sess-{_now_ms()}-{suffix}"


# ============================================================================
# AgentMessage 序列化 / 反序列化
# ============================================================================


#: 当前支持的 AgentMessage role → Pydantic 类映射。
_MESSAGE_ROLE_MAP: dict[str, type[BaseModel]] = {
    "user": UserMessage,
    "assistant": AssistantMessage,
    "toolResult": ToolResultMessage,
    "summary": SummaryMessage,
    "custom": CustomMessage,
}


def serialize_message(msg: AgentMessage) -> dict[str, Any]:
    """AgentMessage → 可 JSON 序列化的 dict。

    走 Pydantic `model_dump(mode="json")`——所有嵌套对象递归 dump。
    """
    return msg.model_dump(mode="json")


def serialize_messages(messages: list[AgentMessage]) -> list[dict[str, Any]]:
    """list[AgentMessage] → list[dict]。"""
    return [serialize_message(m) for m in messages]


def deserialize_message(data: dict[str, Any]) -> AgentMessage:
    """dict → AgentMessage。

    按 `data["role"]` 派发到对应 Pydantic 类：
    - "user"       → UserMessage
    - "assistant"  → AssistantMessage
    - "toolResult" → ToolResultMessage

    其它 role 抛 ValueError。
    """
    role = data.get("role")
    cls = _MESSAGE_ROLE_MAP.get(role) if isinstance(role, str) else None
    if cls is None:
        raise ValueError(
            f"deserialize_message: 不支持的 role {role!r}；"
            f"当前仅支持 {list(_MESSAGE_ROLE_MAP.keys())}"
        )
    return cls.model_validate(data)  # type: ignore[return-value]


def deserialize_messages(items: list[dict[str, Any]]) -> list[AgentMessage]:
    """list[dict] → list[AgentMessage]。"""
    return [deserialize_message(d) for d in items]


# ============================================================================
# Snapshot 序列化 / 反序列化
# ============================================================================


def deserialize_snapshot(data: dict[str, Any]) -> RequestSnapshot:
    """dict → RequestSnapshot；旧版 request-shaped TurnSnapshot 自动兼容。"""
    return RequestSnapshot.model_validate(data)


# ============================================================================
# SessionState
# ============================================================================


class SessionState(BaseModel):
    """会话状态——可序列化的会话级数据。

    字段：
      id                session id（sess-{ts}-{suffix} 或用户传入）
      title              可选标题
      created_at         创建时间（毫秒）
      updated_at         最近更新时间（毫秒）
      messages           当前会话最终 messages 的 dict 列表（每次 turn 后覆盖）
      snapshots          所有 TurnSnapshot 的 dict 列表
      turn_count         已记录的 turn 数（= len(snapshots)）
      metadata           会话级 metadata（project / tags / source / notes 等）
      compactions        CompactionResult 的 dict 列表（Step 15）
      branch_summaries   BranchSummary 的 dict 列表（Step 15）

    约定：
    - messages / snapshots 都是 list[dict]，不直接持有 Pydantic 对象
      （便于序列化到 JSON / SQLite / 任何后端）
    - turn_count 与 len(snapshots) 始终一致，但显式字段方便外部读取
    - id / created_at / title / metadata 在 clear() 后保持不变
    - compactions 不会反向修改 snapshots——snapshots 是历史记录，永久保留
    """
    id: str
    title: str | None = None
    created_at: int
    updated_at: int

    messages: list[dict[str, Any]] = Field(default_factory=list)
    snapshots: list[dict[str, Any]] = Field(default_factory=list)

    turn_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Step 15 新增
    compactions: list[dict[str, Any]] = Field(default_factory=list)
    branch_summaries: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# SessionMemory
# ============================================================================


class SessionMemory:
    """会话级长期上下文。

    构造（两种）：

    ```python
    # 1. 新建
    session = SessionMemory(session_id="demo", title="Demo", metadata={"k": "v"})

    # 2. 基于已有 state（恢复）
    session = SessionMemory(state=existing_state)
    ```

    主要 API：

    - `append_snapshot(snapshot)` ——请求结束后追加；自动更新 messages / turn_count
    - `set_messages(messages)`    ——手动同步当前 messages
    - `get_messages()`            ——返回反序列化后的 list[AgentMessage]
    - `get_snapshots()`           ——返回反序列化后的 list[RequestSnapshot]
    - `clear()`                   ——清空 messages / snapshots，保留 id / created_at / title
    - `update_metadata(values)`   ——合并写入 metadata
    - `set_title(title)`          ——改标题
    - `to_dict / to_json`         ——序列化
    - `from_dict / from_json`     ——反序列化

    设计要点：
    - Session 依附 Harness，不塞进 Agent 内核
    - Session 不持有 Agent 引用（循环引用 + 可变性）
    - error / aborted snapshot 也会被 append——有调试价值
    - messages 来自最后一个 snapshot.messages_after（若非空）
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        state: SessionState | None = None,
    ):
        now = _now_ms()
        if state is not None:
            self._state: SessionState = state
        else:
            self._state = SessionState(
                id=session_id or _gen_session_id(),
                title=title,
                created_at=now,
                updated_at=now,
                metadata=dict(metadata) if metadata else {},
            )

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self._state.id

    @property
    def state(self) -> SessionState:
        return self._state

    # ------------------------------------------------------------------
    # 写入：append_snapshot / set_messages
    # ------------------------------------------------------------------

    def append_snapshot(self, snapshot: RequestSnapshot) -> None:
        """追加一次 turn 的 snapshot。

        - snapshots 列表 append snapshot.to_dict()
        - turn_count 重算为 len(snapshots)
        - updated_at 刷新
        - 若 snapshot.messages_after 非空：用其覆盖 state.messages

        注意：即使 snapshot.status == "error" / "aborted"，也会 append——
        error / aborted snapshot 有调试价值；session 不挑食。
        """
        self._state.snapshots.append(snapshot.to_dict())
        self._state.turn_count = len(self._state.snapshots)
        self._state.updated_at = _now_ms()

        if snapshot.messages_after:
            # list[dict] 拷贝——snapshot.messages_after 已经是 dict 列表
            self._state.messages = list(snapshot.messages_after)

    def set_messages(self, messages: list[AgentMessage]) -> None:
        """手动把 Agent 当前 messages 同步进 session。

        通常用于：attach_session(restore_messages=False) 后调方希望手动同步。
        """
        self._state.messages = serialize_messages(messages)
        self._state.updated_at = _now_ms()

    # ------------------------------------------------------------------
    # 读取：get_messages / get_snapshots
    # ------------------------------------------------------------------

    def get_messages(self) -> list[AgentMessage]:
        """返回反序列化后的 list[AgentMessage]。

        可用于：attach_session 时把 messages 注入新 Agent。
        """
        return deserialize_messages(self._state.messages)

    def get_snapshots(self) -> list[RequestSnapshot]:
        """返回反序列化后的 list[RequestSnapshot]。"""
        return [deserialize_snapshot(d) for d in self._state.snapshots]

    # ------------------------------------------------------------------
    # Step 15：compaction / branch summary
    # ------------------------------------------------------------------

    def append_compaction(self, result: Any) -> None:
        """追加一次 compaction 结果。

        - compactions 列表 append result.to_dict()
        - 若 result.applied：用 result.new_messages 覆盖 state.messages
        - 不删除 snapshots——snapshots 是历史记录，永久保留
        - 刷新 updated_at

        参数类型注为 Any 避免硬 import 循环；实际期望 CompactionResult。
        """
        from ..compaction import CompactionResult  # 局部 import

        if not isinstance(result, CompactionResult):
            raise TypeError(
                f"append_compaction 期望 CompactionResult，实际 {type(result).__name__}"
            )
        self._state.compactions.append(result.to_dict())
        if result.applied:
            self._state.messages = list(result.new_messages)
        self._state.updated_at = _now_ms()

    def get_compactions(self) -> list[Any]:
        """返回反序列化后的 list[CompactionResult]。"""
        from ..compaction import CompactionResult  # 局部 import

        return [CompactionResult.model_validate(d) for d in self._state.compactions]

    def append_branch_summary(self, summary: Any) -> None:
        """追加一次 branch summary——不修改 messages。

        参数类型注为 Any 避免硬 import 循环；实际期望 BranchSummary。
        """
        from ..compaction import BranchSummary  # 局部 import

        if not isinstance(summary, BranchSummary):
            raise TypeError(
                f"append_branch_summary 期望 BranchSummary，实际 {type(summary).__name__}"
            )
        self._state.branch_summaries.append(summary.to_dict())
        self._state.updated_at = _now_ms()

    def get_branch_summaries(self) -> list[Any]:
        """返回反序列化后的 list[BranchSummary]。"""
        from ..compaction import BranchSummary  # 局部 import

        return [BranchSummary.model_validate(d) for d in self._state.branch_summaries]

    def clear_branch_summaries(self) -> None:
        """清空 branch_summaries；不动 messages / snapshots / compactions。"""
        self._state.branch_summaries = []
        self._state.updated_at = _now_ms()

    # ------------------------------------------------------------------
    # clear / metadata / title
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空 messages / snapshots / turn_count；保留 id / created_at / title / metadata。

        注意：默认 clear **不清** compactions / branch_summaries——它们是历史记录。
        若需清空，调 clear_branch_summaries()（compactions 暂不提供 clear 方法，
        历史压缩记录不应清空）。
        """
        self._state.messages = []
        self._state.snapshots = []
        self._state.turn_count = 0
        self._state.updated_at = _now_ms()

    def update_metadata(self, values: dict[str, Any]) -> None:
        """合并写入 metadata。"""
        self._state.metadata.update(values)
        self._state.updated_at = _now_ms()

    def set_title(self, title: str | None) -> None:
        """改标题。"""
        self._state.title = title
        self._state.updated_at = _now_ms()

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return self._state.model_dump(mode="json")

    def to_json(self, indent: int | None = None) -> str:
        """导出为 JSON 字符串（ensure_ascii=False，保留中文等）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMemory:
        """从 dict 构造（与 to_dict 互逆）。"""
        state = SessionState.model_validate(data)
        return cls(state=state)

    @classmethod
    def from_json(cls, text: str) -> SessionMemory:
        """从 JSON 字符串构造。"""
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------
    # Step 13 别名 / clone
    # ------------------------------------------------------------------

    def clone(self) -> SessionMemory:
        """深拷贝当前 session；返回新 SessionMemory 实例。

        用途：测试、store 内部存储、导出时避免共享引用。
        内部走 `copy.deepcopy(self.to_dict())`——所有嵌套 dict / list 都独立。
        """
        return SessionMemory.from_dict(copy.deepcopy(self.to_dict()))

    def export_json(self, indent: int | None = 2) -> str:
        """to_json 的语义别名——默认 indent=2，便于外部接口语义清晰。"""
        return self.to_json(indent=indent)

    @classmethod
    def import_json(cls, text: str) -> SessionMemory:
        """from_json 的语义别名。"""
        return cls.from_json(text)


# ============================================================================
# SessionStore 抽象
# ============================================================================


class SessionStore(abc.ABC):
    """Session 持久化抽象接口。

    Step 12 只要求 save / load / exists 三个方法，全部 async。
    实现可以做真异步 IO（如网络存储），也可以做同步 IO 包一层（如本步的两个实现）。
    """

    @abc.abstractmethod
    async def save(self, session: SessionMemory) -> None:
        """Durably save the latest session state."""
        ...

    @abc.abstractmethod
    async def load(self, session_id: str) -> SessionMemory:
        """加载 session。不存在抛 FileNotFoundError（或实现特定的异常）。"""
        ...

    @abc.abstractmethod
    async def exists(self, session_id: str) -> bool:
        """检查 session 是否存在。"""
        ...


# ============================================================================
# InMemorySessionStore
# ============================================================================


class InMemorySessionStore(SessionStore):
    """内存 SessionStore——dict 实现，进程退出即丢。

    用途：测试、临时会话、单进程内多 harness 共享 session。

    Step 13 改进：save / load 走 copy.deepcopy——避免外部修改 session 后污染
    store 内部数据。语义与 JsonFileSessionStore 一致：写入时是"那一刻的快照"。
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    async def save(self, session: SessionMemory) -> None:
        # deep copy——保存当时快照；外部后续修改不影响 store
        self._items[session.id] = copy.deepcopy(session.to_dict())

    async def load(self, session_id: str) -> SessionMemory:
        if session_id not in self._items:
            raise FileNotFoundError(
                f"InMemorySessionStore: session {session_id!r} 不存在"
            )
        # deep copy 出去——调方修改不影响 store
        return SessionMemory.from_dict(copy.deepcopy(self._items[session_id]))

    async def exists(self, session_id: str) -> bool:
        return session_id in self._items


# ============================================================================
# JsonFileSessionStore
# ============================================================================


#: 安全 session_id 字符集——[a-zA-Z0-9_.-]+，长度 1-128。
#: 用于防止路径穿越（../etc/passwd 之类）。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_session_id(session_id: str) -> None:
    """校验 session_id 是否是安全的文件名。

    允许：字母 / 数字 / 下划线 / 点 / 连字符；长度 1-128。
    拒绝：空字符串、含 / 含 \\、含 ..（路径穿越）、含其它特殊字符。
    """
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"非法 session_id {session_id!r}；"
            f"仅允许字母 / 数字 / 下划线 / 点 / 连字符，长度 1-128"
        )
    # 显式拒绝 ..（路径穿越）——regex 不会拒绝 ".." / "a..b" 等
    if ".." in session_id:
        raise ValueError(
            f"非法 session_id {session_id!r}；不能包含 '..'（防路径穿越）"
        )


class JsonFileSessionStore(SessionStore):
    """Append-only JSON journal——每个 session 一个 `{id}.json` 文件。

    构造：

    ```python
    store = JsonFileSessionStore("./sessions")
    ```

    - 自动创建 root_dir
    - session_id 必须匹配 `^[A-Za-z0-9_.-]{1,128}$`，防路径穿越
    - 新文件写 header + snapshot；后续 save 每次只追加一个 snapshot record
    - 旧单对象 JSON 在第一次 save 时通过 temp + replace 一次性迁移
    - 启动读取可截断 torn final record；中间损坏仍明确报错
    - resolved save 会 flush + fsync；同实例内按 session 串行 append
    """

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _path_for(self, session_id: str) -> Path:
        _validate_session_id(session_id)
        return self.root_dir / f"{session_id}.json"

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    @staticmethod
    def _journal_line(value: dict[str, Any]) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _journal_bytes(
        self,
        session_id: str,
        snapshots: list[dict[str, Any]],
    ) -> bytes:
        chunks = [
            self._journal_line({
                "kind": "session_journal",
                "version": 1,
                "session_id": session_id,
            })
        ]
        chunks.extend(
            self._journal_line({"kind": "snapshot", "snapshot": snapshot})
            for snapshot in snapshots
        )
        return b"".join(chunks)

    @staticmethod
    def _atomic_replace(path: Path, raw: bytes) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _load_path(
        self, path: Path, *, repair_torn_tail: bool,
    ) -> tuple[SessionMemory, bool]:
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        if not lines:
            raise ValueError(f"empty session file: {path.name}")
        try:
            first = json.loads(lines[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Pretty-printed legacy JSON commonly starts with a line containing
            # only ``{``; parse the complete document before classifying it.
            return SessionMemory.from_json(raw.decode("utf-8")), False
        if not (
            isinstance(first, dict)
            and first.get("kind") == "session_journal"
            and first.get("version") == 1
        ):
            return SessionMemory.from_json(raw.decode("utf-8")), False

        latest: SessionMemory | None = None
        offset = len(lines[0])
        last_good_offset = offset
        for index, line in enumerate(lines[1:], start=1):
            if not line.strip():
                offset += len(line)
                last_good_offset = offset
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                is_tail = index == len(lines) - 1
                if repair_torn_tail and is_tail and latest is not None:
                    with path.open("r+b") as stream:
                        stream.truncate(last_good_offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                    break
                raise
            if not isinstance(record, dict) or record.get("kind") != "snapshot":
                raise ValueError(
                    f"invalid session journal record at line {index + 1}"
                )
            snapshot = record.get("snapshot")
            if not isinstance(snapshot, dict):
                raise ValueError(
                    f"invalid session journal snapshot at line {index + 1}"
                )
            latest = SessionMemory.from_dict(snapshot)
            offset += len(line)
            last_good_offset = offset
        if latest is None:
            raise ValueError(f"session journal has no snapshot: {path.name}")
        return latest, True

    async def save(self, session: SessionMemory) -> None:
        path = self._path_for(session.id)
        async with self._lock_for(session.id):
            snapshot = session.to_dict()
            if not path.exists():
                self._atomic_replace(
                    path, self._journal_bytes(session.id, [snapshot])
                )
                return
            previous, is_journal = self._load_path(
                path, repair_torn_tail=True
            )
            if not is_journal:
                self._atomic_replace(
                    path,
                    self._journal_bytes(
                        session.id, [previous.to_dict(), snapshot]
                    ),
                )
                return
            with path.open("ab") as stream:
                stream.write(self._journal_line({
                    "kind": "snapshot",
                    "snapshot": snapshot,
                }))
                stream.flush()
                os.fsync(stream.fileno())

    async def load(self, session_id: str) -> SessionMemory:
        path = self._path_for(session_id)
        if not path.exists():
            raise FileNotFoundError(
                f"JsonFileSessionStore: session {session_id!r} 不存在 ({path})"
            )
        async with self._lock_for(session_id):
            session, _is_journal = self._load_path(
                path, repair_torn_tail=True
            )
            return session

    async def exists(self, session_id: str) -> bool:
        # 与 save / load 一致——非法 session_id 抛 ValueError，不静默返回 False
        return self._path_for(session_id).exists()


__all__ = [
    # State / Memory
    "SessionState", "SessionMemory",
    # Store
    "SessionStore", "InMemorySessionStore", "JsonFileSessionStore",
    # 序列化
    "serialize_message", "serialize_messages",
    "deserialize_message", "deserialize_messages",
    "deserialize_snapshot",
]
