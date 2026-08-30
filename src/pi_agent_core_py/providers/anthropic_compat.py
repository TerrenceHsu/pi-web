"""Anthropic-compatible ProviderAdapter（Step 21）。

把原本 `GLMClient._adapt_stream` + `_to_anthropic_messages` + `_to_anthropic_tool`
的逻辑迁出来，形成可复用的 Anthropic-compatible adapter。GLM / Claude（via 自部署
网关）/ 其它 Anthropic 协议兼容服务都可以基于它。

职责：
- `LLMMessage` → Anthropic `messages` 参数（user / assistant / tool_result）
- `ToolDef` → Anthropic `tools` 参数（name / description / input_schema）
- Anthropic SDK raw stream event → 内部 StreamEvent
- usage → 内部 Usage（input / output / total_tokens）
- stop_reason → 内部 stop / length / tool_use

不做：
- 工具执行（Agent loop 负责）
- 权限策略（policy 子包负责）
- 重试 / fallback（上层负责）
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncIterator
from typing import Any, Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from ..llm_messages import (
    LLMAssistantMessage,
    LLMMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from ..messages import TextContent, ThinkingContent, ToolCall, Usage
from ..stream_events import (
    DoneEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from ..tools import ToolDef
from .base import ProviderAdapter, ProviderRequest
from .errors import ProviderConfigError, ProviderProtocolError

# ============================================================================
# Config
# ============================================================================


class AnthropicCompatConfig(BaseModel):
    """Anthropic-compatible adapter 配置。

    字段说明：
      api_key   鉴权 token（Anthropic 是 sk-ant-...，GLM 是 32 位 hex）
      base_url  网关地址；None 表示用 SDK 默认（https://api.anthropic.com）
      model     模型名（必填）
      timeout_s / max_tokens 透传给 SDK
      temperature  None = 用 SDK 默认；float（含 0.0）= 显式传给 SDK
    """

    api_key: str
    base_url: str | None = None
    model: str
    timeout_s: float = 60.0
    max_tokens: int = 4096
    temperature: float | None = 0.0
    extra_headers: dict[str, str] = Field(default_factory=dict)


# ============================================================================
# Message / Tool 转换
# ============================================================================


def to_anthropic_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """LLMMessage → Anthropic SDK 的 messages 参数。

    - LLMUserMessage → user role + text block
    - LLMAssistantMessage → assistant role + [text block?, tool_use block × N]
      Step 21 修复：tool_use block 必须出现在 assistant message 里，否则
      后续 user 的 tool_result block 找不到配对——Anthropic 协议要求严格配对。
    - LLMToolResultMessage → 合并到最近一个 user message 的 tool_result block；
      若前面没有 user，则新建一个

    不抛错——遇到不认识的子类直接跳过（理论上 union 已穷举）。
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, LLMUserMessage):
            text = "".join(c.text for c in m.content)
            out.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif isinstance(m, LLMAssistantMessage):
            blocks: list[dict[str, Any]] = []
            for content in m.content:
                if isinstance(content, TextContent):
                    if content.text:
                        blocks.append({"type": "text", "text": content.text})
                elif isinstance(content, ThinkingContent):
                    if content.redacted:
                        if content.thinking_signature:
                            blocks.append(
                                {
                                    "type": "redacted_thinking",
                                    "data": content.thinking_signature,
                                }
                            )
                    elif content.thinking_signature:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": content.thinking,
                                "signature": content.thinking_signature,
                            }
                        )
                    elif content.thinking:
                        # Anthropic requires a signature on replayed thinking blocks.
                        # Preserve unsigned reasoning as ordinary assistant text.
                        blocks.append({"type": "text", "text": content.thinking})
            # Step 21 修复：保留 ToolCall → tool_use block
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments or {},
                    }
                )
            if not blocks:
                # Anthropic 拒绝空 content；保底塞一个空 text
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
        elif isinstance(m, LLMToolResultMessage):
            text = "".join(c.text for c in m.content)
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": text,
                "is_error": m.is_error,
            }
            # 合并到上一个 user message，否则新建
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def to_anthropic_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    """ToolDef → Anthropic tools 参数。

    空 tools 返回 []（调用方决定是否把 [] 透传给 SDK）。
    parameters 缺省时填 `{"type": "object", "properties": {}}`。
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters or {"type": "object", "properties": {}},
        }
        for t in tools
    ]


# ============================================================================
# Adapter
# ============================================================================


class AnthropicCompatAdapter(ProviderAdapter):
    """Anthropic-compatible 协议的 ProviderAdapter。

    子类可以覆盖 `provider_id` 来区分具体厂商（GLM / Claude via 自部署网关 / ...）。
    """

    provider_id: str = "anthropic_compat"

    def __init__(
        self,
        config: AnthropicCompatConfig,
        *,
        client: AsyncAnthropic | None = None,
    ) -> None:
        if not config.api_key:
            raise ProviderConfigError(
                "AnthropicCompatConfig.api_key 不能为空——请在 init 阶段校验",
            )
        self.config = config
        self.model = config.model
        # 允许测试注入 mock client
        self._client = client or AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_s,
            default_headers=config.extra_headers or None,
        )
        self._closed: bool = False

    async def aclose(self) -> None:
        """关闭底层 AsyncAnthropic 客户端（释放 httpx 连接池）。

        幂等：多次调用只关闭一次，不抛错。FakeProviderAdapter 不持有
        外部资源，无需覆盖。
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        except Exception:
            # 关闭失败不应让上层崩溃——记录后吞掉
            pass

    async def stream(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[StreamEvent]:
        """调 Anthropic SDK messages.stream，把 raw event 转成 StreamEvent。

        错误处理：协议错误直接抛 ProviderProtocolError（被 ModelClient 捕获后
        转 ErrorEvent）；网络 / SDK 异常也向上抛——ModelClient 统一兜底。
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "system": request.system_prompt,
            "messages": to_anthropic_messages(request.messages),
            "max_tokens": self.config.max_tokens,
        }
        if request.tools:
            kwargs["tools"] = to_anthropic_tools(request.tools)
            if request.metadata.get("pi_agent_tool_choice") == "required":
                kwargs["tool_choice"] = {"type": "any"}
        if request.metadata.get("pi_agent_thinking") == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        if self.config.temperature is not None:
            # 显式传 temperature（含 0.0）——避免 SDK 默认（多数 provider 是 1.0）行为漂移。
            # None = 不传，让 SDK 用其默认值。
            kwargs["temperature"] = self.config.temperature

        async with self._client.messages.stream(**kwargs) as stream:
            async for ev in self._adapt_stream(stream, request.signal):
                yield ev

    async def _adapt_stream(
        self,
        stream: Any,
        signal: asyncio.Event | None,
    ) -> AsyncIterator[StreamEvent]:
        """Anthropic SDK 事件 → pi content-block 生命周期事件流。

        解析：
        - text → TextStartEvent / TextDeltaEvent / TextEndEvent
        - thinking / redacted_thinking → thinking start/delta/end
        - tool_use block（content_block_start + input_json_delta + content_block_stop）
          → toolcall start/delta/end；只有 end 携带可执行 ToolCall
        - message_delta(stop_reason) → 决定 DoneEvent.stop_reason
        - 末态 message.usage → DoneEvent.usage
        """
        final_stop: Literal["stop", "length", "tool_use"] = "stop"
        final_usage = Usage()
        content_blocks: dict[int, dict[str, Any]] = {}

        async for event in stream:
            if signal is not None and signal.is_set():
                yield DoneEvent(stop_reason="aborted", usage=final_usage)
                return

            t = event.type
            if t == "content_block_start":
                cb = event.content_block
                block_type = getattr(cb, "type", None)
                index = event.index
                if block_type == "text":
                    initial_text = getattr(cb, "text", "") or ""
                    content_blocks[index] = {
                        "type": "text",
                        "parts": [initial_text] if initial_text else [],
                    }
                    yield TextStartEvent(content_index=index)
                    if initial_text:
                        yield TextDeltaEvent(
                            content_index=index,
                            delta=initial_text,
                        )
                elif block_type == "tool_use":
                    content_blocks[index] = {
                        "type": "tool_use",
                        "id": cb.id,
                        "name": cb.name,
                        "json_parts": [],
                    }
                    yield ToolCallStartEvent(
                        content_index=index,
                        tool_call_id=cb.id,
                        name=cb.name,
                    )
                elif block_type == "thinking":
                    thinking = getattr(cb, "thinking", "") or ""
                    signature = getattr(cb, "signature", None) or None
                    content_blocks[index] = {
                        "type": "thinking",
                        "parts": [thinking] if thinking else [],
                        "signature": signature,
                        "redacted": False,
                    }
                    yield ThinkingStartEvent(
                        content_index=index,
                        thinking_signature=signature,
                    )
                    if thinking:
                        yield ThinkingDeltaEvent(
                            content_index=index,
                            delta=thinking,
                            thinking_signature=signature,
                        )
                elif block_type == "redacted_thinking":
                    data = getattr(cb, "data", None)
                    signature = data if isinstance(data, str) and data else None
                    content_blocks[index] = {
                        "type": "thinking",
                        "parts": [],
                        "signature": signature,
                        "redacted": True,
                    }
                    yield ThinkingStartEvent(
                        content_index=index,
                        thinking_signature=signature,
                        redacted=True,
                    )
                    # Preserve the former observable payload event while still
                    # representing redacted blocks as a full lifecycle.
                    yield ThinkingDeltaEvent(
                        content_index=index,
                        delta="",
                        thinking_signature=signature,
                        redacted=True,
                    )

            elif t == "content_block_delta":
                delta = event.delta
                if delta.type == "text_delta":
                    block = content_blocks.get(event.index)
                    if block is None:
                        block = {"type": "text", "parts": []}
                        content_blocks[event.index] = block
                        yield TextStartEvent(content_index=event.index)
                    if block.get("type") == "text":
                        block["parts"].append(delta.text)
                    yield TextDeltaEvent(
                        content_index=event.index,
                        delta=delta.text,
                    )
                elif delta.type == "thinking_delta":
                    block = content_blocks.get(event.index)
                    if block is None:
                        block = {
                            "type": "thinking",
                            "parts": [],
                            "signature": None,
                            "redacted": False,
                        }
                        content_blocks[event.index] = block
                        yield ThinkingStartEvent(content_index=event.index)
                    if block.get("type") == "thinking":
                        block["parts"].append(delta.thinking)
                    yield ThinkingDeltaEvent(
                        content_index=event.index,
                        delta=delta.thinking,
                    )
                elif delta.type == "signature_delta":
                    block = content_blocks.get(event.index)
                    if block is None:
                        block = {
                            "type": "thinking",
                            "parts": [],
                            "signature": None,
                            "redacted": False,
                        }
                        content_blocks[event.index] = block
                        yield ThinkingStartEvent(content_index=event.index)
                    if block.get("type") == "thinking":
                        block["signature"] = delta.signature
                    yield ThinkingDeltaEvent(
                        content_index=event.index,
                        delta="",
                        thinking_signature=delta.signature,
                    )
                elif delta.type == "input_json_delta":
                    block = content_blocks.get(event.index)
                    if block is not None and block.get("type") == "tool_use":
                        block["json_parts"].append(delta.partial_json)
                        yield ToolCallDeltaEvent(
                            content_index=event.index,
                            delta=delta.partial_json,
                            tool_call_id=block["id"],
                            name=block["name"],
                        )

            elif t == "content_block_stop":
                block = content_blocks.pop(event.index, None)
                if block is None:
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    yield TextEndEvent(
                        content_index=event.index,
                        content="".join(block["parts"]),
                    )
                elif block_type == "thinking":
                    yield ThinkingEndEvent(
                        content_index=event.index,
                        content="".join(block["parts"]),
                        thinking_signature=block["signature"],
                        redacted=block["redacted"],
                    )
                elif block_type == "tool_use":
                    raw_json = "".join(block["json_parts"])
                    try:
                        args = _json.loads(raw_json) if raw_json else {}
                    except Exception as e:
                        raise ProviderProtocolError(
                            f"tool_use input JSON parse 失败：{e}",
                        ) from e
                    if not isinstance(args, dict):
                        raise ProviderProtocolError(
                            f"tool_use input 必须是 JSON object，实际：{type(args).__name__}",
                        )
                    yield ToolCallEndEvent(
                        content_index=event.index,
                        tool_call=ToolCall(
                            id=block["id"],
                            name=block["name"],
                            arguments=args,
                            raw={
                                "id": block["id"],
                                "name": block["name"],
                                "input": args,
                            },
                        ),
                    )

            elif t == "message_delta":
                sr = event.delta.stop_reason
                if sr == "tool_use":
                    final_stop = "tool_use"
                elif sr == "max_tokens":
                    final_stop = "length"

        # Some compatible gateways omit content_block_stop.  Close complete
        # text/thinking blocks here so consumers still receive balanced events.
        for index, block in sorted(content_blocks.items()):
            block_type = block.get("type")
            if block_type == "text":
                yield TextEndEvent(
                    content_index=index,
                    content="".join(block["parts"]),
                )
            elif block_type == "thinking":
                yield ThinkingEndEvent(
                    content_index=index,
                    content="".join(block["parts"]),
                    thinking_signature=block["signature"],
                    redacted=block["redacted"],
                )
            elif block_type == "tool_use":
                raise ProviderProtocolError(
                    "provider ended with incomplete tool call",
                ) from None

        try:
            final_msg = await stream.get_final_message()
            u = final_msg.usage
            final_usage = Usage(
                input=getattr(u, "input_tokens", 0) or 0,
                output=getattr(u, "output_tokens", 0) or 0,
                total_tokens=(getattr(u, "input_tokens", 0) or 0)
                + (getattr(u, "output_tokens", 0) or 0),
            )
        except Exception:
            pass

        yield DoneEvent(stop_reason=final_stop, usage=final_usage)


__all__ = [
    "AnthropicCompatConfig",
    "AnthropicCompatAdapter",
    "to_anthropic_messages",
    "to_anthropic_tools",
]
