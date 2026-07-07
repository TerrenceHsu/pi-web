# examples/web_server_smoke.md

最小可运行示例：本地 uvicorn web server（trace viewer）。

## 如何运行

```bash
# 1. 项目根目录
cd D:/LLMTutorial/test

# 2. pipy 环境里跑（不需要凭证；FakeClient 即可）
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -c "
import asyncio
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient, TextDeltaEvent, DoneEvent
from pi_agent_core_py.web.app import create_app
import uvicorn

fake = FakeClient([[TextDeltaEvent(delta='hello from fake'), DoneEvent(stop_reason='stop')]])
agent = Agent(system_prompt='demo', client=fake)
harness = AgentHarness(agent)
app = create_app(harness)
uvicorn.run(app, host='127.0.0.1', port=8000)
"
```

或写到独立脚本（与 `tests/test_integration_web_server.py::test_uvicon_subprocess_starts_and_serves_state` 类似）。

## 预期输出

控制台看到 uvicorn 启动日志：

```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

打开浏览器访问：
- `http://127.0.0.1:8000/` —— 前端 fallback HTML（前端未 build）或 Vue UI（已 build）
- `http://127.0.0.1:8000/api/state` —— Agent 当前状态 JSON
- `http://127.0.0.1:8000/api/messages` —— 当前 messages
- `http://127.0.0.1:8000/api/snapshots` —— 历史 snapshots
- `http://127.0.0.1:8000/api/skills` —— attached skills
- `http://127.0.0.1:8000/api/mcp` —— attached MCP servers / tools

POST 触发一次 prompt：

```bash
curl -X POST http://127.0.0.1:8000/api/prompt \
  -H "Content-Type: application/json" \
  -d '{"text":"hello"}'
```

## 当前 endpoint 列表

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | Agent/Harness 状态摘要 |
| `/api/messages` | GET | 当前 messages 列表 |
| `/api/events` | GET | event buffer（最近事件） |
| `/api/events/clear` | POST | 清空 event buffer |
| `/api/snapshots` | GET | snapshot 摘要列表 |
| `/api/snapshots/{index}` | GET | 单个 snapshot 详情 |
| `/api/session` | GET | 当前 attached session |
| `/api/mcp` | GET | MCP servers / tools / prompts |
| `/api/skills` | GET | attached skills |
| `/api/policy/audit` | GET | permission audit log |
| `/api/prompt` | POST | 触发 prompt（独占） |
| `/api/abort` | POST | 中止当前请求 |
| `/api/reset` | POST | 重置 agent 状态 |
| `/api/stream` | GET (SSE) | Server-Sent Events 实时事件流 |

## 未实现的 spec endpoints

| spec 路径 | 当前状态 | 替代 |
|-----------|---------|------|
| `/api/sessions`（复数） | 404 | 用 `/api/session`（单数） |
| `/api/mcp/tools` | 404 | 用 `/api/mcp`（含 tools 字段） |
| `WebSocket /ws/events` | 404 | 用 SSE `/api/stream` |

未实现是因为 Step 20 仅提供 single-session / SSE 版本；如需 multi-session REST
或 WebSocket，需要在 `web/app.py` 中新增（本轮不在范围内）。

## 如何判断失败

- 端口 8000 被占用：改 `port=8000` 为别的端口
- `ModuleNotFoundError: No module named 'fastapi'` / `'uvicorn'`：
  在 pipy 环境内 `pip install -e .[web]`
- 启动后访问 /api/* 一直 500：检查 stderr——harness 构造时可能出错
- 浏览器一直转：检查防火墙；本地优先用 127.0.0.1（不是 0.0.0.0）

## 关停

`Ctrl+C` 触发 uvicorn graceful shutdown。
