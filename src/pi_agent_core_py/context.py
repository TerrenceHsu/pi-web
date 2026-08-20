"""Context 转换——AgentMessage → LLMMessage 的边界。

两层钩子：
- `transform_context(messages)`：AgentMessage 层；应用层可覆盖做压缩、注入等
- `convert_to_llm(messages)`：AgentMessage → LLMMessage；过滤 CustomMessage

调用顺序（在 run_event_loop 中）：
    raw_context: list[AgentMessage]
        ↓ transform_context
    transformed: list[AgentMessage]
        ↓ convert_to_llm
    llm_messages: list[LLMMessage]
        ↓
    ModelClient.stream(messages=llm_messages)

P0-3：UserMessage 中的 FileBlock 不直接发给 provider——转换成一段文本说明，
提示 LLM 可通过 `view_file(file_id=...)` 主动取内容。这样：
- 不暴露 path
- 不把大文件全文塞进 message
- 不发 image block（图片以 image_unsupported 描述，明确不支持理解）
- provider 无关（GLM / Anthropic / OpenAI / Fake 都用同一段文本）
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from .llm_messages import (
    LLMAssistantMessage,
    LLMMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from .messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    FileBlock,
    SummaryMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

# 钩子类型（便于 Step 15 等覆盖）
TransformContextFn = Callable[
    [list[AgentMessage]],
    Awaitable[list[AgentMessage]],
]


SUMMARY_CONTEXT_PREFIX = (
    "The conversation history before this point was compacted into the "
    "following summary:\n\n<summary>\n\n"
)
SUMMARY_CONTEXT_SUFFIX = "\n\n</summary>"


async def transform_context(messages: list[AgentMessage]) -> list[AgentMessage]:
    """默认上下文转换：原样返回。

    应用层可覆盖此函数做：
      - 上下文裁剪（移除太旧的消息）
      - 压缩历史（Step 15）
      - 注入外部上下文（RAG）
      - 过滤 UI-only message
    """
    return messages


def convert_to_llm(messages: list[AgentMessage]) -> list[LLMMessage]:
    """AgentMessage[] → LLMMessage[] 的边界转换。

    规则（Step 5）：
      - UserMessage       → LLMUserMessage
      - AssistantMessage  → LLMAssistantMessage
        Step 21 修复：把 content 里的 ToolCall 块保留到 LLMAssistantMessage.tool_calls，
        让 provider 适配器能重建 tool_use block（Anthropic 协议要求
        tool_result 必须配对前一条 assistant 的 tool_use，否则 multi-turn
        tool 调用会失败）。
      - ToolResultMessage → LLMToolResultMessage（不带 terminate / details；保留
        工具自身 usage 与 deferred-tool load-point metadata）
      - CustomMessage     → 默认过滤掉（不发给 LLM）

    Step 15：
      - SummaryMessage    → LLMUserMessage（pi-compatible `<summary>` envelope）
        让 LLM 看到摘要，但不引入新的 LLM role——保持 LLMMessage 模型不变。

    本函数是同步的——纯映射，无 I/O。
    """
    out: list[LLMMessage] = []
    for m in messages:
        if isinstance(m, UserMessage):
            # P0-3：UserMessage.content 现含 FileBlock；统一转成 TextContent
            # FileBlock → 一段说明文本（不发全文 / 不发 path / 不区分 provider）
            new_content: list[TextContent] = []
            for block in m.content:
                if isinstance(block, FileBlock):
                    new_content.append(TextContent(
                        text=_render_file_block_to_text(block),
                    ))
                else:
                    # TextContent 透传（已是 TextContent 实例）
                    new_content.append(block)
            out.append(LLMUserMessage(
                content=new_content, timestamp=m.timestamp,
            ))
        elif isinstance(m, AssistantMessage):
            assistant_content = [
                c for c in m.content if isinstance(c, (TextContent, ThinkingContent))
            ]
            tool_calls = [c for c in m.content if isinstance(c, ToolCall)]
            out.append(LLMAssistantMessage(
                content=assistant_content,
                tool_calls=tool_calls,
                api=m.api, provider=m.provider, model=m.model,
                stop_reason=m.stop_reason, usage=m.usage,
                timestamp=m.timestamp,
            ))
        elif isinstance(m, ToolResultMessage):
            # Step 5：进入下一轮 LLM 上下文
            out.append(LLMToolResultMessage(
                tool_call_id=m.tool_call_id,
                name=m.name,
                content=list(m.content),
                is_error=m.is_error,
                usage=m.usage,
                added_tool_names=list(m.added_tool_names),
                timestamp=m.timestamp,
            ))
        elif isinstance(m, SummaryMessage):
            # 摘要作为带稳定语义前缀的 user context 注入。正文仍单独持久化，
            # 避免多次 compaction 时把展示前缀重复卷入摘要。
            # 不引入新 LLM role；保持 LLMMessage 模型不变
            text = "\n".join(c.text for c in m.content)
            out.append(LLMUserMessage(
                content=[TextContent(
                    text=f"{SUMMARY_CONTEXT_PREFIX}{text}{SUMMARY_CONTEXT_SUFFIX}"
                )],
                timestamp=m.created_at,
            ))
        elif isinstance(m, CustomMessage):
            # 默认过滤：CustomMessage 不发给 LLM
            continue
    return out


# ============================================================================
# P0-3：FileBlock → 文本说明（provider 无关）
# ============================================================================


def _format_bytes(n: int) -> str:
    """人类可读的字节数（用于 FileBlock 文本说明）。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _render_file_block_to_text(block: FileBlock) -> str:
    """把 FileBlock 转成给 LLM 看的说明文本。

    设计：
    - 列出元信息（file_id / name / mime / size / sha256 / format）
    - 引导 LLM 调用 view_file(file_id=...) 读取可读格式
    - 图片 / pdf / 二进制给明确说明（本轮不支持理解）
    - 不发 path / 不发 base64 / 不发全文

    返回多行字符串（带前缀 [Attached File]，便于 LLM 识别边界）。
    """
    header = (
        f"[Attached File]\n"
        f"- file_id: {block.file_id}\n"
        f"- name: {block.name}\n"
        f"- mime: {block.mime}\n"
        f"- size: {block.size} bytes ({_format_bytes(block.size)})\n"
        f"- sha256: {block.sha256}\n"
        f"- format: {block.format}"
    )

    if block.format in ("markdown", "html", "csv", "parquet", "text"):
        hint = (
            f"\n如需读取该文件内容，请调用 view_file(file_id=\"{block.file_id}\")。"
            f"\n支持读取的格式：markdown / html / csv / parquet / 常见文本文件。"
        )
    elif block.format == "image_unsupported":
        hint = (
            "\n当前不支持图片内容分析（不做 OCR、不做视觉理解）。"
            "如用户问题依赖图片内容，请明确告知暂不支持图片。"
        )
    elif block.format == "pdf":
        hint = "\nPDF 内容本轮暂未解析；如需提取，请让用户提供文本版本。"
    elif block.format == "binary":
        hint = "\n该文件为二进制格式，本轮无法直接读取其内容。"
    else:  # unsupported / unknown
        hint = (
            "\n该文件格式本轮不在 view_file 直接支持列表内；"
            "可尝试 view_file 看是否可读，或请用户提供文本版本。"
        )

    return header + hint


__all__ = [
    "TransformContextFn",
    "SUMMARY_CONTEXT_PREFIX",
    "SUMMARY_CONTEXT_SUFFIX",
    "transform_context",
    "convert_to_llm",
]
