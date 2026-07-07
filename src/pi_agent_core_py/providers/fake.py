"""FakeProviderAdapter——离线测试 / demo（Step 21）。

按预置脚本回放 StreamEvent，记录每次 stream() 调用收到的 system_prompt /
messages / tools，便于 pytest 验证契约。`FakeClient` 是 `ModelClient` 的 thin
wrapper，内部委托给 `FakeProviderAdapter`——保持旧 API 完全兼容。

行为契约：
- 每次调用 `stream()` 消费一组 events（按顺序）
- 第 N+1 次（脚本耗尽）yield `ErrorEvent("no more scripts")`
- `signal.is_set()` 时立即 yield `DoneEvent(stop_reason="aborted")` 并返回
  （Step 21 统一 abort 语义：aborted 走 DoneEvent，不走 ErrorEvent）
- 不调用任何真实网络
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..llm_messages import LLMMessage
from ..stream_events import DoneEvent, ErrorEvent, StreamEvent
from ..tools import ToolDef
from .base import ProviderAdapter, ProviderRequest


class FakeProviderAdapter(ProviderAdapter):
    """按预置脚本回放 StreamEvent。

    ```python
    adapter = FakeProviderAdapter([
        [TextDeltaEvent(delta="hello"), DoneEvent(...)],
    ])
    ```

    字段（与旧 FakeClient 一致）：
      last_messages / last_tools / last_system_prompt
      all_messages_calls / all_tools_calls / all_system_prompt_calls
    """

    provider_id = "fake"
    model = "fake-1"

    def __init__(
        self,
        turns: list[list[StreamEvent]],
        *,
        model: str = "fake-1",
    ) -> None:
        self._turns: list[list[StreamEvent]] = [list(t) for t in turns]
        self._cursor = 0
        self.model = model
        # 记录字段——FakeClient 通过 __getattr__ / 属性 alias 暴露给老用户
        self.last_messages: list[LLMMessage] = []
        self.last_tools: list[ToolDef] | None = None
        self.all_messages_calls: list[list[LLMMessage]] = []
        self.all_tools_calls: list[list[ToolDef] | None] = []
        self.last_system_prompt: str = ""
        self.all_system_prompt_calls: list[str] = []

    async def stream(
        self,
        request: ProviderRequest,
    ) -> AsyncIterator[StreamEvent]:
        """消费一个 turn 的脚本。

        - 记录 system_prompt / messages / tools
        - 脚本耗尽时 yield ErrorEvent("no more scripts")
        - signal set 时 yield DoneEvent(stop_reason="aborted") 并返回
          （Step 21 abort 语义）
        """
        # 兼容旧 FakeClient.stream：tools None 时记录 None，否则深拷贝
        tools = request.tools
        self.last_messages = list(request.messages)
        self.last_tools = list(tools) if tools else None
        self.all_messages_calls.append(list(request.messages))
        self.all_tools_calls.append(list(tools) if tools else None)
        self.last_system_prompt = request.system_prompt
        self.all_system_prompt_calls.append(request.system_prompt)

        if self._cursor >= len(self._turns):
            yield ErrorEvent(message="FakeClient: no more scripts")
            return
        script = self._turns[self._cursor]
        self._cursor += 1
        for ev in script:
            if request.signal is not None and _is_set(request.signal):
                yield DoneEvent(stop_reason="aborted")
                return
            yield ev


def _is_set(signal: Any) -> bool:
    """安全检查 signal.is_set()——支持 asyncio.Event / threading.Event / 自定义。"""
    try:
        return bool(signal.is_set())
    except Exception:
        return False


__all__ = ["FakeProviderAdapter"]
