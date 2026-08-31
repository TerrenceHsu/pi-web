"""OpenAI-compatible ProviderAdapter（P1-E M1-1）.

让 Qwen / Kimi（以及未来其它 OpenAI-compatible 服务）共用同一 Adapter——差异
仅由 ProviderDefinition.id 与 default_base_url 表达。ProviderFactory 根据定义
决定是否实例化本类。

契约要点（详见 docs/design/p1-e-m1-provider-runtime.md §5）：

- 继承 ProviderAdapter；provider_id 由构造参数传入（不在 Config 中）
- `aclose()` 幂等，最多实际关闭一次
- `stream()` 用 `async with client.chat.completions.stream(...)`——SDK 保证
  退出时关闭底层 response，无需手动 close raw stream
- usage 优先（先读 chunk.usage 再看 choices）
- ToolCall buffer 按 index 升序 flush；finish_reason="tool_calls" 或正常流结束
  时统一 flush
- reasoning_content / reasoning / reasoning_text 转成 ThinkingDeltaEvent
- 错误用固定短文本（不含 str(exc) / repr(exc) / response.text / body），
  所有映射 `from None` 中断 cause 链
- asyncio.CancelledError 原样传播；不映射为 ProviderError
- 内部 AsyncOpenAI client 固定 max_retries=0——保证「一次 Agent 请求对应一次
  Provider 请求」的确定性
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..llm_messages import (
    LLMAssistantMessage,
    LLMMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from ..messages import ImageContent, TextContent, ThinkingContent, ToolCall, Usage
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
from .errors import (
    ProviderAuthenticationError,
    ProviderConfigError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderStreamError,
)

# ============================================================================
# URL validators
# ============================================================================


def _reject_userinfo(url: str, label: str) -> None:
    """Reject URLs containing userinfo (user:pass@)——would leak credentials."""
    if "://" not in url:
        raise ValueError(f"{label} must include scheme")
    after_scheme = url.split("://", 1)[1]
    if "@" in after_scheme.split("/", 1)[0]:
        raise ValueError(f"{label} must not contain userinfo (user:pass@)")


def _validate_http_url(url: str, label: str) -> None:
    """Ensure URL is http/https, has host, and does not contain userinfo."""
    if not url or not url.strip():
        raise ValueError(f"{label} must be non-empty")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{label} must be http or https (got {parsed.scheme!r})")
    if not parsed.netloc:
        raise ValueError(f"{label} must have a host")
    _reject_userinfo(url, label)


def _reject_control_chars(value: str, label: str) -> None:
    """Reject ASCII control chars / NUL / CR / LF in short string fields."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} must not contain control characters")


# ============================================================================
# Config
# ============================================================================


class OpenAICompatConfig(BaseModel):
    """OpenAI-compatible Adapter 配置（修订 D）.

    字段说明：
      api_key     SecretStr——Pydantic 序列化 / repr 时不暴露明文
      base_url    http/https；禁 userinfo
      model       trim 后非空，拒绝控制字符
      timeout_s   > 0
      max_tokens  None = 不传给 SDK；正整数 = 透传
      temperature None = 不传给 SDK；float = 透传

    不含：
      provider_id（由 Adapter 构造参数传入）
      extra_headers（M1 不暴露给用户）
      Authorization / organization / project / 自定义 query / 远程模型列表
    """

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(repr=False)
    base_url: str
    model: str
    timeout_s: float = 60.0
    max_tokens: int | None = 4096
    temperature: float | None = None

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value() if v else ""
        if not raw or not raw.strip():
            raise ValueError("api_key must be non-empty")
        return v

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        _validate_http_url(v, "base_url")
        return v

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model must be non-empty")
        _reject_control_chars(v, "model")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _validate_timeout(cls, v: float) -> float:
        if not (v > 0):
            raise ValueError("timeout_s must be positive")
        return v

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("max_tokens must be positive or None")
        return v

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, v: float | None) -> float | None:
        if v is None:
            return v
        # 拒绝 NaN / inf
        import math

        if math.isnan(v) or math.isinf(v):
            raise ValueError("temperature must be finite or None")
        return v


# ============================================================================
# LLMMessage → OpenAI message 转换（修订 H）
# ============================================================================


