"""VirtualFileStore —— 会话级文件上传存储（P0-2）。

设计要点：
- 按 session_id 分桶：`uploads/{session_id}/{file_id}/{safe_filename}`
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

import hashlib
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path

from fastapi import UploadFile
from pydantic import BaseModel

# ============================================================================
# 常量
# ============================================================================


#: 默认单文件大小上限：25 MB
DEFAULT_MAX_FILE_SIZE = 25 * 1024 * 1024

#: 默认单 session 总上传上限：100 MB
DEFAULT_MAX_SESSION_SIZE = 100 * 1024 * 1024

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
    """VirtualFileStore 基类异常。"""


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


# ============================================================================
# VirtualFileStore
# ============================================================================


class VirtualFileStore:
    """会话级文件存储。

    生命周期：
        store = VirtualFileStore(root_dir="./uploads")
        await store.init()                          # 创建 root_dir
        ref = await store.save(session_id, upload)  # 流式读取 upload
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
        self._root_dir = Path(root_dir)
        self._max_file_size = max_file_size
        self._max_session_size = max_session_size
        self._initialized = False

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
        """创建 root_dir（幂等）。"""
        if self._initialized:
            return
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._initialized = True

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
            return FileRef.model_validate(payload)
        except Exception:
            # 损坏 metadata 静默忽略；上层视为 file not found
            return None

    def _write_metadata(self, file_dir: Path, ref: FileRef) -> None:
        """写 metadata.json。"""
        meta_path = file_dir / "metadata.json"
        meta_path.write_text(
            json.dumps(ref.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # save：上传文件
    # ------------------------------------------------------------------

    async def save(
        self,
        session_id: str,
        upload: UploadFile,
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
        ref = FileRef(
            id=file_id,
            session_id=session_id,
            name=safe_name,
            size=total,
            mime=mime,
            sha256=sha.hexdigest(),
            path=str(target_path),
            created_at=_now_ms(),
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
            if not file_dir.is_dir():
                continue
            ref = self._read_metadata(file_dir)
            if ref is not None:
                out.append(ref)
        # 按 created_at 升序，便于 UI 稳定排序
        out.sort(key=lambda r: r.created_at)
        return out

    async def session_total_size(self, session_id: str) -> int:
        """当前 session 所有文件总字节数。"""
        files = await self.list_session(session_id)
        return sum(f.size for f in files)

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, file_id: str) -> None:
        """删除单文件（不带 session 校验，扫库定位）。

        推荐用 delete_for_session(sid, fid) 做隔离；这里保留以兼容 P0-2 早期 API。
        """
        ref = await self.get(file_id)
        if ref is None:
            raise VirtualFileNotFoundError(f"file {file_id!r} not found")
        file_dir = self._file_dir(ref.session_id, file_id)
        # 删整个 file_dir
        try:
            for child in file_dir.iterdir():
                child.unlink(missing_ok=True)
            file_dir.rmdir()
        except FileNotFoundError:
            pass
        except Exception as e:
            raise FileStoreError(
                f"delete failed for {file_id!r}: {type(e).__name__}: {e}"
            ) from e

    async def delete_for_session(
        self, session_id: str, file_id: str,
    ) -> FileRef:
        """session 隔离删除：返回被删的 FileRef。

        - 文件不存在 → VirtualFileNotFoundError
        - 跨 session → FileAccessDeniedError
        """
        ref = await self.get_for_session(session_id, file_id)
        file_dir = self._file_dir(session_id, file_id)
        try:
            for child in file_dir.iterdir():
                child.unlink(missing_ok=True)
            file_dir.rmdir()
        except FileNotFoundError:
            pass
        except Exception as e:
            raise FileStoreError(
                f"delete failed for {file_id!r}: {type(e).__name__}: {e}"
            ) from e
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
                try:
                    for child in file_dir.iterdir():
                        child.unlink(missing_ok=True)
                    file_dir.rmdir()
                    count += 1
                except Exception:
                    continue
            session_dir.rmdir()
        except Exception:
            pass
        return count


__all__ = [
    # 常量
    "DEFAULT_MAX_FILE_SIZE",
    "DEFAULT_MAX_SESSION_SIZE",
    # 异常
    "FileStoreError",
    "VirtualFileNotFoundError",
    "FileAccessDeniedError",
    "FileTooLargeError",
    "SessionStorageLimitError",
    "UnsafeFilenameError",
    # 数据模型
    "FileRef",
    # 工具函数
    "sanitize_filename",
    # 主类
    "VirtualFileStore",
]
