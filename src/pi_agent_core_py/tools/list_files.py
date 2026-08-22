"""list_files 工具——列出当前会话文件夹中的文件（P0-3）。

设计要点：
- factory pattern：`create_list_files_tool(file_store=..., session_id_getter=...)`
  避免工具内部依赖全局状态；Web app / harness 构造时显式注入依赖
- session 隔离：通过 `session_id_getter` 拿当前 sid，调
  `WorkspaceStore.list_session(sid)` 拿 FileRef 列表
- 不返回 path（path 是 agent 内部细节，不应进 LLM 上下文）
- file_store 未配置 / 缺 session_id → 返回 `ToolResult(is_error=True)`
- 输出结构 JSON serializable；按 created_at 升序（与 store 一致）
- format 字段对图片标 "image_unsupported"，由 _classify_format 推断

注：WorkspaceStore / FileRef 通过 TYPE_CHECKING 引用——避免 tools 子包
在 module-load 阶段触发 web/__init__（FastAPI 链）形成 import 循环
（agent.py → tools → web → app → harness → agent）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..messages import TextContent
from . import AgentTool, ToolResult, ToolUpdateCallback
from .view_file import _classify_format

if TYPE_CHECKING:
    # 仅用于类型提示——运行时不导入，避免循环依赖
    from ..web.files import FileRef, WorkspaceStore


class ListFilesTool(AgentTool):
    """列出当前会话文件夹中的用户上传与 Agent 创建文件。"""

    name = "list_files"
    label = "List Files"
    description = (
        "List user-uploaded and agent-created files in the current conversation "
        "session folder. "
        "Returns metadata (id / name / mime / size / sha256 / format) for each file. "
        "Does not return file paths or content—use view_file to read content."
    )
    parameters = {
        "type": "object",
        "properties": {},
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
        sid = self._session_id_getter()
        if not sid:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(
                    text="list_files failed: no active session_id (call within a "
                         "session-scoped context).",
                )],
                is_error=True,
                details={"error_type": "NoActiveSession"},
            )

        try:
            refs = await self._file_store.list_session(sid)
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(
                    text=f"list_files failed: {type(e).__name__}: {e}",
                )],
                is_error=True,
                details={"error_type": type(e).__name__},
            )

        items: list[dict[str, Any]] = []
        for ref in refs:
            items.append(_ref_to_summary(ref))

        payload = {
            "files": items,
            "count": len(items),
            "session_id": sid,
        }

        # 同时给一段简短文本——LLM 单步读得到结果（不必解析 JSON 也能聊）
        if not items:
            text = "当前会话文件夹暂无文件。"
        else:
            lines = [f"当前会话共 {len(items)} 个文件："]
            for it in items:
                lines.append(
                    f"- {it['logical_path']} ({it['format']}, {it['size']} bytes) "
                    f"id={it['id']}"
                )
            text = "\n".join(lines)

        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            details=payload,
        )


def _ref_to_summary(ref: FileRef) -> dict[str, Any]:
    """FileRef → 给 LLM 看的摘要 dict（不含 path / content）。"""
    return {
        "id": ref.id,
        "name": ref.name,
        "logical_path": ref.logical_path,
        "origin": ref.origin,
        "purpose": ref.purpose,
        "mime": ref.mime,
        "format": _classify_format(ref.name, ref.mime),
        "size": ref.size,
        "sha256": ref.sha256,
    }


def create_list_files_tool(
    *,
    file_store: WorkspaceStore,
    session_id_getter: Callable[[], str | None],
) -> ListFilesTool:
    """factory：构造 list_files 工具，绑定 file_store + session_id_getter。"""
    return ListFilesTool(
        file_store=file_store, session_id_getter=session_id_getter,
    )


__all__ = ["ListFilesTool", "create_list_files_tool"]
