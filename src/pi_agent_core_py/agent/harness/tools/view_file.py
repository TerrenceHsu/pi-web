"""Harness tool for viewing files in the active session.

支持的格式：
- markdown / text / code / json / xml / yaml / toml / ini / log → utf-8 前 max_bytes
- html → stdlib html.parser 抽纯文本 + 原始 excerpt
- csv → csv.Sniffer 推断 delimiter + DictReader 读 max_rows
- parquet → pyarrow 读 schema + row_count + 前 max_rows（缺 pyarrow 时返回明确错误）

不支持的格式：
- pdf → 元信息 + "PDF 内容本轮暂未解析"
- image_unsupported → "当前不支持图片内容分析"
- binary / unsupported → 元信息 + 提示

安全：
- 跨 session 访问由 WorkspaceStore.get_for_session 强校验，本工具只拿当前 sid
- 文件读取异常一律转 ToolResult(is_error=True)，不抛出
- 不返回 path（path 是 agent 内部细节）

默认值：
- max_bytes = 64 * 1024  (text / md / html excerpt)
- max_rows = 50          (csv / parquet preview)

注：WorkspaceStore / FileRef / FileStore 异常类通过 deferred import
（在 execute 方法内部）引入——避免 tools 子包 module-load 时触发
web/__init__.py（FastAPI 链）形成 import 循环（agent → tools → web → app
→ harness → agent）。
"""
from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ...messages import TextContent
from ...tooling import AgentTool, ToolResult, ToolUpdateCallback

if TYPE_CHECKING:
    from agent_workspace.store import FileRef, WorkspaceStore

# ============================================================================
# 常量
# ============================================================================


#: 默认 text / md 读取上限——避免把整个大文件塞 LLM 上下文
DEFAULT_MAX_BYTES = 64 * 1024

#: 默认 csv / parquet preview 行数
DEFAULT_MAX_ROWS = 50

#: FileFormat 与 view_file 输出 kind 的对应关系
FileKind = Literal["text", "html", "table", "pdf", "unsupported", "binary"]


# ============================================================================
# 格式分类
# ============================================================================


#: 已知文本/代码扩展名 → format="text"（content 直接 utf-8 读）
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".log", ".ini", ".conf", ".cfg",
    ".json", ".xml", ".yaml", ".yml", ".toml",
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".vue", ".svelte",
    ".css", ".scss", ".less",
    ".sql",
    ".sh", ".bash", ".zsh", ".fish",
    ".go", ".rs", ".java", ".kt", ".kts",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh",
    ".rb", ".php", ".swift",
    ".env",
})

#: markdown 扩展名
_MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".mdx"})

#: html 扩展名
_HTML_EXTENSIONS: frozenset[str] = frozenset({".html", ".htm", ".xhtml"})

#: csv 扩展名
_CSV_EXTENSIONS: frozenset[str] = frozenset({".csv", ".tsv"})

#: parquet 扩展名
_PARQUET_EXTENSIONS: frozenset[str] = frozenset({".parquet", ".pq"})

#: pdf 扩展名
_PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})

#: 图片扩展名（统一 unsupported）
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".svg",
    ".heif", ".heic",
})


def _classify_format(filename: str, mime: str) -> str:
    """根据文件名 + MIME 推断 format 标签。

    返回值见 messages.FileFormat。优先按 MIME，其次按扩展名。
    """
    mime_lc = (mime or "").lower()
    name_lc = (filename or "").lower()
    ext = Path(name_lc).suffix

    # 图片
    if mime_lc.startswith("image/"):
        return "image_unsupported"
    if ext in _IMAGE_EXTENSIONS:
        return "image_unsupported"

    # pdf
    if mime_lc == "application/pdf" or ext in _PDF_EXTENSIONS:
        return "pdf"

    # parquet（优先 MIME，octet-stream + .parquet 也算）
    if (
        mime_lc in ("application/vnd.apache.parquet", "application/x-parquet")
        or ext in _PARQUET_EXTENSIONS
    ):
        return "parquet"

    # csv
    if mime_lc in ("text/csv", "application/csv") or ext in _CSV_EXTENSIONS:
        return "csv"

    # markdown
    if mime_lc in ("text/markdown", "text/x-markdown") or ext in _MARKDOWN_EXTENSIONS:
        return "markdown"

    # html
    if mime_lc in ("text/html", "application/xhtml+xml") or ext in _HTML_EXTENSIONS:
        return "html"

    # text/*
    if mime_lc.startswith("text/"):
        return "text"

    # 已知文本类 MIME / 扩展名
    if mime_lc in (
        "application/json", "application/xml", "application/yaml",
        "application/x-yaml", "application/toml",
        "application/javascript", "application/x-sh",
    ):
        return "text"
    if ext in _TEXT_EXTENSIONS:
        return "text"

    return "unsupported"


