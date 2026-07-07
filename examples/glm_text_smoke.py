"""examples/glm_text_smoke.py

最小可运行示例：真实 GLM 文本调用。

如何运行
--------
1. 在项目根 .env 中至少配置一个凭证：

   ANTHROPIC_AUTH_TOKEN=<你的 GL token>
   ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
   ANTHROPIC_MODEL=glm-4.5-flash

   或使用 GLM-prefixed 变量（GLM_API_KEY / GLM_BASE_URL / GLM_MODEL）。

2. 运行：

   /d/miniconda/envs/pipy/python.exe examples/glm_text_smoke.py

预期输出
--------
程序会打印每个 stream 事件（TextDeltaEvent / DoneEvent）以及最终
AssistantMessage 的 stop_reason + 文本内容。

如何判断失败
------------
- 抛 ProviderConfigError：凭证缺失
- 抛 ProviderAuthenticationError：401/403
- 抛 ProviderRateLimitError：429
- 抛 ProviderProtocolError：响应格式不对（如 model 名错）
- 其它 Exception：网络 / 解析错——查看 stderr 完整 traceback

finally 中 await client.close() 确保 httpx 连接释放。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from pi_agent_core_py import (
    DoneEvent,
    GLMClient,
    LLMUserMessage,
    TextContent,
    TextDeltaEvent,
)


async def main() -> None:
    client = GLMClient(max_tokens=256)
    try:
        print("[glm_text_smoke] 发送请求...")
        async for ev in client.stream(
            system_prompt="You are a concise test bot. Reply in one short sentence.",
            messages=[
                LLMUserMessage(content=[TextContent(text="Say hello in English.")]),
            ],
        ):
            if isinstance(ev, TextDeltaEvent):
                print(f"  TextDelta: {ev.delta!r}")
            elif isinstance(ev, DoneEvent):
                print(
                    f"  Done: stop_reason={ev.stop_reason} "
                    f"usage=(in={ev.usage.input}, out={ev.usage.output})"
                )
                break
    finally:
        await client.close()

    print("[glm_text_smoke] done")


if __name__ == "__main__":
    if not (
        os.environ.get("GLM_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    ):
        raise SystemExit(
            "缺凭证：请先在 .env 配置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY"
        )
    asyncio.run(main())