def _join_text(contents: Sequence[TextContent | ThinkingContent | ImageContent]) -> str:
    return "".join(c.text for c in contents if isinstance(c, TextContent))


def _join_thinking(contents: Sequence[TextContent | ThinkingContent]) -> tuple[str, str | None]:
    blocks = [c for c in contents if isinstance(c, ThinkingContent) and not c.redacted]
    thinking = "".join(c.thinking for c in blocks)
    signature = next((c.thinking_signature for c in blocks if c.thinking_signature), None)
    return thinking, signature


def to_openai_messages(
    *,
    system_prompt: str,
    messages: Sequence[LLMMessage],
) -> list[dict[str, Any]]:
    """LLMMessage → OpenAI Chat Completions messages.

    转换表（修订 H）：
      - system_prompt（非空）     → messages[0] = {role: "system", content: ...}
      - LLMUserMessage            → {role: "user", content: <joined text>}
      - LLMAssistantMessage text  → {role: "assistant", content: <text>}
      - LLMAssistantMessage w/ tc → {role: "assistant", content: <text or None>,
                                     tool_calls: [{id, type: "function",
                                                   function: {name, arguments}}]}
      - LLMToolResultMessage      → {role: "tool", tool_call_id: <id>, content: <text>}

    约束：
      - 保留 tool-call ID / tool name
      - tool arguments 序列化为紧凑 JSON string
      - tool result 必须关联原 tool_call_id
      - 空 system prompt 不插入 system message
      - 不支持的 block 类型抛固定 ProviderProtocolError（错误信息不含正文）
      - 不修改原始 messages 对象
      - ThinkingContent 按原 provider 字段名回放；未知签名回退 reasoning_content
    """
    out: list[dict[str, Any]] = []

    # system 必须是 messages[0]
    if system_prompt and system_prompt.strip():
        out.append({"role": "system", "content": system_prompt})

    pending_tool_images: list[ImageContent] = []

    def flush_tool_images() -> None:
        if not pending_tool_images:
            return
        out.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image.mime_type};base64,{image.data}",
                    },
                }
                for image in pending_tool_images
            ],
        })
        pending_tool_images.clear()

    for m in messages:
        if not isinstance(m, LLMToolResultMessage):
            flush_tool_images()
        if isinstance(m, LLMUserMessage):
            text = _join_text(m.content)
            images = [item for item in m.content if isinstance(item, ImageContent)]
            if images:
                content: list[dict[str, Any]] = []
                if text:
                    content.append({"type": "text", "text": text})
                content.extend(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.mime_type};base64,{image.data}",
                        },
                    }
                    for image in images
                )
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": text})
        elif isinstance(m, LLMAssistantMessage):
            text = _join_text(m.content)
            thinking, thinking_signature = _join_thinking(m.content)
            assistant_payload: dict[str, Any]
            if m.tool_calls:
                tool_calls_payload: list[dict[str, Any]] = []
                for tc in m.tool_calls:
                    if not tc.id:
                        raise ProviderProtocolError(
                            "assistant tool_call missing id",
                        ) from None
                    if not tc.name:
                        raise ProviderProtocolError(
                            "assistant tool_call missing name",
                        ) from None
                    tool_calls_payload.append(
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": _json.dumps(tc.arguments or {}, separators=(",", ":")),
                            },
                        }
                    )
                # OpenAI 协议：assistant message w/ tool_calls 时 content 可为 None
                assistant_payload = {
                    "role": "assistant",
                    "content": text if text else None,
                    "tool_calls": tool_calls_payload,
                }
            else:
                assistant_payload = {"role": "assistant", "content": text}
            if thinking:
                reasoning_field = (
                    thinking_signature
                    if thinking_signature in {"reasoning_content", "reasoning", "reasoning_text"}
                    else "reasoning_content"
                )
                assistant_payload[reasoning_field] = thinking
            out.append(assistant_payload)
        elif isinstance(m, LLMToolResultMessage):
            if not m.tool_call_id:
                raise ProviderProtocolError(
                    "tool result missing tool_call_id",
                ) from None
            text = _join_text(m.content)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": text,
                }
            )
            pending_tool_images.extend(
                item for item in m.content if isinstance(item, ImageContent)
            )
        else:
            # 不支持的 message 类型——固定短文本（不含正文）
            raise ProviderProtocolError(
                "unsupported message type",
            ) from None

    flush_tool_images()
    return out


