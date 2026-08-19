"""Tool 基础模型（Step 4）。

引入：
- `ToolExecutionMode`：工具执行模式（sequential / parallel）
- `ToolDef`：传给 LLM 的工具定义（name + JSON Schema）
- `ToolResult`：工具执行的完整结果（含 tool_call_id / name / content / 元信息）
- `AgentTool`：抽象基类，子类实现 execute()
- `ToolRegistry`：按 name 注册 / 查找 / 删除工具，重复或缺失抛专用异常
- `ToolRegistrationError` / `ToolNotFoundError`：异常类

Step 4 范围：**只定义模型与注册表，不接入 run_event_loop**。
- 不引入 ToolResultMessage（Step 5 加）
- 不修改 stream 适配器（Step 5 加 tool_use parse）
- 不修改 convert_to_llm（Step 5 加 ToolResult 处理）

run_event_loop 的工具执行链路在 Step 5 实现：
    LLM 返回 tool_use → 解析为 ToolCall → ToolRegistry.get(name)
    → AgentTool.execute() → ToolResult → 包装为 ToolResultMessage → 回喂 LLM
"""
from __future__ import annotations

import abc
import asyncio
import typing
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..messages import TextContent

# ============================================================================
# Literal：执行模式
# ============================================================================

#: 单个工具的执行模式。Step 7 才被 run_event_loop 使用。
#: - "parallel"   多个 tool_call 并发执行（默认）
#: - "sequential" 强制串行；本批任一工具声明 sequential，整批都串行
ToolExecutionMode = Literal["sequential", "parallel"]


def _validate_tool_execution_mode(mode: str) -> ToolExecutionMode:
    """Validate a global or per-agent tool execution mode at runtime."""
    if mode not in ("sequential", "parallel"):
        raise ValueError("tool_execution must be 'sequential' or 'parallel'")
    return typing.cast(ToolExecutionMode, mode)


# ============================================================================
# 异常
# ============================================================================


class ToolRegistrationError(Exception):
    """工具注册失败。

    触发场景：
    - 工具 name 为空
    - name 已被其它工具注册（重复）
    """


class ToolNotFoundError(Exception):
    """按 name 查找工具但未找到。

    run_event_loop 执行工具时遇到未注册的工具名会抛此异常（Step 5+）。
    """


# ============================================================================
# ToolDef：传给 LLM 的工具定义
# ============================================================================


class ToolDef(BaseModel):
    """给 LLM 的工具描述。Provider 适配器把它转成 OpenAI tools / Anthropic tools。"""
    name: str                                                 # 唯一名（[a-zA-Z0-9_-]）
    label: str                                                # UI 显示用标签
    description: str                                          # 给 LLM 看的描述
    parameters: dict[str, Any] = Field(                       # JSON Schema dict
        default_factory=lambda: {"type": "object", "properties": {}},
    )


# ============================================================================
# ToolResult：工具执行的完整结果
# ============================================================================


class ToolResult(BaseModel):
    """工具执行结果（agent 内部用，不是消息）。

    与 ToolCall 一一对应：tool_call_id 必须等于对应 ToolCall.id；
    name 必须等于 ToolCall.name（也等于 ToolRegistry 里注册的 name）。

    Step 5 起 run_event_loop 会把 ToolResult 转成 ToolResultMessage（届时引入）
    并加回 messages 列表回喂给 LLM。

    字段：
      tool_call_id  —— 对应 ToolCall.id
      name          —— 工具名（便于 UI / 调试）
      content       —— 发给 LLM 的文本内容
      is_error      —— 是否失败
      terminate     —— 早停提示（仅当本批所有工具都 True 才生效，Step 7 实现）
      details       —— 任意结构化数据，给 UI / 日志，不发给 LLM
    """
    tool_call_id: str
    name: str
    content: list[TextContent] = Field(default_factory=list)
    is_error: bool = False
    terminate: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


#: 工具可在执行期间调用 ``on_update(partial_result)``。callback 返回一个已完成
#: Awaitable，因此同步工具可以忽略返回值，异步工具也可以 ``await`` 它。
ToolUpdateCallback = Callable[[ToolResult], Awaitable[None]]


# ============================================================================
# AgentTool：抽象基类
# ============================================================================


