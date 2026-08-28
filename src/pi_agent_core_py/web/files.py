"""WorkspaceStore —— 会话级 Workspace 与文件存储。

设计要点：
- 每个 session 初始化一个目录：`uploads/{session_id}/`
- 按 session_id 分桶：`uploads/{session_id}/{file_id}/{safe_filename}`
- 用户上传和 Agent 创建的文件使用同一份 metadata / 容量 / 隔离规则
- 文件 metadata 用独立 `metadata.json`（每文件一个），避免单 manifest 并发写
- 文件名 sanitize + Path.resolve 边界检查双重防路径穿越
- 单文件 25 MB / session 总量 100 MB（可配置）
- 流式读取 UploadFile（chunk 64KB），不在内存里堆
- 跨 session 访问通过 `get_for_session(sid, fid)` 强校验
- 不依赖 sqlite；文件 metadata 与文件内容同生共死
- 删 session 时调 `delete_session_files(sid)` 级联清理整个目录

异常体系：
- FileStoreError 基类
- VirtualFileNotFoundError —— 文件不存在
- FileAccessDeniedError —— 跨 session 访问
- FileTooLargeError —— 单文件超限
- SessionStorageLimitError —— session 总量超限
- UnsafeFilenameError —— 路径穿越 / 不安全文件名
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ============================================================================
# 常量
# ============================================================================


#: 默认单文件大小上限：25 MB
DEFAULT_MAX_FILE_SIZE = 25 * 1024 * 1024

#: 默认单 session 总上传上限：100 MB
DEFAULT_MAX_SESSION_SIZE = 100 * 1024 * 1024

#: Session 根目录中的 Agent 指令文件（用户明确要求使用单数文件名）。
AGENT_INSTRUCTIONS_PATH = "AGENT.md"

#: Session 根目录中的持久记忆文件。
MEMORY_PATH = "Memory.md"

#: Agent 与用户代码的唯一逻辑根。目录在第一份代码出现时自然进入文件树。
SCRIPTS_PATH = "scripts"

#: Workspace revision 的隐藏持久化状态；不属于用户可见文件树。
WORKSPACE_STATE_FILENAME = ".workspace.json"

#: WorkspaceStore logical-tree materialization contract used by managed Sandboxes.
WORKSPACE_MATERIALIZATION_SCHEMA: Literal["pi-agent-workspace-materialization/v1"] = (
    "pi-agent-workspace-materialization/v1"
)

#: Sandbox Publisher to WorkspaceStore transaction contract.
WORKSPACE_PUBLISH_TRANSACTION_SCHEMA: Literal[
    "pi-agent-workspace-publish-transaction/v1"
] = "pi-agent-workspace-publish-transaction/v1"
WORKSPACE_PUBLISH_PHASE_SCHEMA: Literal["pi-agent-workspace-publish-phase/v1"] = (
    "pi-agent-workspace-publish-phase/v1"
)

#: 明确视为可执行/工程代码的扩展名。配置和普通文本不自动搬入 scripts。
CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".bash",
    ".c",
    ".cc",
    ".cjs",
    ".cpp",
    ".cs",
    ".css",
    ".fish",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".less",
    ".lua",
    ".mjs",
    ".php",
    ".py",
    ".pyi",
    ".r",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
    ".zsh",
})

MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".mdx"})

#: 新 Session 的安全模板；已有内容不会在登录或重启时被覆盖。
DEFAULT_AGENT_INSTRUCTIONS = """# AGENT.md

This file contains instructions for the agent in this conversation.

## Objective

Describe the objective of this session here.

## Constraints

- Add session-specific constraints here.

## Output preferences

