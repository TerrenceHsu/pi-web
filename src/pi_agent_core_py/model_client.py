"""ModelClient + 向后兼容 shim（Step 21 重构后变 thin wrapper）。

历史：Step 1–20 这里塞了 `ModelClient` 抽象 + `GLMClient` 实现（含 Anthropic
协议解析）+ `FakeClient`（含脚本回放）+ helper 函数。Step 21 把 provider 适配
拆到 `providers/` 子包：

```text
Agent / Loop
   ↓ stream(...)
ModelClient  (thin wrapper，本文件)
   ↓ ProviderRequest
ProviderAdapter  (providers/)
   ↓ StreamEvent  (定义在 stream_events.py)
```

本文件保留：
- `ModelClient`：thin wrapper，把 `stream(...)` 委托给 `ProviderAdapter`
- `FakeClient` / `GLMClient`：旧 API 的向后兼容 shim
- StreamEvent 系列**重新导出**（实参定义在 `stream_events.py`，避免循环 import）

旧 API 行为完全保持：

```python
client = FakeClient([[TextDeltaEvent(delta="hi"), DoneEvent(...)]])
async for ev in client.stream(system_prompt="x", messages=[], tools=None):
    ...
```

```python
client = GLMClient()  # 读 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL
```
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from .llm_messages import LLMMessage
from .providers.base import ProviderAdapter, ProviderRequest
from .providers.errors import ProviderError
from .providers.fake import FakeProviderAdapter
from .providers.glm import GLMConfig, GLMProviderAdapter, resolve_glm_credentials
from .providers.retry import (
    ProviderRetryPolicy,
    is_retryable_provider_error,
    wait_for_retry,
)
from .providers.transform import transform_messages_for_provider
from .stream_events import (
    DoneEvent,
    ErrorEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallEvent,
    ToolCallStartEvent,
)
from .tools import ToolDef

# ============================================================================
# ModelClient（thin wrapper）
# ============================================================================


class ModelClient:
    """LLM 客户端抽象。

    Step 21 重构后：
    - 持有 `ProviderAdapter`，把 `stream(...)` 委托给它
    - 捕获 `ProviderError` / `Exception`，统一转 `ErrorEvent`
    - 子类（FakeClient / GLMClient）只负责构造对应的 adapter

    契约：
    - `stream()` 是 async generator，按顺序 yield StreamEvent
    - 必须以 DoneEvent 或 ErrorEvent 结束
    - provider 异常**不**抛出——改成 yield ErrorEvent
    """

    provider_id: str = ""
    api_id: str = ""
    model: str = ""

    def __init__(
        self,
        adapter: ProviderAdapter,
        *,
        retry_policy: ProviderRetryPolicy | None = None,
    ) -> None:
        self.adapter = adapter
        self.retry_policy = retry_policy or ProviderRetryPolicy()
        # 暴露 adapter 的 provider_id / model，便于上层 metadata 使用
        self.provider_id = getattr(adapter, "provider_id", "") or self.provider_id
        self.api_id = getattr(adapter, "api_id", "") or self.api_id
        self.model = getattr(adapter, "model", "") or self.model

    async def stream(
        self,
        *,
        system_prompt: str,
        messages: list[LLMMessage],
        tools: list[ToolDef] | None = None,
        signal: asyncio.Event | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """统一入口——委托给 adapter，捕获异常转 ErrorEvent。

        `metadata` 可选——传给 adapter 作为 trace / cost tag 等附加信息；
        adapter 可读 `request.metadata` 但不强求使用。

        注意：子类**不应**覆盖此方法；如需自定义行为，覆盖 adapter 即可。
        """
        transformed_messages = transform_messages_for_provider(
            messages,
            target_provider=self.provider_id,
            target_api=self.api_id,
            target_model=self.model,
            supports_images=bool(getattr(self.adapter, "supports_images", False)),
            normalize_tool_call_id=self.adapter.normalize_tool_call_id,
        )
        base_request = ProviderRequest(
            system_prompt=system_prompt,
            messages=transformed_messages,
            tools=list(tools) if tools else [],
            signal=signal,
            metadata=dict(metadata) if metadata else {},
        )
        retry_index = 0
        while True:
            emitted = False
            request_metadata = dict(base_request.metadata)
            if retry_index:
                request_metadata["pi_agent_retry_attempt"] = retry_index
            request = ProviderRequest(
                system_prompt=base_request.system_prompt,
                messages=[message.model_copy(deep=True) for message in base_request.messages],
                tools=[tool.model_copy(deep=True) for tool in base_request.tools],
                signal=base_request.signal,
                metadata=request_metadata,
            )
            try:
                async for ev in self.adapter.stream(request):
                    emitted = True
                    yield ev
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                can_retry = (
                    not emitted
                    and retry_index < self.retry_policy.max_retries
                    and is_retryable_provider_error(e)
                )
                if can_retry:
                    retry_index += 1
                    delay = self.retry_policy.delay_for_retry(retry_index, e)
                    if await wait_for_retry(delay, signal):
                        continue
                    yield DoneEvent(stop_reason="aborted")
                    return
                if isinstance(e, ProviderError):
                    yield ErrorEvent(message=f"{type(e).__name__}: {e}")
                else:
                    yield ErrorEvent(message=f"{type(e).__name__}: {e}")
                return

    async def close(self) -> None:
        """关闭底层 provider adapter 释放资源（httpx client 等）。幂等。

        AgentHarness.close() 会调它；纯内存 / FakeClient 调 no-op。
        """
        try:
            aclose = getattr(self.adapter, "aclose", None)
            if aclose is not None:
                await aclose()
        except Exception:
            # 关闭失败不应让 harness.close() 崩溃
            pass


# ============================================================================
# FakeClient（向后兼容 shim）
# ============================================================================


class FakeClient(ModelClient):
    """按预置脚本回放 StreamEvent。

    旧 API 完全兼容：

    ```python
    fake = FakeClient([
        [TextDeltaEvent(delta="hello"), DoneEvent(...)],
    ])
    ```

    实现委托给 `FakeProviderAdapter`；本类的 `last_messages` / `last_tools` /
    `last_system_prompt` / `all_*_calls` 通过 property 转发给 adapter，保持
    旧 API 不变。
    """

    provider_id = "fake"
    api_id = "fake"
    model = "fake-1"

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        adapter = FakeProviderAdapter(scripts)
        super().__init__(adapter)
        self._fake: FakeProviderAdapter = adapter

    # --- 字段转发（保持旧 API：fake.last_messages / fake.all_tools_calls / ...）

    @property
    def last_messages(self) -> list[LLMMessage]:
        return self._fake.last_messages

    @property
    def last_tools(self) -> list[ToolDef] | None:
        return self._fake.last_tools

    @property
    def last_system_prompt(self) -> str:
        return self._fake.last_system_prompt

    @property
    def all_messages_calls(self) -> list[list[LLMMessage]]:
        return self._fake.all_messages_calls

    @property
    def all_tools_calls(self) -> list[list[ToolDef] | None]:
        return self._fake.all_tools_calls

    @property
    def all_system_prompt_calls(self) -> list[str]:
        return self._fake.all_system_prompt_calls


# ============================================================================
# GLMClient（向后兼容 shim）
# ============================================================================


class GLMClient(ModelClient):
    """智谱 GLM via Anthropic 兼容端点。

    旧 API 完全兼容（保留 `auth_token` kwarg + `ANTHROPIC_*` env）：

    ```python
    client = GLMClient()                  # 读 env
    client = GLMClient(auth_token="...")  # 旧 keyword
    client = GLMClient(api_key="...")     # 新 keyword（推荐）
    ```

    环境变量优先级（高 → 低）：

    ```
    api_key:   显式参数（api_key > auth_token） > GLM_API_KEY
               > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY
    base_url:  显式参数 > GLM_BASE_URL > ANTHROPIC_BASE_URL > BASE_URL > 默认智谱端点
    model:     显式参数 > GLM_MODEL > ANTHROPIC_MODEL > MODEL > glm-4.5-flash
    ```
    """

    provider_id = "glm"
    api_id = "anthropic-messages"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        auth_token: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
        timeout_s: float = 60.0,
        temperature: float = 0.0,
    ) -> None:
        # auth_token 作为旧 keyword 的 fallback（保留向后兼容）
        effective_key = api_key or auth_token
        creds = resolve_glm_credentials(
            api_key=effective_key,
            base_url=base_url,
            model=model,
        )
        config = GLMConfig(
            api_key=creds["api_key"],
            base_url=creds["base_url"],
            model=creds["model"],
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            temperature=temperature,
        )
        adapter = GLMProviderAdapter(config)
        super().__init__(adapter)
        # 兼容旧字段
        self._base_url = creds["base_url"]
        self.max_tokens = max_tokens


__all__ = [
    # Stream 事件（重新导出）
    "StreamEvent",
    "TextStartEvent",
    "TextDeltaEvent",
    "TextEndEvent",
    "ThinkingStartEvent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "DoneEvent",
    "ErrorEvent",
    "ToolCallEvent",
    "ToolCallStartEvent",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
    # ModelClient 系列
    "ModelClient",
    "FakeClient",
    "GLMClient",
]
