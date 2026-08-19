"""Step 21 — 真实 GLM API smoke test。

跟 `steps/step-21-provider-adapter-refactor/glm_real_smoke.py` 配套。

跑：

    D:\\miniconda\\envs\\pipy\\python.exe scripts/run_live_integration_tests.py

固定入口优先使用当前 Web 默认 GLM Profile/Keyring；不会自动加载项目根
``.env``，避免旧凭证覆盖 Web 中已验证可用的配置。
"""

from __future__ import annotations

import pytest

from pi_agent_core_py import (
    DoneEvent,
    GLMClient,
    LLMUserMessage,
    TextContent,
    TextDeltaEvent,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_glm_returns_text_or_done(glm_live_config) -> None:
    """真实 GLM 调用应该至少返回 TextDeltaEvent 或 DoneEvent。"""
    client = GLMClient(
        api_key=glm_live_config.api_key,
        model=glm_live_config.model,
        base_url=glm_live_config.base_url,
    )
    try:
        events: list = []
        async for ev in client.stream(
            system_prompt="You are a test bot. Reply concisely.",
            messages=[LLMUserMessage(content=[TextContent(text="Say OK.")])],
        ):
            events.append(ev)

        # 至少有一个 TextDeltaEvent 或 DoneEvent
        has_text = any(isinstance(e, TextDeltaEvent) for e in events)
        has_done = any(isinstance(e, DoneEvent) for e in events)
        assert has_text or has_done, f"stream 既无 TextDeltaEvent 也无 DoneEvent: {events}"
    finally:
        await client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_glm_done_event_has_usage(glm_live_config) -> None:
    """DoneEvent 的 usage 应该被填充（不是全 0）。"""
    client = GLMClient(
        api_key=glm_live_config.api_key,
        model=glm_live_config.model,
        base_url=glm_live_config.base_url,
    )
    try:
        final = None
        async for ev in client.stream(
            system_prompt="x",
            messages=[LLMUserMessage(content=[TextContent(text="hi")])],
        ):
            if isinstance(ev, DoneEvent):
                final = ev

        assert final is not None
        # usage 至少 input > 0
        assert final.usage.input > 0, f"usage.input 应 > 0，实际：{final.usage.input}"
    finally:
        await client.close()
