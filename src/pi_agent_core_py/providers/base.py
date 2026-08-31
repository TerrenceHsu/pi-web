"""ProviderAdapter 抽象 + ProviderRequest（Step 21）。

定义 provider 适配层的统一契约：

- `ProviderRequest`：ModelClient → Adapter 的入参封装（system_prompt + messages
  + tools + signal + metadata）
- `ProviderAdapter`：抽象基类，子类按 provider 协议实现 `stream()`

Adapter 的**职责边界**：
- ✅ 把 LLMMessage → provider 协议的 messages
- ✅ 把 ToolDef → provider 协议的 tool schema
- ✅ 把 provider raw stream event → 内部 StreamEvent
- ✅ 把 provider 错误 → ProviderError 子类
- ❌ 不执行工具（Agent loop 负责）
- ❌ 不做权限策略（policy 子包负责）
- ❌ 不做 context transform（context.py 负责）
- ❌ 不写 session / snapshot（harness 负责）
- ❌ 不 import Agent / Harness / Session
"""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from ..llm_messages import LLMMessage
from ..stream_events import StreamEvent
from ..tools import ToolDef


class ProviderRequest(BaseModel):
    """ModelClient → ProviderAdapter 的请求封装。

    把原本 `ModelClient.stream(...)` 的 4 个参数打包成一个对象，便于：
    - adapter 之间共用同一签名
    - 后续扩展 metadata（trace context / request id / cost tags）不破坏 API
    """

    system_prompt: str
    messages: list[LLMMessage]
    tools: list[ToolDef] = Field(default_factory=list)
    signal: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderAdapter(abc.ABC):
    """provider 适配器抽象基类。

    子类必须实现 `stream(request) -> AsyncIterator[StreamEvent]`，
    返回内部标准事件流，**不**返回 provider 原始事件。

    契约：
    - 必须以 `DoneEvent` 或 `ErrorEvent` 收尾（除非抛 ProviderError
      被 ModelClient 捕获后转 ErrorEvent）
    - 抛 ProviderError 子类表示 provider 侧错误
    - 未知异常 ModelClient 也会兜底转 ErrorEvent
    - 定期检查 `request.signal.is_set()`——若已 set，尽快 yield
      `DoneEvent(stop_reason="aborted")`（协作式中止）
    - 持有外部资源（httpx client / SDK client）的子类应覆盖 `aclose()`
      释放资源；ModelClient.close() / AgentHarness.close() 会调它
    """

    provider_id: str = ""
    api_id: str = ""
    model: str = ""
    supports_images: bool = False

    def normalize_tool_call_id(self, tool_call_id: str) -> str:
        """Return an ID accepted by this provider's wire protocol.

        Most providers accept opaque IDs, so identity is the safe default.
        Stricter adapters can override this hook; the shared history
        transformer applies the same mapping to calls and results.
        """

        return tool_call_id

    @abc.abstractmethod
    def stream(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    async def aclose(self) -> None:
        """释放 provider 持有的资源（httpx client / SDK client 等）。

        默认实现：no-op。子类按需覆盖。ModelClient.close() 会调它；
        AgentHarness.close() 间接通过 ModelClient.close() 触发。
        幂等——多次调用不应抛错。
        """
        return None


__all__ = [
    "ProviderAdapter",
    "ProviderRequest",
]