# ============================================================================
# HTML 文本提取（stdlib html.parser，不依赖 BeautifulSoup）
# ============================================================================


class _TextExtractor(HTMLParser):
    """把 HTML 转纯文本。

    - <script> / <style> 内容忽略
    - 其它标签的文本数据收集，块级标签之间补一个换行
    """

    _BLOCK_TAGS: frozenset[str] = frozenset({
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "nav", "main", "table",
        "tr", "blockquote", "pre", "hr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._skip_depth = 0  # 在 script/style 内 >0

    def handle_starttag(self, tag: str, attrs: list[Any]) -> None:  # noqa: ARG002
        tag_lc = tag.lower()
        if tag_lc in ("script", "style"):
            self._skip_depth += 1
        elif tag_lc in self._BLOCK_TAGS:
            # 块级标签前补换行，避免不同段落挤一行
            if self._pieces and not self._pieces[-1].endswith("\n"):
                self._pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lc = tag.lower()
        if tag_lc in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag_lc in self._BLOCK_TAGS:
            if self._pieces and not self._pieces[-1].endswith("\n"):
                self._pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if data:
            self._pieces.append(data)

    def get_text(self) -> str:
        text = "".join(self._pieces)
        # 折叠多余空白（保留换行）
        lines = [line.strip() for line in text.splitlines()]
        lines = [ln for ln in lines if ln]
        return "\n".join(lines)


def _extract_html_text(raw: str) -> str:
    """提取 HTML 可读文本。空 / 解析失败返回空字符串。"""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # 解析失败 → 返回已收集到的部分
        pass
    return parser.get_text()


# ============================================================================
# pyarrow parquet 读取（lazy import）
# ============================================================================


def _read_parquet_summary(
    path: Path, max_rows: int,
) -> tuple[dict[str, str], int, list[dict[str, Any]], str | None]:
    """读 parquet schema + row_count + 前 max_rows。

    返回 (schema_dict, row_count, rows, warning)
    - schema_dict: {column_name: arrow_type_string}
    - row_count: metadata.num_rows（不可得时 -1）
    - rows: 前 max_rows 的 dict list（已 JSON-safe 化）
    - warning: 解析过程中的非致命提示（如类型转字符串失败）

    pyarrow 不可用 → 抛 ImportError，由上层 catch 转 ToolResult(is_error=True)。
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise ImportError(
            "parquet support requires pyarrow; install with `pip install pyarrow>=15`"
        ) from e

    pf = pq.ParquetFile(str(path))
    arrow_schema = pf.schema_arrow
    schema_dict: dict[str, str] = {
        name: str(arrow_schema.field(name).type) for name in arrow_schema.names
    }

    metadata = pf.metadata
    row_count = metadata.num_rows if metadata is not None else -1

    # 只读前 max_rows
    batch_iter = pf.iter_batches(
        batch_size=max(1, max_rows),
    )
    rows: list[dict[str, Any]] = []
    warning: str | None = None
    seen = 0
    for batch in batch_iter:
        if seen >= max_rows:
            break
        n = min(batch.num_rows, max_rows - seen)
        for i in range(n):
            try:
                row: dict[str, Any] = {}
                for name in arrow_schema.names:
                    val = batch.column(name)[i].as_py()
                    row[name] = _json_safe_parquet_value(val)
                rows.append(row)
            except Exception as e:
                # 单行解析失败——记 warning，但继续
                warning = f"row {seen + i} parse failed: {type(e).__name__}: {e}"
                break
        seen += batch.num_rows
        if seen >= max_rows:
            break
    return schema_dict, row_count, rows, warning


def _json_safe_parquet_value(val: Any) -> Any:
    """parquet cell → JSON-safe Python 值。

    datetime / date / time → ISO 字符串；bytes → base64；decimal → float；
    list / tuple → list；其它原样返回（pyarrow as_py 一般已是 int/float/str/bool/None）。
    """
    import datetime
    import decimal

    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    if isinstance(val, datetime.timedelta):
        return val.total_seconds()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, (bytes, bytearray)):
        import base64
        return base64.b64encode(bytes(val)).decode("ascii")
    if isinstance(val, (list, tuple)):
        return [_json_safe_parquet_value(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _json_safe_parquet_value(v) for k, v in val.items()}
    return val


# ============================================================================
# CSV 读取
# ============================================================================


def _read_csv_summary(
    raw_text: str, max_rows: int,
) -> tuple[list[str], list[dict[str, str]], bool, str | None]:
    """读 csv → (columns, rows, truncated, warning)。

    - 用 csv.Sniffer 推断 delimiter；失败 fallback ","
    - DictReader 读最多 max_rows + 1 行（多读 1 行用于判断 truncated）
    - 编码已由上层 utf-8 decode（errors=replace 兜底）
    """
    sample = raw_text[:4096]
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample)
        delimiter = dialect.delimiter
    except Exception:
        # Sniffer 失败 → 默认 comma
        pass

    reader = csv.DictReader(io.StringIO(raw_text), delimiter=delimiter)
    columns: list[str] = list(reader.fieldnames or [])
    rows: list[dict[str, str]] = []
    truncated = False
    warning: str | None = None
    count = 0
    for row in reader:
        if count >= max_rows:
            truncated = True
            break
        # row 是 dict[str, str]——DictReader 保证；这里转成可 JSON 化的纯 dict
        rows.append({k: (v if v is not None else "") for k, v in row.items()})
        count += 1

    # 检查是否还有更多行（即使没截断）—— 用一个简单的指示
    if not truncated and count == max_rows:
        # 可能刚好读到 max_rows 就结束；统一标 truncated=False 让 LLM 知道
        # 实际是否还有更多行需要看 row_count
        pass

    return columns, rows, truncated, warning


# ============================================================================
# 主类
# ============================================================================


class ViewFileTool(AgentTool):
    """读取当前会话中用户上传或 Agent 创建文件的内容。"""

    name = "view_file"
    label = "View File"
    description = (
        "Read the content or structural summary of a file in the current session "
        "folder. Supports user uploads and agent-created files in markdown, html, "
        "csv, parquet, and common "
        "text/code files. Image understanding is not supported—images and "
        "other unsupported types return a clear message."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "The file_id returned by list_files / upload.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Max bytes to read for text/markdown/html (default 65536).",
                "minimum": 1,
                "maximum": 1024 * 1024,
            },
            "max_rows": {
                "type": "integer",
                "description": "Max preview rows for csv/parquet (default 50).",
                "minimum": 1,
                "maximum": 1000,
            },
        },
        "required": ["file_id"],
        "additionalProperties": False,
    }
    execution_mode = "parallel"

    def __init__(
        self,
        *,
        file_store: WorkspaceStore,
        session_id_getter: Callable[[], str | None],
    ) -> None:
        self._file_store = file_store
        self._session_id_getter = session_id_getter

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        file_id = args.get("file_id") or ""
        if not isinstance(file_id, str) or not file_id:
            return _error(
                tool_call_id, self.name,
                "file_id is required and must be a non-empty string.",
                error_type="InvalidArguments",
            )

        max_bytes = args.get("max_bytes") or DEFAULT_MAX_BYTES
        try:
            max_bytes = int(max_bytes)
            if max_bytes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _error(
                tool_call_id, self.name,
                f"max_bytes must be a positive int (got {args.get('max_bytes')!r})",
                error_type="InvalidArguments",
            )

        max_rows = args.get("max_rows") or DEFAULT_MAX_ROWS
        try:
            max_rows = int(max_rows)
            if max_rows <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _error(
                tool_call_id, self.name,
                f"max_rows must be a positive int (got {args.get('max_rows')!r})",
                error_type="InvalidArguments",
            )

        sid = self._session_id_getter()
        if not sid:
            return _error(
                tool_call_id, self.name,
                "view_file failed: no active session_id (call within a session-scoped context).",
                error_type="NoActiveSession",
            )

        # deferred import：避免 tools module-load 时触发 web/__init__ 循环
        from agent_workspace.store import (
            FileAccessDeniedError,
            VirtualFileNotFoundError,
        )

        # 拿 FileRef（强校验 session 隔离）
        try:
            ref = await self._file_store.get_for_session(sid, file_id)
        except VirtualFileNotFoundError as e:
            return _error(tool_call_id, self.name, str(e), error_type="FileNotFound")
        except FileAccessDeniedError as e:
            return _error(
                tool_call_id, self.name, str(e),
                error_type="AccessDenied",
            )
        except Exception as e:
            return _error(
                tool_call_id, self.name,
                f"get file metadata failed: {type(e).__name__}: {e}",
                error_type=type(e).__name__,
            )

        fmt = _classify_format(ref.name, ref.mime)

        # 派发到对应 reader
        try:
            if fmt in ("markdown", "text"):
                return self._view_text_like(tool_call_id, ref, fmt, max_bytes)
            if fmt == "html":
                return self._view_html(tool_call_id, ref, max_bytes)
            if fmt == "csv":
                return self._view_csv(tool_call_id, ref, max_bytes, max_rows)
            if fmt == "parquet":
                return self._view_parquet(tool_call_id, ref, max_rows)
            if fmt == "pdf":
                return self._view_pdf(tool_call_id, ref)
            if fmt == "image_unsupported":
                return self._view_image_unsupported(tool_call_id, ref)
            # binary / unsupported
            return self._view_binary_or_unsupported(tool_call_id, ref, fmt)
        except Exception as e:
            return _error(
                tool_call_id, self.name,
                f"read failed for {ref.name!r}: {type(e).__name__}: {e}",
                error_type=type(e).__name__,
            )

    # ------------------------------------------------------------------
    # 各 format reader
    # ------------------------------------------------------------------

    def _view_text_like(
        self, tool_call_id: str, ref: FileRef, fmt: str, max_bytes: int,
    ) -> ToolResult:
        path = Path(ref.path)
        # 优先 utf-8；失败用 errors=replace 兜底
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(raw.encode("utf-8", errors="replace")) > max_bytes
        content = raw[:max_bytes]  # 简单按字符数截——utf-8 字符数 ≤ 字节数

        payload: dict[str, Any] = {
            "kind": "text",
            "format": fmt,
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "truncated": truncated,
            "max_bytes": max_bytes,
            "content": content,
        }
        text = f"[{ref.name} ({fmt})]\n{content}"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )

    def _view_html(
        self, tool_call_id: str, ref: FileRef, max_bytes: int,
    ) -> ToolResult:
        path = Path(ref.path)
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(raw) > max_bytes
        excerpt = raw[:max_bytes]
        text = _extract_html_text(excerpt)

        payload = {
            "kind": "html",
            "format": "html",
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "truncated": truncated,
            "max_bytes": max_bytes,
            "content_text": text,
            "content_html_excerpt": excerpt,
        }
        summary = f"[{ref.name} (html) extracted text]\n{text}"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=summary)],
            details=payload,
        )

    def _view_csv(
        self,
        tool_call_id: str,
        ref: FileRef,
        max_bytes: int,
        max_rows: int,
    ) -> ToolResult:
        path = Path(ref.path)
        # csv 仍按 max_bytes 限制读入大小
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_text(encoding="utf-8", errors="replace")
        truncated_by_bytes = len(raw) > max_bytes
        if truncated_by_bytes:
            raw = raw[:max_bytes]
        columns, rows, truncated_rows, warning = _read_csv_summary(raw, max_rows)

        payload = {
            "kind": "table",
            "format": "csv",
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "columns": columns,
            "rows": rows,
            "row_preview_count": len(rows),
            "truncated": bool(truncated_rows or truncated_by_bytes),
            "max_bytes": max_bytes,
            "max_rows": max_rows,
            "delimiter_warning": warning,
        }
        if rows:
            cols_str = ", ".join(columns)
            head = "\n".join(
                "  " + " | ".join(f"{k}={v}" for k, v in row.items())
                for row in rows[:5]
            )
            text = (
                f"[{ref.name} (csv)]\n"
                f"columns: {cols_str}\n"
                f"preview ({len(rows)} rows):\n{head}\n"
                f"(truncated={payload['truncated']})"
            )
        else:
            text = f"[{ref.name} (csv)] empty (no rows)"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )

    def _view_parquet(
        self, tool_call_id: str, ref: FileRef, max_rows: int,
    ) -> ToolResult:
        try:
            schema_dict, row_count, rows, warning = _read_parquet_summary(
                Path(ref.path), max_rows,
            )
        except ImportError as e:
            # pyarrow 缺失——明确告诉 LLM
            return _error(
                tool_call_id, self.name,
                f"parquet read failed: {e}",
                error_type="PyArrowNotAvailable",
                details={
                    "kind": "error",
                    "format": "parquet",
                    "file_id": ref.id,
                    "name": ref.name,
                },
            )

        payload = {
            "kind": "table",
            "format": "parquet",
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "schema": schema_dict,
            "row_count": row_count,
            "columns": list(schema_dict.keys()),
            "rows": rows,
            "row_preview_count": len(rows),
            "truncated": (
                row_count > max_rows if row_count >= 0
                else len(rows) >= max_rows
            ),
            "max_rows": max_rows,
            "warning": warning,
        }
        if rows:
            cols_str = ", ".join(f"{c}({t})" for c, t in schema_dict.items())
            head = "\n".join(
                "  " + " | ".join(f"{k}={v}" for k, v in row.items())
                for row in rows[:5]
            )
            text = (
                f"[{ref.name} (parquet)]\n"
                f"row_count: {row_count}\n"
                f"schema: {cols_str}\n"
                f"preview ({len(rows)} rows):\n{head}\n"
                f"(truncated={payload['truncated']})"
            )
        else:
            text = f"[{ref.name} (parquet)] empty (no rows)"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )

    def _view_pdf(self, tool_call_id: str, ref: FileRef) -> ToolResult:
        payload = {
            "kind": "pdf",
            "format": "pdf",
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "message": (
                "PDF 内容本轮暂未解析，请用户提供文本版本或后续启用 PDF parser。"
            ),
        }
        text = f"[{ref.name} (pdf)] PDF 内容本轮暂未解析。"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )

    def _view_image_unsupported(
        self, tool_call_id: str, ref: FileRef,
    ) -> ToolResult:
        payload = {
            "kind": "unsupported",
            "format": "image_unsupported",
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "message": "当前不支持图片内容分析。",
        }
        text = (
            f"[{ref.name} (image)] 当前不支持图片内容分析"
            f"（不做 OCR、不做视觉理解）。"
        )
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )

    def _view_binary_or_unsupported(
        self, tool_call_id: str, ref: FileRef, fmt: str,
    ) -> ToolResult:
        kind = "binary" if fmt == "binary" else "unsupported"
        payload: dict[str, Any] = {
            "kind": kind,
            "format": fmt,
            "file_id": ref.id,
            "name": ref.name,
            "mime": ref.mime,
            "size": ref.size,
            "sha256": ref.sha256,
            "message": (
                "该文件不是可直接读取的文本/表格文件。"
                if fmt == "binary"
                else "该文件格式本轮不在 view_file 直接支持列表内。"
            ),
        }
        text = (
            f"[{ref.name} ({fmt})] "
            + payload["message"]
        )
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )


# ============================================================================
# 内部辅助
# ============================================================================


def _error(
    tool_call_id: str,
    name: str,
    message: str,
    *,
    error_type: str,
    details: dict[str, Any] | None = None,
) -> ToolResult:
    """构造 is_error=True 的 ToolResult（统一错误响应）。"""
    payload: dict[str, Any] = {"error_type": error_type}
    if details:
        payload.update(details)
    return ToolResult(
        tool_call_id=tool_call_id,
        name=name,
        content=[TextContent(text=message)],
        is_error=True,
        details=payload,
    )


def create_view_file_tool(
    *,
    file_store: WorkspaceStore,
    session_id_getter: Callable[[], str | None],
) -> ViewFileTool:
    """factory：构造 view_file 工具，绑定 file_store + session_id_getter。"""
    return ViewFileTool(
        file_store=file_store, session_id_getter=session_id_getter,
    )


__all__ = [
    "ViewFileTool",
    "create_view_file_tool",
    "_classify_format",  # list_files 复用
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ROWS",
]