- Add preferred output formats or folders here.
"""

#: 新 Session 的空记忆文档；Checkpointer 首次运行后会原子替换其正文。
DEFAULT_MEMORY = "# Memory\n"

#: 流式读 chunk 大小：64 KB
_CHUNK_SIZE = 64 * 1024

#: 文件名最大长度
_MAX_FILENAME_LEN = 255

#: 安全文件名正则——保留字母 / 数字 / 下划线 / 连字符 / 点 / 空格 / 中日韩字符；
#: 去掉路径分隔符 / 控制字符 / 特殊字符
_SAFE_FILENAME_RE = re.compile(r"[^\w.\- \u4e00-\u9fff]+", flags=re.UNICODE)


# ============================================================================
# 异常
# ============================================================================


class FileStoreError(Exception):
    """WorkspaceStore 基类异常。"""


class VirtualFileNotFoundError(FileStoreError):
    """文件不存在。"""


class FileAccessDeniedError(FileStoreError):
    """跨 session 访问 / 无权限。"""


class FileTooLargeError(FileStoreError):
    """单文件超过 max_file_size。"""

    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(
            f"file size {size} bytes exceeds max_file_size {limit} bytes"
        )


class SessionStorageLimitError(FileStoreError):
    """session 总量超过 max_session_size。"""

    def __init__(self, current: int, new: int, limit: int):
        self.current = current
        self.new = new
        self.limit = limit
        super().__init__(
            f"session storage limit exceeded: current={current} + new={new} > "
            f"max_session_size={limit}"
        )


class UnsafeFilenameError(FileStoreError):
    """文件名不安全（路径穿越 / 控制字符）。"""


class FileVersionConflictError(FileStoreError):
    """文件更新时 expected sha256 与当前版本不一致。"""


class WorkspaceVersionConflictError(FileStoreError):
    """Workspace mutation 使用了过期 revision。"""

    def __init__(self, expected: int, current: int):
        self.expected = expected
        self.current = current
        super().__init__(
            f"workspace changed since it was opened: expected revision "
            f"{expected}, current revision {current}"
        )


class WorkspacePathConflictError(FileStoreError):
    """目标逻辑路径已被另一个文件占用。"""


class WorkspaceTreeConflictError(FileStoreError):
    """Workspace bytes no longer match an immutable Sandbox baseline."""


class WorkspacePublishPolicyError(FileStoreError):
    """A Sandbox artifact attempted to mutate a protected Workspace path."""


# ============================================================================
# FileRef
# ============================================================================


class FileRef(BaseModel):
    """单文件 metadata（持久化在 metadata.json 中）。"""

    id: str
    session_id: str
    name: str
    size: int
    mime: str
    sha256: str
    path: str
    created_at: int
    logical_path: str = ""
    origin: Literal["system", "upload", "agent", "user", "legacy"] = "legacy"
    purpose: Literal["file", "agent_instructions", "memory"] = "file"
    updated_at: int | None = None

    @model_validator(mode="after")
    def _fill_backward_compatible_fields(self) -> FileRef:
        """旧 metadata 按原文件名和创建时间补齐新增字段。"""
        if not self.logical_path:
            self.logical_path = self.name
        if self.updated_at is None:
            self.updated_at = self.created_at
        return self


class WorkspaceState(BaseModel):
    """Session Workspace 的持久化并发状态。"""

    schema_version: Literal[1] = 1
    session_id: str
    revision: int = Field(ge=0)
    created_at: int
    updated_at: int


class WorkspaceMaterializationEntry(BaseModel):
    """One logical Workspace file copied into an immutable staging tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceMaterialization(BaseModel):
    """Revision-bound logical tree produced without storage metadata leakage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-workspace-materialization/v1"] = (
        WORKSPACE_MATERIALIZATION_SCHEMA
    )
    session_id: str
    revision: int = Field(ge=0)
    root_path: Path
    entries: tuple[WorkspaceMaterializationEntry, ...]
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_tree(self) -> WorkspaceMaterialization:
        paths = tuple(entry.logical_path for entry in self.entries)
        if paths != tuple(sorted(paths)):
            raise ValueError("Workspace materialization entries must be sorted")
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("Workspace materialization paths must be unique")
        if self.file_count != len(self.entries):
            raise ValueError("Workspace materialization file_count is inconsistent")
        if self.total_bytes != sum(entry.size for entry in self.entries):
            raise ValueError("Workspace materialization total_bytes is inconsistent")
        if self.tree_sha256 != _workspace_materialization_digest(self.entries):
            raise ValueError("Workspace materialization tree SHA is inconsistent")
        return self


class WorkspacePublishChange(BaseModel):
    """One verified final file supplied by a Sandbox artifact publisher."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_path: str
    source_path: Path
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspacePublishResult(BaseModel):
    """Observable result of one atomic WorkspaceStore publish transaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: str = Field(pattern=r"^publish-[0-9a-f]{32}$")
    previous_revision: int = Field(ge=0)
    revision: int = Field(ge=0)
    changed_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]


class _WorkspacePublishIntentChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_path: str
    before_ref: FileRef | None
    after_ref: FileRef
    staged_path: str


class _WorkspacePublishIntentDelete(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_path: str
    before_ref: FileRef
    tombstone_path: str


class _WorkspacePublishIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-workspace-publish-transaction/v1"] = (
        WORKSPACE_PUBLISH_TRANSACTION_SCHEMA
    )
    transaction_id: str = Field(pattern=r"^publish-[0-9a-f]{32}$")
    session_id: str
    expected_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_state: WorkspaceState
    after_state: WorkspaceState
    changes: tuple[_WorkspacePublishIntentChange, ...]
    deletions: tuple[_WorkspacePublishIntentDelete, ...]


class _WorkspacePublishPhase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-workspace-publish-phase/v1"] = (
        WORKSPACE_PUBLISH_PHASE_SCHEMA
    )
    phase: Literal["prepared", "committing", "committed"]


# ============================================================================
# 工具函数
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _gen_file_id() -> str:
    return f"file-{_now_ms()}-{uuid.uuid4().hex[:8]}"


def sanitize_filename(name: str) -> str:
    """文件名 sanitize：去除路径分隔符 / 控制字符 / 特殊字符。

    返回安全的基名（不含任何路径）。空 / 全部去掉后空 → "upload.bin"。
    长度截断到 255。
    """
    if not isinstance(name, str) or not name:
        return "upload.bin"
    # 取基名（剥掉任何路径前缀），防御 "..\\evil.txt" / "/etc/passwd"
    base = Path(name).name
    # 去掉控制字符（ord < 32） + 替换不安全字符
    cleaned = _SAFE_FILENAME_RE.sub("_", base)
    # 去掉 Windows 保留名前缀的 ".." / "." 段
    cleaned = cleaned.lstrip(".")
    # 控制字符二次防御
    cleaned = "".join(c for c in cleaned if ord(c) >= 32)
    # 长度截断（保留扩展名：先看有没有 .）
    if len(cleaned) > _MAX_FILENAME_LEN:
        # 简单截断——保留扩展名
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 16:
            cleaned = stem[: _MAX_FILENAME_LEN - len(ext) - 1] + dot + ext
        else:
            cleaned = cleaned[:_MAX_FILENAME_LEN]
    if not cleaned:
        return "upload.bin"
    return cleaned


def normalize_logical_path(filename: str, folder: str | None = None) -> str:
    """构造安全的 Session 内逻辑路径，不映射为物理磁盘路径。"""
    safe_name = sanitize_filename(filename)
    if folder is None or not folder.strip():
        return safe_name

    normalized = folder.replace("\\", "/").strip()
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnsafeFilenameError("logical folder must be relative")
    raw_parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise UnsafeFilenameError("logical folder contains an unsafe segment")
    safe_parts = [sanitize_filename(part) for part in raw_parts]
    return "/".join([*safe_parts, safe_name])


def normalize_workspace_logical_path(logical_path: str) -> str:
    """严格校验用户/Agent 提供的 Workspace 相对 POSIX 路径。"""
    if not isinstance(logical_path, str) or not logical_path:
        raise UnsafeFilenameError("workspace logical path must be non-empty")
    if logical_path != logical_path.strip():
        raise UnsafeFilenameError("workspace logical path cannot have outer whitespace")
    if len(logical_path) > 1024:
        raise UnsafeFilenameError("workspace logical path is too long")
    if "\\" in logical_path:
        raise UnsafeFilenameError("workspace logical path must use '/' separators")
    if logical_path.startswith("/") or re.match(r"^[A-Za-z]:", logical_path):
        raise UnsafeFilenameError("workspace logical path must be relative")
    if any(ord(char) < 32 or ord(char) == 127 for char in logical_path):
        raise UnsafeFilenameError("workspace logical path contains control characters")

    parts = logical_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafeFilenameError("workspace logical path contains an unsafe segment")
    if parts[0].casefold() == ".pi-agent":
        raise UnsafeFilenameError(".pi-agent is reserved for system state")
    for part in parts:
        if len(part) > _MAX_FILENAME_LEN:
            raise UnsafeFilenameError("workspace path segment is too long")
        if sanitize_filename(part) != part:
            raise UnsafeFilenameError(
                f"workspace logical path contains an unsafe segment: {part!r}"
            )
    return "/".join(parts)


def is_code_filename(filename: str) -> bool:
    """Return whether a filename belongs under the managed scripts root."""
    return PurePosixPath(filename.casefold()).suffix in CODE_EXTENSIONS


def is_markdown_filename(filename: str) -> bool:
    """Return whether a filename is an editable Markdown document."""
    return PurePosixPath(filename.casefold()).suffix in MARKDOWN_EXTENSIONS


def workspace_logical_path(
    filename: str,
    folder: str | None = None,
    *,
    purpose: Literal["file", "agent_instructions", "memory"] = "file",
) -> str:
    """Resolve a managed file into its canonical Workspace logical path."""
    safe_name = sanitize_filename(filename)
    if purpose == "agent_instructions":
        return AGENT_INSTRUCTIONS_PATH
    if purpose == "memory":
        return MEMORY_PATH

    normalized_folder: str | None = None
    if folder is not None and folder.strip():
        normalized_folder = normalize_workspace_logical_path(folder)
    if is_code_filename(safe_name):
        if normalized_folder is None:
            normalized_folder = SCRIPTS_PATH
        elif normalized_folder.casefold() != SCRIPTS_PATH and not (
            normalized_folder.casefold().startswith(f"{SCRIPTS_PATH}/")
        ):
            normalized_folder = f"{SCRIPTS_PATH}/{normalized_folder}"

    logical_path = normalize_logical_path(safe_name, normalized_folder)
    logical_path = normalize_workspace_logical_path(logical_path)
    if logical_path.casefold() in {
        AGENT_INSTRUCTIONS_PATH.casefold(),
        MEMORY_PATH.casefold(),
    }:
        raise UnsafeFilenameError(
            "AGENT.md and Memory.md are reserved Workspace root files"
        )
    return logical_path


def _guess_mime(filename: str, content_type: str | None) -> str:
    """MIME：优先 content_type，否则 mimetypes.guess_type，否则 octet-stream。"""
    if content_type:
        return content_type
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _sha256_of_file(path: Path) -> str:
    """流式计算文件 sha256。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _workspace_materialization_digest(
    entries: tuple[WorkspaceMaterializationEntry, ...],
) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.logical_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def is_sandbox_publishable_workspace_path(logical_path: str) -> bool:
    """Return whether Sandbox output may replace this logical Workspace path."""
    try:
        normalized = normalize_workspace_logical_path(logical_path)
    except FileStoreError:
        return False
    if normalized != logical_path:
        return False
    parts = PurePosixPath(normalized).parts
    folded = tuple(part.casefold() for part in parts)
    if normalized.casefold() in {
        AGENT_INSTRUCTIONS_PATH.casefold(),
        MEMORY_PATH.casefold(),
    }:
        return False
    if folded[0] == "documents":
        return False
    if folded[0] == SCRIPTS_PATH and len(parts) > 1:
        return True
    return is_markdown_filename(parts[-1])


def _write_model_atomic(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    raw = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        with temp_path.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _regular_file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _is_link_reparse_or_non_regular(path: Path, info: os.stat_result) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or (is_junction is not None and is_junction())
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    )


def _ensure_plain_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    is_junction = getattr(path, "is_junction", None)
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or (is_junction is not None and is_junction())
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    ):
        raise UnsafeFilenameError("Workspace transaction directory is unsafe")