# ============================================================================
# ToolDef → OpenAI tool 转换
# ============================================================================


def to_openai_tools(tools: Sequence[ToolDef]) -> list[dict[str, Any]]:
    """ToolDef → OpenAI tools 参数.

    目标结构：
        {
          "type": "function",
          "function": {"name", "description", "parameters"}
        }

    约束：
      - 使用现有 ToolDef JSON Schema；不篡改 / 不加 strict=true
      - 空工具列表由 caller 决定是否省略字段（本函数仍返回 []）
      - 不发送旧版 functions 参数
      - 不支持的 ToolDef 抛固定 ProviderProtocolError
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        if not t.name:
            raise ProviderProtocolError("tool missing name") from None
        # parameters 缺省时填标准 empty schema
        params = t.parameters or {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": params,
                },
            }
        )
    return out


# ============================================================================
# ToolCall buffer
# ============================================================================


@dataclass
class _ToolCallBuffer:
    """按 tool_call.index 累积 partial JSON 的缓冲."""

    id: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list)
    content_index: int | None = None


# ============================================================================
# Stream parsing helpers
# ============================================================================


def _extract_usage(chunk: ChatCompletionChunk) -> Usage | None:
    """读 chunk.usage（OpenAI 末态 chunk 可能只含 usage）."""
    u = getattr(chunk, "usage", None)
    if u is None:
        return None
    prompt = getattr(u, "prompt_tokens", None) or 0
    completion = getattr(u, "completion_tokens", None) or 0
    total = getattr(u, "total_tokens", None)
    prompt_details = getattr(u, "prompt_tokens_details", None)
    completion_details = getattr(u, "completion_tokens_details", None)
    cache_read = (
        getattr(prompt_details, "cached_tokens", None)
        or getattr(u, "prompt_cache_hit_tokens", None)
        or getattr(u, "cached_tokens", 0)
        or 0
    )
    cache_write = (
        getattr(prompt_details, "cache_write_tokens", 0) or 0
    )
    raw_reasoning = getattr(completion_details, "reasoning_tokens", None)
    reasoning = int(raw_reasoning or 0) if raw_reasoning is not None else None
    uncached_input = max(0, prompt - cache_read - cache_write)
    if total is None or total == 0:
        total = uncached_input + completion + cache_read + cache_write
    return Usage(
        input=int(uncached_input),
        output=int(completion),
        cache_read=int(cache_read),
        cache_write=int(cache_write),
        reasoning=reasoning,
        total_tokens=int(total),
    )


def _merge_usage(current: Usage, new: Usage | None) -> Usage:
    """多个 usage chunk 合并：使用最新非空值；负数视为 0."""
    if new is None:
        return current
    return Usage(
        input=max(0, new.input),
        output=max(0, new.output),
        cache_read=max(0, new.cache_read),
        cache_write=max(0, new.cache_write),
        cache_write_1h=max(0, new.cache_write_1h),
        reasoning=(max(0, new.reasoning) if new.reasoning is not None else None),
        total_tokens=max(0, new.total_tokens),
    )


def _signal_set(signal: Any) -> bool:
    """安全检查 signal.is_set()——支持 asyncio.Event / threading.Event / 自定义."""
    if signal is None:
        return False
    try:
        return bool(signal.is_set())
    except Exception:
        return False


def _build_tool_call(buffer: _ToolCallBuffer) -> ToolCall:
    """从 buffer 构造 ToolCall——校验 id / name / arguments."""
    if not buffer.id:
        raise ProviderProtocolError("provider returned invalid tool call") from None
    if not buffer.name:
        raise ProviderProtocolError("provider returned invalid tool call") from None

    raw_json = "".join(buffer.argument_parts)
    try:
        args = _json.loads(raw_json) if raw_json else {}
    except Exception:
        raise ProviderProtocolError(
            "provider returned invalid tool call",
        ) from None

    if not isinstance(args, dict):
        # 不接受 array / scalar / 残缺 JSON
        raise ProviderProtocolError(
            "provider returned invalid tool call",
        ) from None

    return ToolCall(
        id=buffer.id,
        name=buffer.name,
        arguments=args,
        raw={
            "id": buffer.id,
            "name": buffer.name,
            "input": args,
        },
    )


# ============================================================================
# Error mapping（修订 G：固定短文本 + from None）
# ============================================================================


def _map_to_provider_error(exc: Exception) -> ProviderError:
    """SDK / 网络异常 → ProviderError 子类.

    严格约束：
      - 不使用 str(exc) / repr(exc) / response.text / request body
      - 所有 message 固定短文本
      - 全部 raise ... from None（中断 __cause__ 链）
    """
    # 局部 import 避免顶部硬依赖整个 openai 异常层级
    from openai import (
        APIConnectionError,
        APIError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        ConflictError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
        UnprocessableEntityError,
    )

    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return ProviderAuthenticationError("provider authentication failed")
    if isinstance(exc, RateLimitError):
        return ProviderRateLimitError("provider rate limit exceeded")
    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return ProviderStreamError("provider connection failed")
    if isinstance(exc, (BadRequestError, NotFoundError, ConflictError, UnprocessableEntityError)):
        return ProviderProtocolError("provider rejected request")
    if isinstance(exc, APIError):
        # 其它非认证 APIError——按 stream 错误兜底
        return ProviderStreamError("provider connection failed")
    return ProviderStreamError("provider connection failed")


# ============================================================================
# Adapter
# ============================================================================


class OpenAICompatibleProvider(ProviderAdapter):
    """OpenAI-compatible ProviderAdapter.

    Qwen / Kimi 共用——provider_id 由构造参数传入。
    """

    api_id = "openai-completions"

    def __init__(
        self,
        config: OpenAICompatConfig,
        *,
        provider_id: str,
        client: AsyncOpenAI | None = None,
        supports_images: bool = False,
    ) -> None:
        if not config.api_key.get_secret_value():
            raise ProviderConfigError("OpenAICompatConfig.api_key is empty")
        if not provider_id or not provider_id.strip():
            raise ProviderConfigError("provider_id must be non-empty")

        self.config = config
        # 实例属性覆盖类属性——区分 Qwen / Kimi
        self.provider_id = provider_id
        self.model = config.model
        self.supports_images = supports_images

        self._closed: bool = False

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            # max_retries=0：保证一次 Agent 请求对应一次 Provider 请求
            self._client = AsyncOpenAI(
                api_key=config.api_key.get_secret_value(),
                base_url=config.base_url,
                timeout=config.timeout_s,
                max_retries=0,
            )
            self._owns_client = True

    # ----------------------------------------------------------------------
    # stream()
    # ----------------------------------------------------------------------

    async def stream(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[StreamEvent]:
        """调 OpenAI Chat Completions stream helper，转成内部 StreamEvent.

        修订 E + F + I：
        - usage 优先（先读 chunk.usage）
        - choices 为空时 continue（usage-only chunk）
        - 不假设 choices[0] 永远存在
        - usage 缺失不是错误
        - text/thinking/tool-call 块均产生 start/delta/end
        - ToolCall buffers 按 content index 升序 flush
        - stream 正常结束时也 flush 已完成 tool calls
        - signal abort 后关闭 stream
        - asyncio.CancelledError 原样传播
        - reasoning_content / reasoning / reasoning_text 保留为 ThinkingDeltaEvent
        """
        if self._closed:
            raise ProviderConfigError("provider adapter is closed") from None

        # 调用前已 abort——不创建远程请求
        if _signal_set(request.signal):
            yield DoneEvent(stop_reason="aborted", usage=Usage())
            return

        kwargs = self._build_request_kwargs(request)

        final_stop: Literal["stop", "length", "tool_use"] = "stop"
        final_usage = Usage()
        tool_buffers: dict[int, _ToolCallBuffer] = {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        text_content_index: int | None = None
        thinking_content_index: int | None = None
        thinking_signature: str | None = None
        next_content_index = 0

        def allocate_content_index() -> int:
            nonlocal next_content_index
            index = next_content_index
            next_content_index += 1
            return index

        signal = request.signal
        stream_manager: Any = None
        try:
            # chat.completions.stream(...) 返回 AsyncChatCompletionStreamManager
            # ——必须用 async with 才能进入 stream 状态；SDK 保证退出时关闭 response
            stream_manager = self._client.chat.completions.stream(**kwargs)
            async with stream_manager as s:
                async for event in s:
                    # OpenAI SDK 的 stream helper 产生 ChatCompletionStreamEvent
                    # ——只处理 "chunk" 类型；其它（tool_choice / content_part 等）跳过
                    event_type = getattr(event, "type", None)
                    if event_type != "chunk":
                        continue
                    chunk = event.chunk

                    # 1. 先读 usage（usage-only chunk 时 choices 为空）
                    new_usage = _extract_usage(chunk)
                    if new_usage is not None:
                        final_usage = _merge_usage(final_usage, new_usage)

                    # 2. choices 为空时 continue
                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]
                    delta = choice.delta

                    # 3. signal 检查（协作式中止）——立刻 yield aborted 并退出
                    if _signal_set(signal):
                        yield DoneEvent(stop_reason="aborted", usage=final_usage)
                        return

                    # 4. text delta
                    content = getattr(delta, "content", None)
                    if content:
                        if text_content_index is None:
                            text_content_index = allocate_content_index()
                            yield TextStartEvent(content_index=text_content_index)
                        text_parts.append(content)
                        yield TextDeltaEvent(
                            content_index=text_content_index,
                            delta=content,
                        )

                    # 5. tool_call 增量累积并逐片发出；完整调用只在 end 可执行。
                    raw_tool_calls = getattr(delta, "tool_calls", None)
                    if raw_tool_calls:
                        for tc in raw_tool_calls:
                            idx = getattr(tc, "index", None)
                            if idx is None:
                                # 缺失 index——固定协议错误
                                raise ProviderProtocolError(
                                    "provider returned invalid tool call",
                                ) from None
                            buf = tool_buffers.setdefault(idx, _ToolCallBuffer())
                            tc_id = getattr(tc, "id", None)
                            if tc_id:
                                # 重复冲突的 id 检查
                                if buf.id and buf.id != tc_id:
                                    raise ProviderProtocolError(
                                        "provider returned invalid tool call",
                                    ) from None
                                buf.id = tc_id
                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                fn_name = getattr(fn, "name", None)
                                if fn_name:
                                    if buf.name and buf.name != fn_name:
                                        raise ProviderProtocolError(
                                            "provider returned invalid tool call",
                                        ) from None
                                    buf.name = fn_name
                                fn_args = getattr(fn, "arguments", None)
                                if fn_args:
                                    buf.argument_parts.append(fn_args)
                            if buf.content_index is None:
                                buf.content_index = allocate_content_index()
                                yield ToolCallStartEvent(
                                    content_index=buf.content_index,
                                    tool_call_id=buf.id,
                                    name=buf.name,
                                )
                            if fn is not None and fn_args:
                                yield ToolCallDeltaEvent(
                                    content_index=buf.content_index,
                                    delta=fn_args,
                                    tool_call_id=buf.id,
                                    name=buf.name,
                                )

                    # 6. reasoning 增量：取第一个非空字段，避免兼容端点重复返回。
                    for reasoning_field in (
                        "reasoning_content",
                        "reasoning",
                        "reasoning_text",
                    ):
                        reasoning_delta = getattr(delta, reasoning_field, None)
                        if isinstance(reasoning_delta, str) and reasoning_delta:
                            if thinking_content_index is None:
                                thinking_content_index = allocate_content_index()
                                thinking_signature = reasoning_field
                                yield ThinkingStartEvent(
                                    content_index=thinking_content_index,
                                    thinking_signature=thinking_signature,
                                )
                            thinking_parts.append(reasoning_delta)
                            yield ThinkingDeltaEvent(
                                content_index=thinking_content_index,
                                delta=reasoning_delta,
                                thinking_signature=reasoning_field,
                            )
                            break

                    # 7. finish_reason 决定最终 stop
                    fr = getattr(choice, "finish_reason", None)
                    if fr == "stop":
                        final_stop = "stop"
                    elif fr == "length":
                        final_stop = "length"
                    elif fr in ("tool_calls", "function_call"):
                        # function_call 是旧式——按项目既有 stop-reason 处理映射为 tool_use
                        final_stop = "tool_use"

                # /async for event

            # /async with stream_manager

            # 8. 正常结束：按内容块出现顺序发出 canonical end。
            end_events: list[tuple[int, StreamEvent]] = []
            if text_content_index is not None:
                end_events.append(
                    (
                        text_content_index,
                        TextEndEvent(
                            content_index=text_content_index,
                            content="".join(text_parts),
                        ),
                    )
                )
            if thinking_content_index is not None:
                end_events.append(
                    (
                        thinking_content_index,
                        ThinkingEndEvent(
                            content_index=thinking_content_index,
                            content="".join(thinking_parts),
                            thinking_signature=thinking_signature,
                        ),
                    )
                )
            tool_end_events: list[tuple[int, int, StreamEvent]] = []
            for provider_index, buf in tool_buffers.items():
                if buf.content_index is None:
                    continue
                tool_end_events.append(
                    (
                        provider_index,
                        buf.content_index,
                        ToolCallEndEvent(
                            content_index=buf.content_index,
                            tool_call=_build_tool_call(buf),
                        ),
                    )
                )
            if end_events:
                end_events.extend(
                    (content_index, event) for _, content_index, event in tool_end_events
                )
                for _, end_event in sorted(end_events, key=lambda item: item[0]):
                    yield end_event
            else:
                # Preserve the adapter's historical pure-tool ordering by the
                # provider's tool-call index, even if chunks arrived interleaved.
                for _, _, end_event in sorted(
                    tool_end_events,
                    key=lambda item: item[0],
                ):
                    yield end_event

            # 9. 最终 DoneEvent（整个正常流只能产生一个）
            yield DoneEvent(stop_reason=final_stop, usage=final_usage)

        except asyncio.CancelledError:
            # 修订 E：cancellation 原样传播；不映射为 ProviderError
            # async with 仍会关闭 response
            raise
        except ProviderError:
            # 已是 ProviderError 子类——向上抛，ModelClient 转 ErrorEvent
            raise
        except Exception as exc:
            # 未知异常——固定短文本，from None
            raise _map_to_provider_error(exc) from None

    # ----------------------------------------------------------------------
    # 内部：请求构造
    # ----------------------------------------------------------------------

    def _build_request_kwargs(self, request: ProviderRequest) -> dict[str, Any]:
        """构造 chat.completions.stream() 的 kwargs.

        固定：
          - stream 由 helper 处理（不在 kwargs 中）
          - stream_options.include_usage = True

        仅在值存在时发送：
          - temperature（None 省略）
          - max_tokens（None 省略）
          - tools（空省略）

        不发送：
          - extra_headers / reasoning_effort / parallel_tool_calls /
            response_format / 旧版 functions / function_call

        `extra_body` 仅用于内部、Provider 已知支持的 GLM thinking 开关。
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": to_openai_messages(
                system_prompt=request.system_prompt,
                messages=request.messages,
            ),
            "stream_options": {"include_usage": True},
        }

        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature

        if self.config.max_tokens is not None:
            kwargs["max_tokens"] = self.config.max_tokens

        if request.tools:
            kwargs["tools"] = to_openai_tools(request.tools)
            if request.metadata.get("pi_agent_tool_choice") == "required":
                kwargs["tool_choice"] = "required"

        if request.metadata.get("pi_agent_thinking") == "disabled":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        return kwargs

    # ----------------------------------------------------------------------
    # aclose()
    # ----------------------------------------------------------------------

    async def aclose(self) -> None:
        """关闭底层 AsyncOpenAI client.

        幂等——多次调用不抛错。close 异常吞掉。_closed=True 后不能再发请求。
        """
        if self._closed:
            return
        self._closed = True
        # 仅关闭自有的 client（测试注入的 client 由 caller 负责）
        if not self._owns_client:
            return
        try:
            await self._client.close()
        except Exception:
            pass


__all__ = [
    "OpenAICompatConfig",
    "OpenAICompatibleProvider",
    "to_openai_messages",
    "to_openai_tools",
]
