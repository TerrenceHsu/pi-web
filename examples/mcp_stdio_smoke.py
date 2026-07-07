"""examples/mcp_stdio_smoke.py

最小可运行示例：真实 stdio MCP server + MCPClient。

如何运行
--------
1. 不需要任何凭证；只需要 Python。

2. 运行：

   /d/miniconda/envs/pipy/python.exe examples/mcp_stdio_smoke.py

   会启动 tests/fixtures/fake_mcp_stdio_server.py 作为子进程。

预期输出
--------
程序打印：
- MCP initialize 结果
- tools/list 返回的工具列表
- 调用 echo 工具的结果

如何判断失败
------------
- FileNotFoundError / MCPConnectionError：fake_mcp_stdio_server.py 路径错
- MCPProtocolError：JSON-RPC 响应错——检查 server script
- transport close 后子进程没退出：检查 OS 信号（Windows 上 SIGTERM 不存在，
  Python 用 terminate() 等价于 TerminateProcess）

finally 中 await client.close() 确保子进程退出。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pi_agent_core_py.mcp import MCPClient, MCPServerConfig

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fake_mcp_stdio_server.py"
)


async def main() -> None:
    if not _FIXTURE.is_file():
        raise SystemExit(f"缺 fixture: {_FIXTURE}")

    config = MCPServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=[str(_FIXTURE)],
        timeout_s=10.0,
    )
    client = MCPClient(config)
    try:
        print("[mcp_stdio_smoke] connect + initialize")
        await client.connect()
        init_result = await client.initialize()
        print(f"  serverInfo: {init_result.get('serverInfo')}")

        print("[mcp_stdio_smoke] list_tools")
        tools = await client.list_tools()
        for t in tools:
            print(f"  - {t.name}: {t.description}")

        print("[mcp_stdio_smoke] call echo")
        result = await client.call_tool("echo", {"text": "demo-ping"})
        text = "".join(c.text for c in result.content)
        print(f"  echo returned: {text!r} (is_error={result.is_error})")
    finally:
        await client.close()
    print("[mcp_stdio_smoke] done")


if __name__ == "__main__":
    asyncio.run(main())
