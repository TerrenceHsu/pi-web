"""P1-D1: Session Export Markdown——canonical renderer + filename sanitize。

**设计**（用户审核补充）：
1. renderer 是纯函数——不感知 SQLite / revision 数据库
2. 输入用 `ExportMessage(id, role, text, idx)` 数据结构，按 idx ASC
3. 默认不导出 runtime cards（tool args / MCP env / trace / request_id / sequence）
4. HTML 保留用户原文——不执行/不解释/不删除
5. filename 做 path traversal + 控制字符清理 + 80 字符上限
6. 超限返回 413，不静默截断

**D2 复用**：renderer 保持纯函数，D2 revision 过滤在 API 层完成后再传入 renderer。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# ============================================================================
# 数据结构
# ============================================================================


@dataclass(frozen=True)
class ExportMessage:
    """导出用的单条消息——API 层从 SQLite 读取后构造。"""

    id: str
    role: str  # "user" | "assistant" | "toolResult" | "summary" | "system" | ...
    text: str
    idx: int


@dataclass(frozen=True)
class MarkdownExportOptions:
    """导出选项——D1 默认安全最小导出。"""

    include_runtime_summaries: bool = False
    include_file_names: bool = True


# ============================================================================
# 常量
# ============================================================================


MAX_EXPORT_CHARS = 2_000_000
MAX_EXPORT_BYTES = 5 * 1024 * 1024
MAX_FILENAME_LEN = 80
FALLBACK_FILENAME = "chat-export"


# ============================================================================
# Filename 安全
# ============================================================================


# 移除 Windows / Unix 非法字符 + 控制字符 + path traversal
_FILENAME_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_DOT_DOT = re.compile(r"\.\.+")


def sanitize_filename(title: str) -> str:
    r"""清理 session title → 安全 filename（不含扩展名）。

    - 移除 `/ \ : * ? " < > |` 和控制字符
    - 去除 `..`（path traversal）
    - trim 空白和结尾点
    - 最大 80 字符
    - 空标题 fallback `chat-export`
    """
    if not title:
        return FALLBACK_FILENAME
    safe = _FILENAME_UNSAFE.sub("", title)
    safe = _DOT_DOT.sub("", safe)
    safe = safe.strip().rstrip(".")
    if not safe:
        return FALLBACK_FILENAME
    if len(safe) > MAX_FILENAME_LEN:
        safe = safe[:MAX_FILENAME_LEN].rstrip(".")
    return safe or FALLBACK_FILENAME


def build_export_filename(title: str, date_str: str) -> str:
    """构造 `{sanitized_title}_{YYYYMMDD}.md`。"""
    safe = sanitize_filename(title)
    return f"{safe}_{date_str}.md"


def build_content_disposition(filename: str) -> str:
    """构造 Content-Disposition——RFC 5987 UTF-8 filename*。

    ASCII fallback 用 sanitized filename；Unicode 用 filename*。
    """
    from urllib.parse import quote

    ascii_fallback = filename.encode("ascii", errors="ignore").decode("ascii")
    if not ascii_fallback or any(ord(c) > 127 for c in ascii_fallback):
        ascii_fallback = FALLBACK_FILENAME + ".md"
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"


# ============================================================================
# Canonical Markdown Renderer
# ============================================================================


def render_session_markdown(
    *,
    session_title: str,
    messages: Sequence[ExportMessage],
    options: MarkdownExportOptions | None = None,
    exported_at: str = "",
) -> str:
    """渲染 session → Markdown 字符串。

    **纯函数**——不访问 SQLite / revision / streamItems。
    API 层负责读取 active messages 并按 idx ASC 传入。

    输出结构：
    ```
    # {Session Title}

    _Exported at: {UTC ISO}_

    ## User

    user 正文

    ## Assistant

    assistant 正文

    ---

    ## User

    ...
    ```

    **安全**：
    - 不导出 system prompt / Skill prompt body / MCP env / raw tool args / trace
    - 不导出 request_id / sequence / absolute path
    - HTML 保留用户原文（不执行/不解释/不删除）
    - 换行统一 `\n`
    """
    if options is None:
        options = MarkdownExportOptions()

    lines: list[str] = []
    # 标题
    title = session_title or "Session"
    lines.append(f"# {title}")
    lines.append("")
    if exported_at:
        lines.append(f"_Exported at: {exported_at}_")
        lines.append("")

    first = True
    for msg in messages:
        role = msg.role
        text = msg.text or ""

        # 过滤——默认只导出 user / assistant
        if role == "user":
            header = "User"
        elif role == "assistant":
            header = "Assistant"
        elif role == "toolResult":
            if not options.include_runtime_summaries:
                continue
            header = "Tool Result"
        elif role == "system":
            # 永不导出 system message
            continue
        else:
            if not options.include_runtime_summaries:
                continue
            header = role.capitalize() or "Message"

        if not first:
            lines.append("---")
            lines.append("")

        lines.append(f"## {header}")
        lines.append("")
        lines.append(text.rstrip())
        lines.append("")
        first = False

    result = "\n".join(lines)
    # 文件末尾保留一个换行
    if result and not result.endswith("\n"):
        result += "\n"
    return result


# ============================================================================
# 导出大小检查
# ============================================================================


def check_export_size(markdown: str) -> str | None:
    """检查导出大小——超限返回安全 error message，否则 None。"""
    char_count = len(markdown)
    if char_count > MAX_EXPORT_CHARS:
        return f"export exceeds max chars ({char_count} > {MAX_EXPORT_CHARS})"
    byte_count = len(markdown.encode("utf-8"))
    if byte_count > MAX_EXPORT_BYTES:
        return f"export exceeds max bytes ({byte_count} > {MAX_EXPORT_BYTES})"
    return None


# ============================================================================
# Helper——从 SQLite messages 构造 ExportMessage
# ============================================================================


def messages_to_export_items(
    messages: Sequence[Any],
) -> list[ExportMessage]:
    """从 AgentMessage list 构造 ExportMessage list——API 层使用。

    按 idx ASC 排序（调用方应已排好）。
    只提取 user / assistant 正文——不提取 toolResult / system。
    """
    items: list[ExportMessage] = []
    for idx, msg in enumerate(messages):
        role = getattr(msg, "role", "")
        text = _extract_text(msg)
        # 用 idx 作为排序键——如果 message 有稳定 id 用 id
        msg_id = (
            getattr(msg, "id", None)
            or getattr(msg, "message_id", None)
            or f"msg-{idx}"
        )
        items.append(ExportMessage(id=str(msg_id), role=role, text=text, idx=idx))
    return items


def _extract_text(msg: Any) -> str:
    """从 AgentMessage.content 提取纯文本——拼接所有 TextContent。"""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif hasattr(block, "type") and block.type == "text":
                parts.append(getattr(block, "text", ""))
        return "\n".join(parts)
    return ""


__all__ = [
    "ExportMessage",
    "MarkdownExportOptions",
    "MAX_EXPORT_CHARS",
    "MAX_EXPORT_BYTES",
    "sanitize_filename",
    "build_export_filename",
    "build_content_disposition",
    "render_session_markdown",
    "check_export_size",
    "messages_to_export_items",
]