def _copy_verified_workspace_file(
    source: Path,
    target: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> tuple[int, str]:
    """Copy one immutable generation and reject path/content races."""
    before = source.stat(follow_symlinks=False)
    if _is_link_reparse_or_non_regular(source, before) or before.st_size != expected_size:
        raise FileStoreError("Workspace content metadata does not match a regular file")

    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    source_fd = os.open(source, source_flags)
    target_fd: int | None = None
    target_created = False
    digest = hashlib.sha256()
    copied = 0
    succeeded = False
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or _regular_file_identity(
            opened
        ) != _regular_file_identity(before):
            raise FileStoreError("Workspace content changed before materialization")
        target_fd = os.open(target, target_flags, 0o600)
        target_created = True
        while chunk := os.read(source_fd, _CHUNK_SIZE):
            copied += len(chunk)
            if copied > expected_size:
                raise FileStoreError("Workspace content changed during materialization")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                written = os.write(target_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("Workspace materialization write made no progress")
                offset += written
        os.fsync(target_fd)
        final_open = os.fstat(source_fd)
        final_path = source.stat(follow_symlinks=False)
        actual_sha256 = digest.hexdigest()
        if (
            _regular_file_identity(final_open) != _regular_file_identity(before)
            or _regular_file_identity(final_path) != _regular_file_identity(before)
            or _is_link_reparse_or_non_regular(source, final_path)
            or copied != expected_size
            or actual_sha256 != expected_sha256
        ):
            raise FileStoreError("Workspace content changed during materialization")
        succeeded = True
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)
        if target_created and not succeeded:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    return copied, digest.hexdigest()


def _verify_workspace_file(
    source: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Re-hash an immutable generation and reject link/content races."""
    before = source.stat(follow_symlinks=False)
    if _is_link_reparse_or_non_regular(source, before) or before.st_size != expected_size:
        raise WorkspaceTreeConflictError("Workspace content metadata no longer matches")
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, source_flags)
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _regular_file_identity(
            opened
        ) != _regular_file_identity(before):
            raise WorkspaceTreeConflictError("Workspace content changed before publish")
        while chunk := os.read(descriptor, _CHUNK_SIZE):
            size += len(chunk)
            if size > expected_size:
                raise WorkspaceTreeConflictError("Workspace content changed during publish")
            digest.update(chunk)
        final_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final_path = source.stat(follow_symlinks=False)
    if (
        _regular_file_identity(final_open) != _regular_file_identity(before)
        or _regular_file_identity(final_path) != _regular_file_identity(before)
        or _is_link_reparse_or_non_regular(source, final_path)
        or size != expected_size
        or digest.hexdigest() != expected_sha256
    ):
        raise WorkspaceTreeConflictError("Workspace content changed during publish")


def _extended_length_path(path: Path) -> Path:
    r"""把绝对路径归一为 Windows 扩展长度形式（``\\?\C:\...``）。

    未启用 LongPathsEnabled 注册表项时，普通路径超过 260 字符的文件 IO
    会以 FileNotFoundError / OSError 失败。store 的目录层级
    ``root/session_id/file_id/.<name>.<uuid>.tmp`` 在深嵌套 temp 目录下
    很容易越界；在根路径统一加 ``\\?\`` 前缀后所有派生路径自动支持
    32k 字符。非 Windows 平台与已带前缀的路径原样返回。
    """
    if os.name != "nt":
        return path
    text = os.path.abspath(os.fspath(path))
    if text.startswith("\\\\?\\"):
        return Path(text)
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text.lstrip("\\"))
    return Path("\\\\?\\" + text)


# ============================================================================
# WorkspaceStore
# ============================================================================


class WorkspaceStore:
    """Session Workspace 的唯一文件事实源。

    生命周期：
        store = WorkspaceStore(root_dir="./uploads")
        await store.init()                          # 创建 root_dir
        await store.ensure_session_workspace(sid)  # 初始化两个固定根文件
        ref = await store.save(session_id, upload)  # 流式读取 upload
        ref = await store.write_text(sid, name, content)  # Agent 创建文本文件
        ref2 = await store.get_for_session(sid, fid)
        await store.delete(fid)
        await store.delete_session_files(sid)

    并发：纯文件 IO，无共享状态。同一 file_id 只能由 store 内部 _gen_file_id
    生成；外部不可控。同 session 并发写不同 file_id 互不干扰。
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        max_session_size: int = DEFAULT_MAX_SESSION_SIZE,
    ):
        self._root_dir = _extended_length_path(Path(root_dir))
        self._max_file_size = max_file_size
        self._max_session_size = max_session_size
        self._initialized = False
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def max_file_size(self) -> int:
        return self._max_file_size

    @property
    def max_session_size(self) -> int:
        return self._max_session_size

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """创建 root_dir，并收敛上次退出留下的托管文件 generation。"""
        if self._initialized:
            return
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._recover_managed_files()
        self._initialized = True

    async def ensure_session_folder(self, session_id: str) -> Path:
        """创建并返回 session 工作目录（幂等且强制限制在 root_dir 内）。"""
        if not self._initialized:
            await self.init()
        session_dir = self._session_dir(session_id)
        resolved = self._resolve_and_check(
            session_dir,
            expect_under=self._root_dir,
        )
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _workspace_state_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / WORKSPACE_STATE_FILENAME

    def _read_workspace_state_unlocked(
        self,
        session_id: str,
    ) -> WorkspaceState | None:
        state_path = self._workspace_state_path(session_id)
        if not state_path.is_file():
            return None
        try:
            state = WorkspaceState.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise FileStoreError("workspace revision state is invalid") from exc
        if state.session_id != session_id:
            raise FileStoreError("workspace revision state belongs to another session")
        return state

    def _write_workspace_state_unlocked(self, state: WorkspaceState) -> None:
        session_dir = self._session_dir(state.session_id)
        state_path = self._workspace_state_path(state.session_id)
        temp_path = session_dir / f".workspace.{uuid.uuid4().hex}.tmp"
        self._resolve_and_check(state_path, expect_under=session_dir)
        self._resolve_and_check(temp_path, expect_under=session_dir)
        raw = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with temp_path.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(state_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    async def _ensure_workspace_state_unlocked(
        self,
        session_id: str,
    ) -> WorkspaceState:
        await self.ensure_session_folder(session_id)
        existing = self._read_workspace_state_unlocked(session_id)
        if existing is not None:
            return existing
        now = _now_ms()
        created = WorkspaceState(
            session_id=session_id,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self._write_workspace_state_unlocked(created)
        return created

    @staticmethod
    def _check_workspace_revision(
        state: WorkspaceState,
        expected_workspace_revision: int | None,
    ) -> None:
        if (
            expected_workspace_revision is not None
            and expected_workspace_revision != state.revision
        ):
            raise WorkspaceVersionConflictError(
                expected_workspace_revision,
                state.revision,
            )

    def _advance_workspace_revision_unlocked(
        self,
        state: WorkspaceState,
    ) -> WorkspaceState:
        advanced = state.model_copy(update={
            "revision": state.revision + 1,
            "updated_at": _now_ms(),
        })
        self._write_workspace_state_unlocked(advanced)
        return advanced

    async def get_workspace_state(self, session_id: str) -> WorkspaceState:
        """Read or migrate the persistent Workspace revision state."""
        async with self._session_lock(session_id):
            return await self._ensure_workspace_state_unlocked(session_id)

    async def materialize_workspace_revision(
        self,
        session_id: str,
        destination: Path,
        *,
        expected_workspace_revision: int | None = None,
    ) -> WorkspaceMaterialization:
        """Export one revision as a new logical tree without storage metadata.

        The Session mutation lock remains held while immutable content
        generations are copied and verified. The returned tree is independent
        from later Workspace mutations and is safe to feed into a Sandbox
        snapshot builder.
        """
        if not destination.is_absolute():
            raise UnsafeFilenameError("Workspace materialization path must be absolute")
        await self.ensure_session_workspace(session_id)
        target = _extended_length_path(destination).resolve(strict=False)
        uploads_root = self._root_dir.resolve(strict=False)
        if (
            target == uploads_root
            or target.is_relative_to(uploads_root)
            or uploads_root.is_relative_to(target)
        ):
            raise UnsafeFilenameError(
                "Workspace materialization must be outside the WorkspaceStore root"
            )

        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            refs = await self.list_session(session_id)
            materialized = await asyncio.to_thread(
                self._materialize_workspace_revision_unlocked,
                state,
                refs,
                target,
            )
            current = self._read_workspace_state_unlocked(session_id)
            if current is None or current.revision != state.revision:
                shutil.rmtree(target, ignore_errors=True)
                raise FileStoreError("Workspace revision changed during materialization")
            return materialized

    def _materialize_workspace_revision_unlocked(
        self,
        state: WorkspaceState,
        refs: list[FileRef],
        destination: Path,
    ) -> WorkspaceMaterialization:
        """Synchronous copy body; caller owns the Session mutation lock."""
        if destination.exists():
            raise FileStoreError("Workspace materialization destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(mode=0o700)
        entries: list[WorkspaceMaterializationEntry] = []
        seen_paths: set[str] = set()
        try:
            for ref in sorted(refs, key=lambda item: item.logical_path):
                if ref.session_id != state.session_id:
                    raise FileAccessDeniedError(
                        "Workspace materialization contains a cross-session file"
                    )
                logical_path = normalize_workspace_logical_path(ref.logical_path)
                if logical_path != ref.logical_path:
                    raise UnsafeFilenameError(
                        "Workspace materialization requires canonical logical paths"
                    )
                folded = logical_path.casefold()
                if folded in seen_paths:
                    raise WorkspacePathConflictError(
                        "Workspace materialization contains duplicate logical paths"
                    )
                seen_paths.add(folded)

                file_dir = self._file_dir(state.session_id, ref.id)
                source = self._resolve_and_check(Path(ref.path), expect_under=file_dir)
                target = destination.joinpath(*PurePosixPath(logical_path).parts)
                target = self._resolve_and_check(target, expect_under=destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                size, digest = _copy_verified_workspace_file(
                    source,
                    target,
                    expected_size=ref.size,
                    expected_sha256=ref.sha256,
                )
                entries.append(
                    WorkspaceMaterializationEntry(
                        logical_path=logical_path,
                        size=size,
                        sha256=digest,
                    )
                )
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise

        frozen_entries = tuple(entries)
        return WorkspaceMaterialization(
            session_id=state.session_id,
            revision=state.revision,
            root_path=destination.resolve(strict=True),
            entries=frozen_entries,
            file_count=len(frozen_entries),
            total_bytes=sum(entry.size for entry in frozen_entries),
            tree_sha256=_workspace_materialization_digest(frozen_entries),
        )

    async def publish_workspace_changes(
        self,
        session_id: str,
        *,
        transaction_id: str,
        expected_workspace_revision: int,
        expected_workspace_sha256: str,
        changes: tuple[WorkspacePublishChange, ...],
        deleted_paths: tuple[str, ...],
    ) -> WorkspacePublishResult:
        """Atomically commit an approved Sandbox change set into one Workspace.

        All payloads are copied and verified before the durable intent enters
        ``committing``. The Session mutation lock covers baseline revalidation,
        pointer swaps, the single revision increment and rollback. Startup
        recovery rolls back any intent without a durable ``committed`` phase.
        """
        if re.fullmatch(r"publish-[0-9a-f]{32}", transaction_id) is None:
            raise FileStoreError("Workspace publish transaction id is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", expected_workspace_sha256) is None:
            raise FileStoreError("Workspace publish baseline SHA is invalid")
        await self.ensure_session_workspace(session_id)
        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            refs = await self.list_session(session_id)
            return self._publish_workspace_changes_unlocked(
                state,
                refs,
                transaction_id=transaction_id,
                expected_workspace_sha256=expected_workspace_sha256,
                changes=changes,
                deleted_paths=deleted_paths,
            )

    def _publish_workspace_changes_unlocked(
        self,
        state: WorkspaceState,
        refs: list[FileRef],
        *,
        transaction_id: str,
        expected_workspace_sha256: str,
        changes: tuple[WorkspacePublishChange, ...],
        deleted_paths: tuple[str, ...],
    ) -> WorkspacePublishResult:
        entries: list[WorkspaceMaterializationEntry] = []
        refs_by_path: dict[str, FileRef] = {}
        for ref in sorted(refs, key=lambda item: item.logical_path):
            logical_path = normalize_workspace_logical_path(ref.logical_path)
            if logical_path != ref.logical_path or ref.session_id != state.session_id:
                raise WorkspaceTreeConflictError("Workspace metadata is not canonical")
            folded = logical_path.casefold()
            if folded in refs_by_path:
                raise WorkspaceTreeConflictError("Workspace paths are not unique")
            file_dir = self._file_dir(state.session_id, ref.id)
            source = self._resolve_and_check(Path(ref.path), expect_under=file_dir)
            _verify_workspace_file(
                source,
                expected_size=ref.size,
                expected_sha256=ref.sha256,
            )
            refs_by_path[folded] = ref
            entries.append(
                WorkspaceMaterializationEntry(
                    logical_path=logical_path,
                    size=ref.size,
                    sha256=ref.sha256,
                )
            )
        current_tree_sha256 = _workspace_materialization_digest(tuple(entries))
        if current_tree_sha256 != expected_workspace_sha256:
            raise WorkspaceTreeConflictError("Workspace tree changed after Sandbox creation")

        ordered_changes = tuple(sorted(changes, key=lambda item: item.logical_path))
        ordered_deleted = tuple(sorted(deleted_paths))
        requested_paths: set[str] = set()
        for logical_path in (
            *(change.logical_path for change in ordered_changes),
            *ordered_deleted,
        ):
            normalized = normalize_workspace_logical_path(logical_path)
            folded = normalized.casefold()
            if normalized != logical_path or folded in requested_paths:
                raise WorkspacePublishPolicyError("Workspace publish paths are not unique")
            if not is_sandbox_publishable_workspace_path(normalized):
                raise WorkspacePublishPolicyError(
                    f"Sandbox cannot publish protected path {logical_path!r}"
                )
            requested_paths.add(folded)

        if not ordered_changes and not ordered_deleted:
            return WorkspacePublishResult(
                transaction_id=transaction_id,
                previous_revision=state.revision,
                revision=state.revision,
                changed_paths=(),
                deleted_paths=(),
            )

        session_dir = self._session_dir(state.session_id)
        transactions_root = session_dir / ".workspace-transactions"
        self._resolve_and_check(transactions_root, expect_under=session_dir)
        _ensure_plain_directory(transactions_root)
        transaction_root = transactions_root / transaction_id
        self._resolve_and_check(transaction_root, expect_under=transactions_root)
        if transaction_root.exists():
            raise FileStoreError("Workspace publish transaction already exists")
        transaction_root.mkdir(mode=0o700)
        payload_root = transaction_root / "payloads"
        payload_root.mkdir(mode=0o700)

        intent_changes: list[_WorkspacePublishIntentChange] = []
        intent_deletions: list[_WorkspacePublishIntentDelete] = []
        replaced_bytes = 0
        new_bytes = 0
        now = _now_ms()
        try:
            for index, change in enumerate(ordered_changes):
                existing = refs_by_path.get(change.logical_path.casefold())
                if existing is not None:
                    if (
                        existing.purpose != "file"
                        or existing.logical_path != change.logical_path
                    ):
                        raise WorkspacePublishPolicyError(
                            "Sandbox cannot replace a protected Workspace file"
                        )
                    file_id = existing.id
                    created_at = existing.created_at
                    replaced_bytes += existing.size
                else:
                    file_id = _gen_file_id()
                    created_at = now
                file_dir = self._file_dir(state.session_id, file_id)
                self._resolve_and_check(file_dir, expect_under=session_dir)
                generation_path = file_dir / f".content-{uuid.uuid4().hex}.blob"
                self._resolve_and_check(generation_path, expect_under=file_dir)
                staged_path = payload_root / f"{index:08d}.blob"
                self._resolve_and_check(staged_path, expect_under=payload_root)
                if change.size > self._max_file_size:
                    raise FileTooLargeError(
                        size=change.size,
                        limit=self._max_file_size,
                    )
                source_path = change.source_path
                if not source_path.is_absolute():
                    raise WorkspacePublishPolicyError(
                        "Workspace publish source path must be absolute"
                    )
                copied, digest = _copy_verified_workspace_file(
                    source_path,
                    staged_path,
                    expected_size=change.size,
                    expected_sha256=change.sha256,
                )
                if PurePosixPath(change.logical_path).parts[0].casefold() != SCRIPTS_PATH:
                    try:
                        with staged_path.open("r", encoding="utf-8") as stream:
                            while stream.read(_CHUNK_SIZE):
                                pass
                    except UnicodeDecodeError as exc:
                        raise WorkspacePublishPolicyError(
                            "Sandbox Markdown output must be valid UTF-8"
                        ) from exc
                filename = PurePosixPath(change.logical_path).name
                after_ref = FileRef(
                    id=file_id,
                    session_id=state.session_id,
                    name=filename,
                    size=copied,
                    mime=_guess_mime(filename, None),
                    sha256=digest,
                    path=str(generation_path),
                    created_at=created_at,
                    logical_path=change.logical_path,
                    origin="agent",
                    purpose="file",
                    updated_at=now,
                )
                intent_changes.append(
                    _WorkspacePublishIntentChange(
                        logical_path=change.logical_path,
                        before_ref=existing,
                        after_ref=after_ref,
                        staged_path=str(staged_path),
                    )
                )
                new_bytes += copied

            deleted_bytes = 0
            for index, logical_path in enumerate(ordered_deleted):
                existing = refs_by_path.get(logical_path.casefold())
                if existing is None:
                    raise WorkspaceTreeConflictError(
                        "Sandbox deletion target is no longer present"
                    )
                if existing.purpose != "file" or existing.logical_path != logical_path:
                    raise WorkspacePublishPolicyError(
                        "Sandbox cannot delete a protected Workspace file"
                    )
                deleted_bytes += existing.size
                tombstone_path = session_dir / (
                    f".publish-deleted-{transaction_id[8:]}-{index:08d}-{existing.id}"
                )
                self._resolve_and_check(tombstone_path, expect_under=session_dir)
                intent_deletions.append(
                    _WorkspacePublishIntentDelete(
                        logical_path=logical_path,
                        before_ref=existing,
                        tombstone_path=str(tombstone_path),
                    )
                )

            projected_size = (
                sum(ref.size for ref in refs)
                - replaced_bytes
                - deleted_bytes
                + new_bytes
            )
            if projected_size > self._max_session_size:
                raise SessionStorageLimitError(
                    current=sum(ref.size for ref in refs) - replaced_bytes - deleted_bytes,
                    new=new_bytes,
                    limit=self._max_session_size,
                )
            after_state = state.model_copy(update={
                "revision": state.revision + 1,
                "updated_at": now,
            })
            intent = _WorkspacePublishIntent(
                transaction_id=transaction_id,
                session_id=state.session_id,
                expected_tree_sha256=expected_workspace_sha256,
                before_state=state,
                after_state=after_state,
                changes=tuple(intent_changes),
                deletions=tuple(intent_deletions),
            )
            _write_model_atomic(transaction_root / "intent.json", intent)
            _write_model_atomic(
                transaction_root / "phase.json",
                _WorkspacePublishPhase(phase="prepared"),
            )
            try:
                self._apply_workspace_publish_intent_unlocked(intent, transaction_root)
            except BaseException:
                try:
                    self._rollback_workspace_publish_intent_unlocked(intent, transaction_root)
                except BaseException as rollback_exc:
                    raise FileStoreError(
                        "Workspace publish failed and could not be rolled back"
                    ) from rollback_exc
                raise
        except BaseException:
            if transaction_root.exists() and not (transaction_root / "intent.json").exists():
                shutil.rmtree(transaction_root, ignore_errors=True)
            raise

        try:
            self._cleanup_workspace_publish_intent_unlocked(intent, transaction_root)
        except Exception:
            # ``committed`` is durable; init recovery can repeat cleanup.
            pass
        return WorkspacePublishResult(
            transaction_id=transaction_id,
            previous_revision=state.revision,
            revision=intent.after_state.revision,
            changed_paths=tuple(change.logical_path for change in intent.changes),
            deleted_paths=tuple(entry.logical_path for entry in intent.deletions),
        )

    def _apply_workspace_publish_intent_unlocked(
        self,
        intent: _WorkspacePublishIntent,
        transaction_root: Path,
    ) -> None:
        _write_model_atomic(
            transaction_root / "phase.json",
            _WorkspacePublishPhase(phase="committing"),
        )
        for change in intent.changes:
            after = change.after_ref
            file_dir = self._file_dir(intent.session_id, after.id)
            staged_path = self._resolve_and_check(
                Path(change.staged_path),
                expect_under=transaction_root / "payloads",
            )
            generation_path = self._resolve_and_check(
                Path(after.path),
                expect_under=file_dir,
            )
            if change.before_ref is None:
                file_dir.mkdir(mode=0o700, exist_ok=False)
            else:
                current = self._read_metadata(file_dir)
                if current != change.before_ref:
                    raise WorkspaceTreeConflictError(
                        "Workspace metadata changed during publish"
                    )
            os.replace(staged_path, generation_path)
            self._write_metadata(file_dir, after)

        for deletion in intent.deletions:
            file_dir = self._file_dir(intent.session_id, deletion.before_ref.id)
            current = self._read_metadata(file_dir)
            if current != deletion.before_ref:
                raise WorkspaceTreeConflictError("Workspace deletion target changed")
            tombstone = self._resolve_and_check(
                Path(deletion.tombstone_path),
                expect_under=self._session_dir(intent.session_id),
            )
            if tombstone.exists():
                raise WorkspaceTreeConflictError("Workspace tombstone already exists")
            os.replace(file_dir, tombstone)

        current_state = self._read_workspace_state_unlocked(intent.session_id)
        if current_state != intent.before_state:
            raise WorkspaceTreeConflictError("Workspace revision changed during publish")
        self._write_workspace_state_unlocked(intent.after_state)
        _write_model_atomic(
            transaction_root / "phase.json",
            _WorkspacePublishPhase(phase="committed"),
        )

    def _rollback_workspace_publish_intent_unlocked(
        self,
        intent: _WorkspacePublishIntent,
        transaction_root: Path,
    ) -> None:
        for deletion in reversed(intent.deletions):
            file_dir = self._file_dir(intent.session_id, deletion.before_ref.id)
            tombstone = self._resolve_and_check(
                Path(deletion.tombstone_path),
                expect_under=self._session_dir(intent.session_id),
            )
            if tombstone.exists():
                if file_dir.exists():
                    raise FileStoreError("Workspace rollback found conflicting delete state")
                os.replace(tombstone, file_dir)
        for change in reversed(intent.changes):
            file_dir = self._file_dir(intent.session_id, change.after_ref.id)
            generation_path = self._resolve_and_check(
                Path(change.after_ref.path),
                expect_under=file_dir,
            )
            if change.before_ref is None:
                if file_dir.exists():
                    self._delete_file_dir_unlocked(intent.session_id, change.after_ref.id)
            else:
                if not file_dir.is_dir():
                    raise FileStoreError("Workspace rollback target is missing")
                self._write_metadata(file_dir, change.before_ref)
                if generation_path != Path(change.before_ref.path):
                    generation_path.unlink(missing_ok=True)
        self._write_workspace_state_unlocked(intent.before_state)
        shutil.rmtree(transaction_root, ignore_errors=False)

    def _cleanup_workspace_publish_intent_unlocked(
        self,
        intent: _WorkspacePublishIntent,
        transaction_root: Path,
    ) -> None:
        for change in intent.changes:
            if change.before_ref is None:
                continue
            prior_path = self._resolve_and_check(
                Path(change.before_ref.path),
                expect_under=self._file_dir(intent.session_id, change.before_ref.id),
            )
            if prior_path != Path(change.after_ref.path):
                prior_path.unlink(missing_ok=True)
        for deletion in intent.deletions:
            tombstone = self._resolve_and_check(
                Path(deletion.tombstone_path),
                expect_under=self._session_dir(intent.session_id),
            )
            if tombstone.exists():
                shutil.rmtree(tombstone)
        shutil.rmtree(transaction_root)

    # ------------------------------------------------------------------
    # 内部辅助：路径构造 + 边界检查
    # ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        """session_id 不能含路径分隔符——只作为目录名。"""
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            raise UnsafeFilenameError(
                f"unsafe session_id {session_id!r}"
            )
        return self._root_dir / session_id

    def _file_dir(self, session_id: str, file_id: str) -> Path:
        """file_id 同样不能含路径分隔符。"""
        if not file_id or "/" in file_id or "\\" in file_id or ".." in file_id:
            raise UnsafeFilenameError(
                f"unsafe file_id {file_id!r}"
            )
        return self._session_dir(session_id) / file_id

    def _resolve_and_check(
        self, target: Path, *, expect_under: Path,
    ) -> Path:
        """resolve target 并校验仍在 expect_under 下；失败抛 UnsafeFilenameError。"""
        try:
            resolved = target.resolve(strict=False)
            root_resolved = expect_under.resolve(strict=False)
            if not resolved.is_relative_to(root_resolved):
                raise UnsafeFilenameError(
                    f"path {target!r} escapes uploads root {expect_under!r}"
                )
            return resolved
        except OSError as e:
            raise UnsafeFilenameError(
                f"path resolve failed for {target!r}: {e}"
            ) from e

    def _read_metadata(self, file_dir: Path) -> FileRef | None:
        """读 metadata.json；不存在 / 损坏返回 None。"""
        meta_path = file_dir / "metadata.json"
        if not meta_path.is_file():
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            ref = FileRef.model_validate(payload)
            # 历史版本 metadata 里可能存普通路径；统一归一为与
            # _root_dir 相同的 Windows 扩展长度形式，保证
            # _resolve_and_check 两侧可比、update_text 等消费方可用
            normalized = _extended_length_path(Path(ref.path))
            if os.fspath(normalized) != ref.path:
                ref = ref.model_copy(update={"path": os.fspath(normalized)})
            return ref
        except Exception:
            # 损坏 metadata 静默忽略；上层视为 file not found
            return None

    def _write_metadata(self, file_dir: Path, ref: FileRef) -> None:
        """以同目录 temp + replace 原子发布 metadata pointer。"""
        meta_path = file_dir / "metadata.json"
        temp_path = file_dir / f".metadata.{uuid.uuid4().hex}.tmp"
        raw = json.dumps(
            ref.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode("utf-8")
        try:
            with temp_path.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(meta_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _validate_workspace_publish_intent_unlocked(
        self,
        intent: _WorkspacePublishIntent,
        transaction_root: Path,
    ) -> None:
        if (
            intent.transaction_id != transaction_root.name
            or intent.before_state.session_id != intent.session_id
            or intent.after_state.session_id != intent.session_id
            or intent.after_state.revision != intent.before_state.revision + 1
        ):
            raise FileStoreError("Workspace publish intent identity is invalid")
        session_dir = self._session_dir(intent.session_id)
        transactions_root = session_dir / ".workspace-transactions"
        if transaction_root.parent.resolve(strict=False) != transactions_root.resolve(
            strict=False
        ):
            raise FileStoreError("Workspace publish intent is outside its Session")
        seen: set[str] = set()
        for change in intent.changes:
            logical_path = normalize_workspace_logical_path(change.logical_path)
            if (
                logical_path != change.logical_path
                or not is_sandbox_publishable_workspace_path(logical_path)
                or logical_path.casefold() in seen
                or change.after_ref.logical_path != logical_path
                or change.after_ref.session_id != intent.session_id
                or change.after_ref.purpose != "file"
            ):
                raise FileStoreError("Workspace publish change intent is invalid")
            seen.add(logical_path.casefold())
            if change.before_ref is not None and (
                change.before_ref.session_id != intent.session_id
                or change.before_ref.logical_path != logical_path
                or change.before_ref.id != change.after_ref.id
                or change.before_ref.purpose != "file"
            ):
                raise FileStoreError("Workspace publish replacement intent is invalid")
            file_dir = self._file_dir(intent.session_id, change.after_ref.id)
            self._resolve_and_check(Path(change.after_ref.path), expect_under=file_dir)
            self._resolve_and_check(
                Path(change.staged_path),
                expect_under=transaction_root / "payloads",
            )
        for deletion in intent.deletions:
            logical_path = normalize_workspace_logical_path(deletion.logical_path)
            if (
                logical_path != deletion.logical_path
                or not is_sandbox_publishable_workspace_path(logical_path)
                or logical_path.casefold() in seen
                or deletion.before_ref.session_id != intent.session_id
                or deletion.before_ref.logical_path != logical_path
                or deletion.before_ref.purpose != "file"
            ):
                raise FileStoreError("Workspace publish deletion intent is invalid")
            seen.add(logical_path.casefold())
            self._resolve_and_check(
                Path(deletion.before_ref.path),
                expect_under=self._file_dir(intent.session_id, deletion.before_ref.id),
            )
            tombstone = self._resolve_and_check(
                Path(deletion.tombstone_path),
                expect_under=session_dir,
            )
            if not tombstone.name.startswith(
                f".publish-deleted-{intent.transaction_id[8:]}-"
            ):
                raise FileStoreError("Workspace publish tombstone is invalid")

    def _recover_workspace_publish_transactions_unlocked(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        transactions_root = session_dir / ".workspace-transactions"
        if not transactions_root.exists():
            return
        _ensure_plain_directory(transactions_root)
        for transaction_root in sorted(transactions_root.iterdir()):
            try:
                _ensure_plain_directory(transaction_root)
            except (OSError, UnsafeFilenameError) as exc:
                raise FileStoreError(
                    "Workspace publish transaction directory is invalid"
                ) from exc
            intent_path = transaction_root / "intent.json"
            if not intent_path.is_file():
                shutil.rmtree(transaction_root)
                continue
            try:
                intent = _WorkspacePublishIntent.model_validate_json(
                    intent_path.read_text(encoding="utf-8")
                )
                phase_path = transaction_root / "phase.json"
                phase = (
                    _WorkspacePublishPhase.model_validate_json(
                        phase_path.read_text(encoding="utf-8")
                    )
                    if phase_path.is_file()
                    else _WorkspacePublishPhase(phase="prepared")
                )
                self._validate_workspace_publish_intent_unlocked(
                    intent,
                    transaction_root,
                )
            except FileStoreError:
                raise
            except Exception as exc:
                raise FileStoreError("Workspace publish recovery state is invalid") from exc
            if phase.phase == "committed":
                self._cleanup_workspace_publish_intent_unlocked(intent, transaction_root)
                continue
            current_state = self._read_workspace_state_unlocked(session_id)
            if current_state not in (intent.before_state, intent.after_state):
                raise FileStoreError("Workspace publish recovery found a revision conflict")
            self._rollback_workspace_publish_intent_unlocked(intent, transaction_root)
        try:
            transactions_root.rmdir()
        except OSError:
            pass

    def _recover_managed_files(self) -> None:
        """Repair old interrupted writes and remove unreferenced temp generations.

        ``metadata.json`` is the commit pointer. New writes never mutate its
        referenced content file. The legacy in-place update protocol may leave
        a ``*.bak`` or content/metadata hash mismatch; both states are repaired
        conservatively before the store becomes visible.
        """
        if not self._root_dir.is_dir():
            return
        for session_dir in self._root_dir.iterdir():
            if not session_dir.is_dir():
                continue
            self._recover_workspace_publish_transactions_unlocked(session_dir.name)
            for child in list(session_dir.iterdir()):
                if (
                    child.is_file()
                    and child.name.startswith(".workspace.")
                    and child.suffix == ".tmp"
                ):
                    try:
                        child.unlink(missing_ok=True)
                    except OSError:
                        pass
                elif child.is_dir() and child.name.startswith(".deleted-"):
                    try:
                        for tombstone_child in child.iterdir():
                            tombstone_child.unlink(missing_ok=True)
                        child.rmdir()
                    except OSError:
                        pass
            for file_dir in session_dir.iterdir():
                if not file_dir.is_dir() or file_dir.name.startswith("."):
                    continue
                ref = self._read_metadata(file_dir)
                if ref is None:
                    continue
                target = Path(ref.path)
                try:
                    self._resolve_and_check(target, expect_under=file_dir)
                except UnsafeFilenameError:
                    continue
                if not target.is_file():
                    backups = sorted(
                        (
                            child for child in file_dir.iterdir()
                            if child.is_file() and child.name.endswith(".bak")
                        ),
                        key=lambda child: child.stat().st_mtime_ns,
                        reverse=True,
                    )
                    if backups:
                        try:
                            backups[0].replace(target)
                        except OSError:
                            continue
                if not target.is_file():
                    continue
                actual_size = target.stat().st_size
                actual_sha = _sha256_of_file(target)
                if ref.size != actual_size or ref.sha256 != actual_sha:
                    ref = ref.model_copy(update={
                        "size": actual_size,
                        "sha256": actual_sha,
                        "updated_at": max(
                            ref.updated_at or ref.created_at,
                            int(target.stat().st_mtime * 1000),
                        ),
                    })
                    self._write_metadata(file_dir, ref)
                for child in file_dir.iterdir():
                    if child == target or child.name == "metadata.json":
                        continue
                    if child.is_file():
                        try:
                            child.unlink(missing_ok=True)
                        except OSError:
                            pass

    async def _unique_logical_path(
        self,
        session_id: str,
        requested_path: str,
    ) -> str:
        """为逻辑树分配不冲突的路径（Windows 语义下大小写不敏感）。"""
        existing = {
            ref.logical_path.casefold()
            for ref in await self.list_session(session_id)
        }
        if requested_path.casefold() not in existing:
            return requested_path

        path = PurePosixPath(requested_path)
        parent = "" if str(path.parent) == "." else str(path.parent)
        stem = path.stem
        suffix = path.suffix
        index = 2
        while True:
            filename = f"{stem} ({index}){suffix}"
            candidate = f"{parent}/{filename}" if parent else filename
            if candidate.casefold() not in existing:
                return candidate
            index += 1

    # ------------------------------------------------------------------
    # save：上传文件
    # ------------------------------------------------------------------

    async def save(
        self,
        session_id: str,
        upload: UploadFile,
        *,
        relative_folder: str | None = None,
        expected_workspace_revision: int | None = None,
    ) -> FileRef:
        """Persist one upload and atomically advance its Workspace revision."""
        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            ref = await self._save_unlocked(
                session_id,
                upload,
                relative_folder=relative_folder,
            )
            try:
                self._advance_workspace_revision_unlocked(state)
            except Exception:
                try:
                    self._delete_file_dir_unlocked(session_id, ref.id)
                except Exception:
                    pass
                raise
            return ref

    async def _save_unlocked(
        self,
        session_id: str,
        upload: UploadFile,
        *,
        relative_folder: str | None,
    ) -> FileRef:
        """保存上传文件到 uploads/{session_id}/{file_id}/{safe_filename}。

        - 流式读 UploadFile（chunk 64KB），不一次性读内存
        - 单文件超 max_file_size 立即停止 + 删除已写部分 + 抛 FileTooLargeError
        - session 总量超限：保存前预检 + 抛 SessionStorageLimitError（无需回滚）
        - 文件名 sanitize + resolve 边界检查
        """
        if not self._initialized:
            await self.init()

        # session_id 校验（防御）
        session_dir = self._session_dir(session_id)
        # 边界检查 session 目录
        self._resolve_and_check(session_dir, expect_under=self._root_dir)

        # 文件名 sanitize
        original_name = upload.filename or "upload.bin"
        safe_name = sanitize_filename(original_name)
        logical_path = await self._unique_logical_path(
            session_id,
            workspace_logical_path(safe_name, relative_folder),
        )

        # 预检 session 总量
        current_size = await self.session_total_size(session_id)
        # 单文件先按 max_file_size 预估上限判断 session 是否会超
        if current_size + min(self._max_file_size, self._max_session_size) > self._max_session_size:
            # 若当前已接近上限，连最小文件都装不下——提前拒
            if current_size >= self._max_session_size:
                raise SessionStorageLimitError(
                    current=current_size, new=0, limit=self._max_session_size,
                )

        # 准备 file_dir + 文件路径
        file_id = _gen_file_id()
        file_dir = self._file_dir(session_id, file_id)
        # 边界检查 file_dir（file_id 是 uuid，但二次防御）
        self._resolve_and_check(file_dir, expect_under=self._root_dir)
        file_dir.mkdir(parents=True, exist_ok=True)

        target_path = file_dir / safe_name
        # 边界检查 target_path（safe_name 经过 sanitize，但 resolve 兜底）
        self._resolve_and_check(target_path, expect_under=file_dir)

        # 流式写入 + sha256 计算
        sha = hashlib.sha256()
        total = 0
        try:
            with target_path.open("wb") as f:
                while True:
                    chunk = await upload.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    # 单文件超限：停止写 + 删除已写部分
                    if total > self._max_file_size:
                        f.close()
                        try:
                            target_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        # 清理空 file_dir
                        try:
                            file_dir.rmdir()
                        except Exception:
                            pass
                        raise FileTooLargeError(
                            size=total, limit=self._max_file_size,
                        )
                    # session 总量超限（边写边检，避免超大 chunk 撕开限制）
                    if current_size + total > self._max_session_size:
                        f.close()
                        try:
                            target_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        try:
                            file_dir.rmdir()
                        except Exception:
                            pass
                        raise SessionStorageLimitError(
                            current=current_size, new=total,
                            limit=self._max_session_size,
                        )
                    sha.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        except FileStoreError:
            raise
        except Exception as e:
            # 任意 IO 异常 → 清理 + 重新包装
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                file_dir.rmdir()
            except Exception:
                pass
            raise FileStoreError(
                f"save failed for {original_name!r}: {type(e).__name__}: {e}"
            ) from e

        # 构造 FileRef + 写 metadata
        mime = _guess_mime(safe_name, upload.content_type)
        now = _now_ms()
        ref = FileRef(
            id=file_id,
            session_id=session_id,
            name=safe_name,
            size=total,
            mime=mime,
            sha256=sha.hexdigest(),
            path=str(target_path),
            created_at=now,
            logical_path=logical_path,
            origin="upload",
            purpose="file",
            updated_at=now,
        )
        try:
            self._write_metadata(file_dir, ref)
        except Exception as e:
            # metadata 写失败 → 删除已上传文件，避免孤儿
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                (file_dir / "metadata.json").unlink(missing_ok=True)
            except Exception:
                pass
            try:
                file_dir.rmdir()
            except Exception:
                pass
            raise FileStoreError(
                f"write metadata failed: {type(e).__name__}: {e}"
            ) from e

        return ref

    async def write_text(
        self,
        session_id: str,
        filename: str,
        content: str,
        *,
        content_type: str | None = None,
        folder: str | None = None,
        origin: Literal["system", "agent", "user"] = "agent",
        purpose: Literal["file", "agent_instructions", "memory"] = "file",
        expected_workspace_revision: int | None = None,
        unique_logical_path: bool = True,
    ) -> FileRef:
        """在 session 工作目录创建一个新的 UTF-8 文本文件。

        该入口供 Agent 的 ``write_file`` 工具使用。它不会接受或暴露物理路径，
        每次调用都生成新的 file_id，因此不会覆盖用户上传或之前生成的文件。
        文件名清理、路径边界、单文件大小和 session 总容量与上传路径一致。
        """
        safe_name = sanitize_filename(filename)
        requested_path = workspace_logical_path(
            safe_name,
            folder,
            purpose=purpose,
        )
        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            if unique_logical_path:
                logical_path = await self._unique_logical_path(
                    session_id,
                    requested_path,
                )
            else:
                existing = await self.get_by_logical_path(
                    session_id,
                    requested_path,
                )
                if existing is not None:
                    raise WorkspacePathConflictError(
                        f"workspace path {requested_path!r} already exists"
                    )
                logical_path = requested_path
            ref = await self._write_text_unlocked(
                session_id,
                safe_name,
                content,
                logical_path=logical_path,
                content_type=content_type,
                origin=origin,
                purpose=purpose,
            )
            try:
                self._advance_workspace_revision_unlocked(state)
            except Exception:
                try:
                    self._delete_file_dir_unlocked(session_id, ref.id)
                except Exception:
                    pass
                raise
            return ref

    async def _write_text_unlocked(
        self,
        session_id: str,
        safe_name: str,
        content: str,
        *,
        logical_path: str,
        content_type: str | None,
        origin: Literal["system", "agent", "user"],
        purpose: Literal["file", "agent_instructions", "memory"],
    ) -> FileRef:
        if not isinstance(content, str):
            raise FileStoreError("content must be a string")

        raw = content.encode("utf-8")
        size = len(raw)
        if size > self._max_file_size:
            raise FileTooLargeError(size=size, limit=self._max_file_size)

        session_dir = await self.ensure_session_folder(session_id)
        current_size = await self.session_total_size(session_id)
        if current_size + size > self._max_session_size:
            raise SessionStorageLimitError(
                current=current_size,
                new=size,
                limit=self._max_session_size,
            )

        file_id = _gen_file_id()
        file_dir = self._file_dir(session_id, file_id)
        self._resolve_and_check(file_dir, expect_under=session_dir)
        file_dir.mkdir(parents=True, exist_ok=False)

        target_path = file_dir / safe_name
        self._resolve_and_check(target_path, expect_under=file_dir)
        temp_path = file_dir / f".{safe_name}.{uuid.uuid4().hex}.tmp"
        self._resolve_and_check(temp_path, expect_under=file_dir)

        now = _now_ms()
        ref = FileRef(
            id=file_id,
            session_id=session_id,
            name=safe_name,
            size=size,
            mime=_guess_mime(safe_name, content_type),
            sha256=hashlib.sha256(raw).hexdigest(),
            path=str(target_path),
            created_at=now,
            logical_path=logical_path,
            origin=origin,
            purpose=purpose,
            updated_at=now,
        )
        try:
            temp_path.write_bytes(raw)
            temp_path.replace(target_path)
            self._write_metadata(file_dir, ref)
        except Exception as e:
            for child in (temp_path, target_path, file_dir / "metadata.json"):
                try:
                    child.unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                file_dir.rmdir()
            except Exception:
                pass
            raise FileStoreError(
                f"write_text failed for {safe_name!r}: {type(e).__name__}: {e}"
            ) from e

        return ref

    async def ensure_session_workspace(
        self,
        session_id: str,
    ) -> tuple[Path, FileRef]:
        """幂等初始化 Session 目录及唯一固定根文件。

        ``AGENT.md`` 和 ``Memory.md`` 都在 Session 创建时出现。旧 Session
        缺少任一文件时在启动恢复中补齐；大小写等价的旧逻辑路径会被规范为
        固定大小写，同时保留正文、file id 和时间戳。

        返回值暂时保留历史 ``(session_dir, agent_ref)`` 契约；新调用方应把
        store 本身视为 Workspace API，并按逻辑路径读取根文件。
        """
        session_dir = await self.ensure_session_folder(session_id)
        async with self._session_lock(session_id):
            created: list[FileRef] = []
            agent_ref = await self.get_by_logical_path(
                session_id,
                AGENT_INSTRUCTIONS_PATH,
            )
            if agent_ref is not None:
                if (
                    agent_ref.purpose != "agent_instructions"
                    or agent_ref.logical_path != AGENT_INSTRUCTIONS_PATH
                ):
                    agent_ref = agent_ref.model_copy(update={
                        "logical_path": AGENT_INSTRUCTIONS_PATH,
                        "purpose": "agent_instructions",
                    })
                    self._write_metadata(
                        self._file_dir(session_id, agent_ref.id),
                        agent_ref,
                    )
            else:
                agent_ref = await self._write_text_unlocked(
                    session_id,
                    AGENT_INSTRUCTIONS_PATH,
                    DEFAULT_AGENT_INSTRUCTIONS,
                    logical_path=AGENT_INSTRUCTIONS_PATH,
                    content_type="text/markdown",
                    origin="system",
                    purpose="agent_instructions",
                )
                created.append(agent_ref)

            try:
                memory_ref = await self.get_by_logical_path(
                    session_id,
                    MEMORY_PATH,
                )
                if memory_ref is not None:
                    if (
                        memory_ref.purpose != "memory"
                        or memory_ref.logical_path != MEMORY_PATH
                    ):
                        memory_ref = memory_ref.model_copy(update={
                            "logical_path": MEMORY_PATH,
                            "purpose": "memory",
                        })
                        self._write_metadata(
                            self._file_dir(session_id, memory_ref.id),
                            memory_ref,
                        )
                else:
                    memory_ref = await self._write_text_unlocked(
                        session_id,
                        MEMORY_PATH,
                        DEFAULT_MEMORY,
                        logical_path=MEMORY_PATH,
                        content_type="text/markdown",
                        origin="system",
                        purpose="memory",
                    )
                    created.append(memory_ref)
            except BaseException:
                # New Session creation is all-or-nothing from the caller's
                # perspective. Existing roots are never deleted on recovery.
                for ref in reversed(created):
                    try:
                        self._delete_file_dir_unlocked(session_id, ref.id)
                    except Exception:
                        pass
                raise

            await self._ensure_workspace_state_unlocked(session_id)
            return session_dir, agent_ref

    async def update_text(
        self,
        session_id: str,
        file_id: str,
        content: str,
        *,
        expected_sha256: str | None = None,
        expected_workspace_revision: int | None = None,
        origin: Literal["system", "upload", "agent", "user", "legacy"] | None = "user",
        purpose: Literal["file", "agent_instructions", "memory"] | None = None,
    ) -> FileRef:
        """用 sha256 乐观锁和 immutable generation 原子更新文本文件。

        新正文先写入独立 generation；一次 ``metadata.json`` replace 是唯一
        commit point。进程在 commit 前退出时旧 metadata 仍指向旧正文，commit
        后退出时则只会留下可清理的旧 generation，不会出现正文/metadata 半套。
        """
        if not isinstance(content, str):
            raise FileStoreError("content must be a string")
        raw = content.encode("utf-8")
        if len(raw) > self._max_file_size:
            raise FileTooLargeError(size=len(raw), limit=self._max_file_size)

        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            ref = await self.get_for_session(session_id, file_id)
            if expected_sha256 is not None and expected_sha256 != ref.sha256:
                raise FileVersionConflictError("file changed since it was opened")
            current_size = await self.session_total_size(session_id)
            if current_size - ref.size + len(raw) > self._max_session_size:
                raise SessionStorageLimitError(
                    current=current_size - ref.size,
                    new=len(raw),
                    limit=self._max_session_size,
                )

            prior_path = Path(ref.path)
            file_dir = self._file_dir(session_id, file_id)
            self._resolve_and_check(prior_path, expect_under=file_dir)
            generation_path = file_dir / f".content-{uuid.uuid4().hex}.blob"
            self._resolve_and_check(generation_path, expect_under=file_dir)

            updated = ref.model_copy(update={
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "path": str(generation_path),
                "origin": origin or ref.origin,
                "purpose": purpose or ref.purpose,
                "updated_at": _now_ms(),
            })
            try:
                with generation_path.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._write_metadata(file_dir, updated)
            except Exception as e:
                try:
                    generation_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise FileStoreError(
                    f"update_text failed for {ref.name!r}: {type(e).__name__}: {e}"
                ) from e
            try:
                self._advance_workspace_revision_unlocked(state)
            except Exception as exc:
                try:
                    self._write_metadata(file_dir, ref)
                    generation_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    raise FileStoreError(
                        "workspace revision update and file rollback failed"
                    ) from rollback_exc
                raise FileStoreError(
                    "workspace revision update failed; file update was rolled back"
                ) from exc
            try:
                if prior_path != generation_path:
                    prior_path.unlink(missing_ok=True)  # noqa: ASYNC240
            except OSError:
                # metadata 已提交；旧 generation 只是可在下次 init 清理的孤儿。
                pass
            return updated

    async def move_file(
        self,
        session_id: str,
        file_id: str,
        logical_path: str,
        *,
        expected_sha256: str | None = None,
        expected_workspace_revision: int | None = None,
    ) -> FileRef:
        """Move/rename an editable Markdown entry without moving content bytes."""
        target_path = normalize_workspace_logical_path(logical_path)
        target_name = PurePosixPath(target_path).name
        if not is_markdown_filename(target_name):
            raise UnsafeFilenameError("Markdown files must keep a Markdown extension")
        if target_path.casefold() in {
            AGENT_INSTRUCTIONS_PATH.casefold(),
            MEMORY_PATH.casefold(),
        }:
            raise UnsafeFilenameError("fixed Workspace root files cannot be moved")

        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            ref = await self.get_for_session(session_id, file_id)
            if ref.purpose != "file" or not is_markdown_filename(ref.name):
                raise UnsafeFilenameError("only ordinary Markdown files can be moved")
            if expected_sha256 is not None and expected_sha256 != ref.sha256:
                raise FileVersionConflictError("file changed since it was opened")
            occupied = await self.get_by_logical_path(session_id, target_path)
            if occupied is not None and occupied.id != file_id:
                raise WorkspacePathConflictError(
                    f"workspace path {target_path!r} already exists"
                )
            if ref.logical_path == target_path:
                return ref

            moved = ref.model_copy(update={
                "name": target_name,
                "logical_path": target_path,
                "mime": _guess_mime(target_name, "text/markdown"),
                "origin": "user",
                "updated_at": _now_ms(),
            })
            file_dir = self._file_dir(session_id, file_id)
            self._write_metadata(file_dir, moved)
            try:
                self._advance_workspace_revision_unlocked(state)
            except Exception as exc:
                try:
                    self._write_metadata(file_dir, ref)
                except Exception as rollback_exc:
                    raise FileStoreError(
                        "workspace revision update and move rollback failed"
                    ) from rollback_exc
                raise FileStoreError(
                    "workspace revision update failed; move was rolled back"
                ) from exc
            return moved

    # ------------------------------------------------------------------
    # get / list
    # ------------------------------------------------------------------

    async def get(self, file_id: str) -> FileRef | None:
        """全库查 file_id（不带 session 校验）；不存在返回 None。

        调用方在 Web 层应优先用 get_for_session 做 session 隔离。
        """
        if not file_id or ".." in file_id or "/" in file_id:
            return None
        # 扫 root_dir / *session_id* / file_id
        if not self._root_dir.is_dir():
            return None
        for session_dir in self._root_dir.iterdir():
            if not session_dir.is_dir():
                continue
            file_dir = session_dir / file_id
            if not file_dir.is_dir():
                continue
            ref = self._read_metadata(file_dir)
            if ref is not None:
                return ref
        return None

    async def get_for_session(
        self, session_id: str, file_id: str,
    ) -> FileRef:
        """读取文件并强校验 session_id 匹配。

        - 文件不存在 → VirtualFileNotFoundError
        - 文件存在但 session_id 不匹配 → FileAccessDeniedError
        """
        file_dir = self._file_dir(session_id, file_id)
        # 边界检查
        try:
            self._resolve_and_check(file_dir, expect_under=self._root_dir)
        except UnsafeFilenameError as e:
            raise FileAccessDeniedError(str(e)) from e

        if not file_dir.is_dir():
            # 也检查一下是不是在别的 session 下（明确拒绝而不是 404）
            other = await self.get(file_id)
            if other is not None and other.session_id != session_id:
                raise FileAccessDeniedError(
                    f"file {file_id!r} does not belong to session {session_id!r}"
                )
            raise VirtualFileNotFoundError(
                f"file {file_id!r} not found in session {session_id!r}"
            )

        ref = self._read_metadata(file_dir)
        if ref is None:
            raise VirtualFileNotFoundError(
                f"file {file_id!r} metadata missing in session {session_id!r}"
            )
        if ref.session_id != session_id:
            # metadata 与 query 不一致——拒绝
            raise FileAccessDeniedError(
                f"file {file_id!r} does not belong to session {session_id!r}"
            )
        return ref

    async def list_session(self, session_id: str) -> list[FileRef]:
        """列出 session 所有文件 metadata。session 不存在不报错——返回空 list。"""
        session_dir = self._session_dir(session_id)
        try:
            self._resolve_and_check(session_dir, expect_under=self._root_dir)
        except UnsafeFilenameError:
            return []
        if not session_dir.is_dir():
            return []
        out: list[FileRef] = []
        for file_dir in session_dir.iterdir():
            if not file_dir.is_dir() or file_dir.name.startswith("."):
                continue
            ref = self._read_metadata(file_dir)
            if ref is not None:
                out.append(ref)
        # 按 created_at 升序，便于 UI 稳定排序
        out.sort(key=lambda r: r.created_at)
        return out

    async def get_by_logical_path(
        self,
        session_id: str,
        logical_path: str,
    ) -> FileRef | None:
        """按大小写不敏感的 Session 逻辑路径查找文件。"""
        target = logical_path.replace("\\", "/").casefold()
        for ref in await self.list_session(session_id):
            if ref.logical_path.casefold() == target:
                return ref
        return None

    async def session_total_size(self, session_id: str) -> int:
        """当前 session 所有文件总字节数。"""
        files = await self.list_session(session_id)
        return sum(f.size for f in files)

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def _delete_file_dir_unlocked(self, session_id: str, file_id: str) -> None:
        file_dir = self._file_dir(session_id, file_id)
        try:
            for child in file_dir.iterdir():
                child.unlink(missing_ok=True)
            file_dir.rmdir()
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise FileStoreError(
                f"delete failed for {file_id!r}: {type(exc).__name__}: {exc}"
            ) from exc

    async def delete(self, file_id: str) -> None:
        """删除单文件（不带 session 校验，扫库定位）。

        推荐用 delete_for_session(sid, fid) 做隔离；这里保留以兼容 P0-2 早期 API。
        """
        ref = await self.get(file_id)
        if ref is None:
            raise VirtualFileNotFoundError(f"file {file_id!r} not found")
        await self.delete_for_session(ref.session_id, file_id)

    async def delete_for_session(
        self,
        session_id: str,
        file_id: str,
        *,
        expected_sha256: str | None = None,
        expected_workspace_revision: int | None = None,
    ) -> FileRef:
        """session 隔离删除：返回被删的 FileRef。

        - 文件不存在 → VirtualFileNotFoundError
        - 跨 session → FileAccessDeniedError
        """
        async with self._session_lock(session_id):
            state = await self._ensure_workspace_state_unlocked(session_id)
            self._check_workspace_revision(state, expected_workspace_revision)
            ref = await self.get_for_session(session_id, file_id)
            if expected_sha256 is not None and expected_sha256 != ref.sha256:
                raise FileVersionConflictError("file changed since it was opened")

            file_dir = self._file_dir(session_id, file_id)
            tombstone = self._session_dir(session_id) / (
                f".deleted-{file_id}-{uuid.uuid4().hex}"
            )
            self._resolve_and_check(
                tombstone,
                expect_under=self._session_dir(session_id),
            )
            try:
                file_dir.replace(tombstone)
            except FileNotFoundError as exc:
                raise VirtualFileNotFoundError(
                    f"file {file_id!r} not found in session {session_id!r}"
                ) from exc
            except Exception as exc:
                raise FileStoreError(
                    f"delete failed for {file_id!r}: {type(exc).__name__}: {exc}"
                ) from exc

            try:
                self._advance_workspace_revision_unlocked(state)
            except Exception as exc:
                try:
                    tombstone.replace(file_dir)
                except Exception as rollback_exc:
                    raise FileStoreError(
                        "workspace revision update and delete rollback failed"
                    ) from rollback_exc
                raise FileStoreError(
                    "workspace revision update failed; delete was rolled back"
                ) from exc

            try:
                for child in tombstone.iterdir():
                    child.unlink(missing_ok=True)
                tombstone.rmdir()
            except OSError:
                # Deletion is already committed. Hidden tombstones are ignored
                # by readers and cleaned during the next store initialization.
                pass
            return ref

    async def delete_session_files(self, session_id: str) -> int:
        """删除 session 下所有文件；返回删除的文件数。

        幂等：session_dir 不存在不报错。
        """
        session_dir = self._session_dir(session_id)
        try:
            self._resolve_and_check(session_dir, expect_under=self._root_dir)
        except UnsafeFilenameError:
            return 0
        if not session_dir.is_dir():
            return 0
        count = 0
        # 删 session_dir 整个目录树
        try:
            for file_dir in session_dir.iterdir():
                if not file_dir.is_dir():
                    continue
                count_as_file = not file_dir.name.startswith(".")
                try:
                    for child in file_dir.iterdir():
                        child.unlink(missing_ok=True)
                    file_dir.rmdir()
                    if count_as_file:
                        count += 1
                except Exception:
                    continue
            for child in session_dir.iterdir():
                if child.is_file() and (
                    child.name == WORKSPACE_STATE_FILENAME
                    or child.name.startswith(".workspace.")
                ):
                    child.unlink(missing_ok=True)
            session_dir.rmdir()
        except Exception:
            pass
        return count


# Backward-compatible import for downstream users. Both names reference the
# exact same implementation and storage; this is not a second file store.
VirtualFileStore = WorkspaceStore


__all__ = [
    # 常量
    "DEFAULT_MAX_FILE_SIZE",
    "DEFAULT_MAX_SESSION_SIZE",
    "AGENT_INSTRUCTIONS_PATH",
    "MEMORY_PATH",
    "SCRIPTS_PATH",
    "WORKSPACE_STATE_FILENAME",
    "WORKSPACE_MATERIALIZATION_SCHEMA",
    "WORKSPACE_PUBLISH_TRANSACTION_SCHEMA",
    "WORKSPACE_PUBLISH_PHASE_SCHEMA",
    "CODE_EXTENSIONS",
    "MARKDOWN_EXTENSIONS",
    "DEFAULT_AGENT_INSTRUCTIONS",
    "DEFAULT_MEMORY",
    # 异常
    "FileStoreError",
    "VirtualFileNotFoundError",
    "FileAccessDeniedError",
    "FileTooLargeError",
    "SessionStorageLimitError",
    "UnsafeFilenameError",
    "FileVersionConflictError",
    "WorkspaceVersionConflictError",
    "WorkspacePathConflictError",
    "WorkspaceTreeConflictError",
    "WorkspacePublishPolicyError",
    # 数据模型
    "FileRef",
    "WorkspaceState",
    "WorkspaceMaterialization",
    "WorkspaceMaterializationEntry",
    "WorkspacePublishChange",
    "WorkspacePublishResult",
    # 工具函数
    "sanitize_filename",
    "normalize_logical_path",
    "normalize_workspace_logical_path",
    "is_code_filename",
    "is_markdown_filename",
    "is_sandbox_publishable_workspace_path",
    "workspace_logical_path",
    # 主类
    "WorkspaceStore",
    "VirtualFileStore",
]