class AgentTool(abc.ABC):
    """Agent 可调用的工具。

    子类必须设置：
        name           —— 唯一名
        label          —— UI 标签（缺省回退到 name）
        description    —— 给 LLM 看的描述
        parameters     —— JSON Schema dict
        execution_mode —— "parallel"（默认）或 "sequential"

    并实现：
        async execute(tool_call_id, args, *, signal=None, on_update=None) -> ToolResult

    Step 4 不做 JSON Schema 校验；后续 step 视情况加 validate_arguments。
    """

    name: str = ""
    label: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    execution_mode: ToolExecutionMode = "parallel"

    @abc.abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        """执行工具。

        ``signal`` 是 request 的协作式中止信号；``on_update`` 用于报告增量
        ToolResult。契约：抛异常表示失败，run_event_loop 会捕获并包成安全的
        ``is_error=True`` ToolResult，避免单工具失败让整个 agent 崩溃。

        runtime 对旧版二参数 execute 实现保留兼容；新工具应实现完整签名。
        """
        ...

    @property
    def definition(self) -> ToolDef:
        """派生为 ToolDef。每次访问构造新对象，防外部修改。

        抛 ToolRegistrationError：
        - name 为空
        - description 为空（LLM 靠描述决定何时选这个工具）
        """
        if not self.name:
            raise ToolRegistrationError(
                f"{type(self).__name__} 未设置 name 属性"
            )
        if not self.description:
            raise ToolRegistrationError(
                f"工具 '{self.name}' 未设置 description（LLM 需要描述来选工具）"
            )
        return ToolDef(
            name=self.name,
            label=self.label or self.name,
            description=self.description,
            parameters=self.parameters or {"type": "object", "properties": {}},
        )


# ============================================================================
# ToolRegistry：按 name 注册 / 查找 / 删除
# ============================================================================


class ToolRegistry:
    """工具注册表。

    - register(tool)         注册；重复名或 name 为空抛 ToolRegistrationError
    - unregister(name)       注销；不存在则静默（参考 dict.pop 语义）
    - get(name)              取工具；不存在抛 ToolNotFoundError
    - has(name)              bool 检查（不抛错）
    - names() / list()       列出已注册工具
    - definitions()          导出 ToolDef 列表（传给 Provider）
    """

    def __init__(self, tools: list[AgentTool] | None = None):
        self._tools: dict[str, AgentTool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: AgentTool) -> None:
        """注册工具。

        抛 ToolRegistrationError：
        - tool.name 为空
        - tool.description 为空（LLM 需要描述来决定何时选这个工具）
        - tool.name 已注册（重复）
        """
        if not tool.name:
            raise ToolRegistrationError(
                f"{type(tool).__name__} 未设置 name 属性"
            )
        if not tool.description:
            raise ToolRegistrationError(
                f"工具 '{tool.name}' 未设置 description（LLM 需要描述来选工具）"
            )
        if tool.name in self._tools:
            raise ToolRegistrationError(
                f"工具 '{tool.name}' 已注册（重复注册）"
            )
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具。不存在则静默（不抛错）。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> AgentTool:
        """取工具。不存在抛 ToolNotFoundError。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"工具 '{name}' 未注册"
            )
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        """已注册工具名列表（按插入序）。"""
        return list(self._tools.keys())

    def list(self) -> list[AgentTool]:
        """已注册工具对象列表（按插入序）。"""
        return list(self._tools.values())

    def definitions(self) -> list[ToolDef]:  # type: ignore[valid-type]
        """导出所有工具的 ToolDef（会被 Provider 适配器转成各家协议）。"""
        return [t.definition for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> typing.Iterator[AgentTool]:
        return iter(self._tools.values())

    # 兼容旧 API（Step 4 重命名前后都能用）
    def add(self, tool: AgentTool) -> None:
        self.register(tool)

    def remove(self, name: str) -> None:
        self.unregister(name)

    def all(self) -> list[AgentTool]:  # type: ignore[valid-type]
        return self.list()


# 内置工具实现（Step 5.5 新增）
# Session 文件工具（list_files / view_file / write_file）由 Web composition root
# 绑定 VirtualFileStore + session_id_getter 后注册。
# ruff: noqa: E402, I001
from .list_files import ListFilesTool, create_list_files_tool
from .view_file import (
    DEFAULT_MAX_BYTES as VIEW_FILE_DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS as VIEW_FILE_DEFAULT_MAX_ROWS,
    ViewFileTool,
    create_view_file_tool,
)
from .web_search import WebSearchTool
from .write_file import WriteFileTool, create_write_file_tool

__all__ = [
    "ToolExecutionMode",
    "ToolRegistrationError", "ToolNotFoundError",
    "ToolDef", "ToolResult", "ToolUpdateCallback", "AgentTool", "ToolRegistry",
    # 内置工具
    "WebSearchTool",
    # P0-3 内置工具
    "ListFilesTool", "create_list_files_tool",
    "ViewFileTool", "create_view_file_tool",
    "WriteFileTool", "create_write_file_tool",
    "VIEW_FILE_DEFAULT_MAX_BYTES", "VIEW_FILE_DEFAULT_MAX_ROWS",
]
