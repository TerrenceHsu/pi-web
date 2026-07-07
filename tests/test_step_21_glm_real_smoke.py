"""Step 21 — 真实 GLM API smoke test。

跟 `steps/step-21-provider-adapter-refactor/glm_real_smoke.py` 配套——
pytest 入口，缺凭证时自动 skip。

跑：

    /d/miniconda/envs/pipy/python.exe -m pytest tests/test_step_21_glm_real_smoke.py \\
        -v -m integration

跑前需要至少设置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY 之一。
凭证可以从环境变量读，也可以从项目根 .env 读（本模块在 collect 阶段会
通过 python-dotenv 加载）。
"""
from __future__ import annotations

import os
from pathlib import Path

# collect 阶段加载 .env（pytest 启动时）
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    import warnings

    warnings.warn(
        "python-dotenv 未安装——.env 不会被加载。"
        "请运行 `pip install -e .` 安装完整核心依赖。",
        stacklevel=2,
    )

import pytest

from pi_agent_core_py import (
    DoneEvent,
    GLMClient,
    LLMUserMessage,
    TextContent,
    TextDeltaEvent,
)

_HAS_CREDS = bool(
    os.environ.get("GLM_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or os.environ.get("ANTHROPIC_API_KEY"),
)

_SKIP_REASON = (
    "需要至少设置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY 之一"
)


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_real_glm_returns_text_or_done() -> None:
    """真实 GLM 调用应该至少返回 TextDeltaEvent 或 DoneEvent。"""
    client = GLMClient()
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
@pytest.mark.skipif(not _HAS_CREDS, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_real_glm_done_event_has_usage() -> None:
    """DoneEvent 的 usage 应该被填充（不是全 0）。"""
    client = GLMClient()
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
