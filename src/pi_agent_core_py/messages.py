"""消息类型（Step 5 + Step 15 + P0-3）。

相对 Step 4 的变化：
- 新增 `ToolResultMessage`：工具执行结果作为消息进入 AgentMessage 历史
- `Message` / `AgentMessage` union 加入 `ToolResultMessage`

Step 15 新增：
- `SummaryMessage`：compaction 生成的上下文摘要消息
- `Message` / `AgentMessage` union 加入 `SummaryMessage`
- `convert_to_llm` 把 SummaryMessage → LLMUserMessage("[Conversation Summary]\n...")

P0-3 新增：
- `FileBlock`：UserMessage.content 里的文件元信息块（不发全文）
- `UserMessage.content` 扩展为 `list[TextContent | FileBlock]`，靠 `type` 判别
- `convert_to_llm` 把 FileBlock 转成文本说明，提示 LLM 调用 view_file
- 不引入 ImageBlock；图片统一以 FileBlock(format="image_unsupported") 注入

Step 5 起，run_event_loop 接入单工具执行：模型返回 ToolCall → registry 查找 →
执行 → 包成 ToolResultMessage → 加入上下文 → 再次调用 LLM。
"""
from __future__ import annotations

import time
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

# ============================================================================
# 内容块
# ============================================================================


class TextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingContent(BaseModel):
    """Assistant reasoning content preserved across events and turns.

    ``thinking_signature`` carries provider metadata needed to replay signed
    thinking blocks. For redacted Anthropic blocks it stores the opaque
    encrypted payload while ``thinking`` remains empty.
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False


#: FileBlock.format 取值——附件类型分类（不区分 provider，仅给 LLM 提示）
#:
#: - markdown / html / csv / parquet / text：view_file 可读
#: - pdf：本轮不解析（元信息 + 提示）
#: - image_unsupported：明确不支持图片理解（不做 OCR / 不做 GLM-4V 直读）
#: - binary：未知二进制（仅元信息）
#: - unsupported：兜底（其它未知格式）
FileFormat = Literal[
    "markdown", "html", "csv", "parquet", "text",
    "pdf", "image_unsupported", "binary", "unsupported",
]


class FileBlock(BaseModel):
    """UserMessage.content 中的文件元信息块（P0-3）。

    设计原则：
    - **只放元信息**——不发全文 / 不发 path / 不发 base64
    - LLM 需要内容时通过 `view_file(file_id=...)` 主动取
    - 大文件由 view_file 负责 max_bytes / max_rows 截断
    - 图片统一以 format="image_unsupported" 注入；本轮不做图片理解

    字段：
      file_id   VirtualFileStore 里的 file id（view_file 用它取内容）
      name      原始文件名（已 sanitize）
      mime      MIME 类型
      size      字节数
      sha256    内容 sha256（用于稳定性校验 / UI 显示）
      format    分类提示（见 FileFormat union）
      summary   可选——简短描述（本轮不强制，预留）
    """

    type: Literal["file"] = "file"
    file_id: str
    name: str
    mime: str
    size: int
    sha256: str
    format: FileFormat = "unsupported"
    summary: str | None = None


class ToolCall(BaseModel):
    """AssistantMessage.content 里的工具调用块。

    字段：
      id         —— Provider 给的调用 ID（与 ToolResult.tool_call_id 对应）
      name       —— 工具名（必须在 ToolRegistry 中）
      arguments  —— 参数 dict（已 JSON 解析过；默认 {}）
      raw        —— Provider 原始 tool_use 数据（dict | None），便于调试
    """
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None


AssistantContent = Union[TextContent, ThinkingContent, ToolCall]  # noqa: UP007

#: P0-3：UserMessage.content 现可包含文件元信息块 FileBlock。
#: 通过 `type` 字段判别（"text" / "file"）——Pydantic 自动 union 解析。
#: 旧 str 入参已被 TextContent 替代；这里保持 list 入参契约不破坏。
UserContent = Union[TextContent, FileBlock]  # noqa: UP007


class Usage(BaseModel):
    input: int = 0
    output: int = 0
    total_tokens: int = 0


class GenerationMetrics(BaseModel):
    """Provider timing for one real LLM call, excluding tool execution."""

    latency_ms: int | None = None
    time_to_first_token_ms: int | None = None
    usage_available: bool = False


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================================
# 标准消息
# ============================================================================


class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    # P0-3：UserMessage.content 现可含 FileBlock（按 type 判别）。
    # 旧 [TextContent] 入参仍兼容——TextContent / FileBlock 都是 Pydantic BaseModel。
    content: list[UserContent] = Field(default_factory=list)
    timestamp: int = Field(default_factory=_now_ms)


class AssistantMessage(BaseModel):
    """content 可含 TextContent + ThinkingContent + ToolCall。"""
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list)
    api: str
    provider: str
    model: str
    stop_reason: str = "stop"
    error_message: str | None = None
    usage: Usage = Field(default_factory=Usage)
    generation_metrics: GenerationMetrics | None = None
    timestamp: int = Field(default_factory=_now_ms)


class ToolResultMessage(BaseModel):
    """工具执行结果消息（Step 5 新增）。

    对应一次 ToolCall：tool_call_id 必须等于对应 ToolCall.id；
    name 必须等于 ToolCall.name。

    Step 5 起 run_event_loop 把工具执行结果包成此类型加回 messages，
    通过 convert_to_llm 转成 LLMToolResultMessage 进入下一轮 LLM 调用。

    字段：
      tool_call_id  —— 对应 ToolCall.id
      name          —— 工具名（便于 UI / 调试）
      content       —— 发给 LLM 的文本结果
      is_error      —— 工具是否失败
      terminate     —— 是否停止自动下一轮 LLM 调用
      details       —— 结构化数据，仅给 UI / observability；不发给 LLM
      timestamp     —— 毫秒
    """
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    name: str
    content: list[TextContent] = Field(default_factory=list)
    is_error: bool = False
    terminate: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: int = Field(default_factory=_now_ms)


# ============================================================================
# SummaryMessage（Step 15 新增）
# ============================================================================


#: SummaryMessage 的类型——区分 context compaction 与 branch summary。
#: - "context_compaction"：替换历史 messages 的上下文摘要
#: - "branch_summary"     ：分支级旁路摘要（一般不入 Agent 上下文）
SummaryType = Literal["context_compaction", "branch_summary"]


class SummaryMessage(BaseModel):
    """Compaction 生成的上下文摘要消息（Step 15）。

    字段：
      role                  固定 "summary"
      summary_type          context_compaction / branch_summary
      content               摘要正文（TextContent 列表）
      source_message_count  摘要覆盖的原始 message 数
      source_snapshot_ids   摘要来源 snapshot id 列表
      source_turn_count     摘要覆盖的 turn 数
      created_at            毫秒时间戳
      metadata              策略 / 保留条数 / 估算 token 等

    通过 convert_to_llm 转成 LLMUserMessage——让 LLM 看到摘要内容。
    """
    role: Literal["summary"] = "summary"
    summary_type: SummaryType = "context_compaction"
    content: list[TextContent] = Field(default_factory=list)
    source_message_count: int = 0
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_turn_count: int = 0
    created_at: int = Field(default_factory=_now_ms)
    metadata: dict[str, Any] = Field(default_factory=dict)


# 事件字段 / agent_end.messages 用这个（Step 5：含 ToolResultMessage；Step 15：加 SummaryMessage）
Message = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage | SummaryMessage,
    Field(discriminator="role"),
]


# ============================================================================
# 自定义消息（Step 3 起）
# ============================================================================


class CustomMessage(BaseModel):
    """Agent 内部自定义消息，默认不发给 LLM。"""
    role: Literal["custom"] = "custom"
    custom_type: str
    content: str
    display: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: int = Field(default_factory=_now_ms)


# ============================================================================
# AgentMessage union（Step 5：含 ToolResultMessage；Step 15：加 SummaryMessage）
# ============================================================================


AgentMessage = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage | CustomMessage | SummaryMessage,
    Field(discriminator="role"),
]


__all__ = [
    "TextContent", "ThinkingContent", "ToolCall", "AssistantContent",
    # P0-3
    "FileFormat", "FileBlock", "UserContent",
    "Usage", "GenerationMetrics",
    "UserMessage", "AssistantMessage", "ToolResultMessage", "Message",
    "CustomMessage", "AgentMessage",
    # Step 15
    "SummaryType", "SummaryMessage",
]
