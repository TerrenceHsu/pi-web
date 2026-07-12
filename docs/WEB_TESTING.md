# Web Testing

> **Web Claude P0 MVP**（2026-07-07）。详见 [`docs/RELEASE_NOTES_WEB_CLAUDE_P0.md`](RELEASE_NOTES_WEB_CLAUDE_P0.md)。
>
> 测试覆盖 v0.0.22 Web backend baseline + P0-1 sqlite sessions + P0-2 files + P0-3 file tools + P0-4 Skills/MCP Web UI Step 1–2 + P0-5 default system prompt。
> 所有 web 相关测试统一标 `@pytest.mark.slow`，离线 baseline 不依赖。

## 测试分层

```
tests/
├── test_integration_web_server.py        v0.0.22 baseline（25 用例）
│   ├── spec endpoints 兼容（sessions / mcp/tools）
│   ├── hook 生命周期（multiple create_app + dispose_app）
│   ├── event_buffer 上限（default 1000 / max_size 覆盖）
│   ├── 409 路径（harness 已被外部占用 / state.running flag）
│   ├── WebSocket（hello + broadcast after prompt）
│   ├── SSE limit（?limit=0 / ?limit=1 + bg POST）
│   ├── prompt preview 保护（403 默认 / 403 非 localhost / True + localhost 通过）
│   ├── 基础 endpoint 健康检查
│   └── uvicorn subprocess（端口 retry）
│
├── test_web_sessions_sqlite.py           P0-1 sqlite 多会话（13 用例）
│   ├── GET /api/sessions 列表
│   ├── POST /api/sessions 创建
│   ├── GET /api/messages?session_id= 历史
│   ├── POST /api/prompt body.session_id 持久化
│   ├── default session 自动创建
│   ├── 多 session 不串消息
│   ├── DELETE /api/sessions/{sid} 级联
│   ├── 兼容旧 GET /api/messages（无 session_id）
│   ├── 409 / PATCH 重命名
│
├── test_session_sqlite.py                P0-1 sqlite store 单元（30 用例）
│   └── CRUD / append / list 强类型 / replace / snapshot / 错误 / 并发 / 持久化
│
├── test_virtual_file_store.py            P0-2 file store 单元（33 用例）
│   └── sanitize / init / save/get/list / delete / 大小限制 / 路径穿越 / mime
│
├── test_web_files.py                     P0-2 web 层（22 用例）
│   ├── POST /api/sessions/{sid}/files 单 / 多上传
│   ├── GET /api/sessions/{sid}/files 列出 + session 隔离
│   ├── GET /api/sessions/{sid}/files/{fid} 下载
│   ├── DELETE /api/sessions/{sid}/files/{fid} + DELETE /api/files/{fid}?session_id=
│   ├── 404 / 403 / 413 错误路径
│   ├── 路径穿越防御
│   ├── sha256 / mime 正确
│   └── 删 session 级联 uploads
│
├── test_file_tools.py                    P0-3 list_files / view_file（27 用例）
│   ├── list_files（含 unsupported 图片 / 跨 session 隔离 / no session）
│   ├── view_file md / text / 大文本截断
│   ├── html（HTMLParser 去 script/style）
│   ├── csv（columns/rows / max_rows / Sniffer）
│   ├── parquet（schema/rows / max_rows / pyarrow 缺失）
│   ├── pdf / image / binary
│   ├── errors（missing / cross-session / invalid args / no session）
│   └── JSON serializable / classify_format
│
├── test_prompt_file_injection.py         P0-3 prompt + file_ids（15 用例）
│   ├── md/html/csv 注入
│   ├── 图片 image_unsupported
│   ├── 多文件顺序
│   ├── text + files 同条 UserMessage
│   ├── missing 404 / cross-session 403
│   ├── 无附件不回归
│   ├── attachment metadata
│   └── convert_to_llm 不暴露 path
│
├── test_system_prompt.py                 P0-5 默认 system prompt（17 用例）
│   └── build_default_system_prompt（skills/mcp/files 段 + 图片 unsupported 描述）
│
├── test_web_skills_api.py                P0-4 Step 1 Skills API（26 用例）
│   ├── POST /api/skills/upload 成功 / 重名 409 / 格式错 400 / 非 utf-8 400 / 太大 400
│   ├── 部分成功 207 / 无文件 400 / registry 未 attach 422
│   ├── enable / disable 切换 + GET 列表反映
│   ├── enable / disable / GET 不存在 → 404
│   ├── GET /api/skills/{name} 默认无 prompt / ?include_prompt=true 默认 403 / allow + localhost 200
│   └── POST /api/prompt skill_names / skill_selection 合并去重 / unknown skill → 400（不再 500）
│
├── test_web_mcp_api.py                   P0-4 Step 2 MCP API（33 用例）
│   ├── GET /api/mcp/servers 初始为空
│   ├── POST /api/mcp/servers 成功 / 重名 409 / 非法 name 400 / 空 command 400 / bad args 400 / bad env 400
│   ├── **env value 绝不出现在任何 response（强校验）**
│   ├── POST /api/mcp/servers/{name}/test 成功（fake stdio）/ 不污染 harness / 失败 502 / 不存在 404 / 不写 state
│   ├── POST /enable / disable + GET /api/mcp/tools 看到 tools / disabled 看不到
│   ├── DELETE /api/mcp/servers/{name}（先 disable 释放 transport；清孤儿 disabled tools）
│   ├── POST /api/mcp/tools/{tool_name}/disable 真实 unregister / enable 重新 register
│   ├── invalid tool_name → 400 / unknown tool → 404 / server disabled → 409 / 幂等
│   └── disable filter survives server refresh（关键回归点）
│
└── test_web_prompt_async.py              P1-B1 异步 prompt + request registry（20 用例，默认运行）
    ├── POST /api/prompt/async 202 + request_id 立即返回（delayed FakeClient 验证）
    ├── GET /api/requests/{id} status 流转 queued → running → completed
    ├── messages 持久化到 session / file_ids / skill_names 注入
    ├── 4xx 校验失败不创建 request（unknown skill / missing file / empty text）
    ├── 并发 409（同 session + 全局 harness busy）
    ├── harness 异常 → status=error + safe_error
    ├── abort running / completed 幂等 / unknown 404
    ├── shutdown 收敛 active task
    └── 旧 POST /api/prompt + POST /api/abort 兼容别名验证

test_web_event_envelope.py               P1-B2 WebEventEnvelope + 去重（11 用例，默认运行）
    ├── envelope 7 字段 schema（event_id/request_id/session_id/sequence/type/timestamp/payload）
    ├── event_id 唯一 / sequence 全局单调递增
    ├── prompt 事件关联正确 request_id/session_id
    ├── async request 记录 event_start/end_sequence
    ├── GET /api/events 过滤：after_sequence / session_id / request_id / limit
    ├── buffer 截断后 gap=true（first_sequence 自动跟随 head）
    └── envelope.payload 保留原 AgentEvent 字段 + _received_at_ms（向后兼容）
```

总计：**Web Claude P0 MVP 共 ~259 个 web 相关用例**（v0.0.22 baseline 25 + P0-1 sqlite 30+13 + P0-2 files 33+22 + P0-3 file tools 27+15 + P0-5 system prompt 17 + P0-4 Step 1 skills 26 + P0-4 Step 2 mcp 33 + step-20 serializers/state/app 51 + P1-B1 async prompt 20 + P1-B2 envelope 11 ≈ 259）。

总计：**Web Claude P0 MVP 共 ~228 个 web 相关用例**（v0.0.22 baseline 25 + P0-1 sqlite 30+13 + P0-2 files 33+22 + P0-3 file tools 27+15 + P0-5 system prompt 17 + P0-4 Step 1 skills 26 + P0-4 Step 2 mcp 33 + step-20 serializers/state/app 51 ≈ 228）。

---

## 运行命令

### 最终验证全套（P0 MVP）

```bash
# 后端——offline baseline
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow and not docker" -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"

# 后端——web 集成 + P0-1~P0-5（slow）
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest tests/test_web_sessions_sqlite.py tests/test_session_sqlite.py tests/test_web_files.py tests/test_virtual_file_store.py tests/test_file_tools.py tests/test_prompt_file_injection.py tests/test_system_prompt.py -v -m "slow and not docker" -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"

# 后端——Skills/MCP Web API（P0-4 Step 1/2）
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest tests/test_web_skills_api.py tests/test_web_mcp_api.py -v -m "slow and not docker" -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"

# 后端——v0.0.22 baseline 集成
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest tests/test_integration_web_server.py -v -m "slow and not docker" -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"

# Lint
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m ruff check src tests

# 前端——build（含 vue-tsc 类型检查）
cd src/pi_agent_core_py/web/frontend && npm install && npm run build
```

### 预期结果

| 命令 | 预期 |
|------|------|
| Offline baseline（not slow） | **678 passed** |
| Skills/MCP Web API | **59 passed** |
| Web Files / Prompt-File-Injection | **37 passed** |
| Web Sessions / SQLite / File Tools / System Prompt | **87 passed** |
| v0.0.22 Web integration | **25 passed** |
| ruff | **All checks passed** |
| npm run build（含 vue-tsc） | **123 modules / ~128 KB JS**（gzip ~45 KB） |

---

## 手动 smoke checklist（P0 MVP）

后端 / 前端启动后，浏览器打开 `http://127.0.0.1:8000` 按 checklist 验证：

### 基础聊天
1. ✅ 自动加载 default session
2. ✅ 点 `+ New chat` 新建 session，旧 session 保留在列表
3. ✅ 输入消息 + Enter → assistant 流式回答
4. ✅ Shift+Enter 换行
5. ✅ running 时 Send → Stop；点 Stop 触发 abort

### 文件上传
6. ✅ 📎 选择 md / html / csv / parquet / 文本 → AttachmentBar 显示 FileChip
7. ✅ 拖放文件到输入框 → 同 6
8. ✅ 输入"读这个文件" + Send → 看到 `FileRead` card（view_file 工具调用）
9. ✅ 上传图片 → FileChip 显示但发 prompt 时模型明确回复"不支持图片"
10. ✅ 上传 PDF → 同 9（"PDF 正文暂未解析"）

### Skills Modal
11. ✅ 点左栏 `Skills` → 居中 Modal 打开（**不是右栏 Drawer**）
12. ✅ 上传 SKILL.md → 列表出现 skill card
13. ✅ enable skill → status badge 变绿
14. ✅ 勾选 "Use this turn" → 发 prompt → 中间消息流出现 `SkillUsed` card

### MCP Modal
15. ✅ 点左栏 `MCP` → 居中 Modal 打开
16. ✅ Add server（name / command / args JSON / env key=value）→ 提交后**所有字段清空**
17. ✅ 点 Test → "✓ last test: N tools detected"
18. ✅ 点 Enable → status badge 变 `attached · N tools`；下方 Tools 区出现 tools
19. ✅ 在 Tools 区点 Disable 单个 tool → badge 变 disabled
20. ✅ **关键**：刷新 server list → env keys 显示 KEY 名 + `(values hidden)`，**value 永远不回显**

### 错误路径
21. ✅ 发送相同 prompt 两次（快速）→ 第二次收到 409 "Agent is already running"
22. ✅ Skills modal 上传重名 SKILL.md → 看到 409 错误条
23. ✅ MCP modal Test 不存在的命令 → 看到 502 + 错误描述

### Session 管理
24. ✅ 重命名 session（hover → ✎）
25. ✅ 删除 session（hover → × + confirm）→ 自动切到剩余 / 新建

---

## P1-B1 Async Prompt + Request Registry（2026-07-12）

详细报告：[`docs/P1_B_VALIDATION_REPORT.md`](P1_B_VALIDATION_REPORT.md)。

### 新增测试

- `tests/test_web_prompt_async.py`（20 用例，**默认运行**——Fast FakeClient + TestClient，无外部依赖）

### 新增 endpoint

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/prompt/async` | 异步触发 prompt，立即返回 202 + request_id |
| GET | `/api/requests/{request_id}` | 查询 request status（active + history） |
| POST | `/api/requests/{request_id}/abort` | 幂等 abort |
| POST | `/api/abort` | **保留为兼容别名**——转发到 active request |

### 运行命令

```bash
# B1 测试单独跑
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest \
  tests/test_web_prompt_async.py -v \
  -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"

# 验证旧 POST /api/prompt 不回归
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest \
  tests/test_integration_web_server.py tests/test_web_sessions_sqlite.py \
  tests/test_prompt_file_injection.py tests/test_web_skills_api.py -v \
  -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"
```

### 关键测试 fixture

`web_client` / `web_client_slow` 用 `with TestClient(app) as client` 让 lifespan + portal 持续——background task 跨 request 正常跑（必要条件，否则 task 在 response 后被 cancel）。

---

## P1-B2 WebEventEnvelope + Event Dedup（2026-07-12）

详细报告：[`docs/P1_B2_VALIDATION_REPORT.md`](P1_B2_VALIDATION_REPORT.md)。

### 新增测试

- `tests/test_web_event_envelope.py`（11 用例，**默认运行**）

### 新增/变更行为

| 区域 | 变更 |
|---|---|
| 事件 schema | 所有 WS / SSE / GET /api/events 事件用统一 `WebEventEnvelope`（7 字段：event_id / request_id / session_id / sequence / type / timestamp / payload） |
| 序列号 | 全局单调递增 `state.next_event_sequence`；广播入口一次性分配 |
| buffer | `TraceEventBuffer.first_sequence` / `last_sequence` 跟随 buffer head 自动更新；maxlen 截断时自动调整 |
| GET /api/events | 加 `?session_id` / `?request_id` / `?after_sequence` / `?limit`；响应含 `first_available_sequence` / `last_available_sequence` / `has_more` / `gap` |
| 前端 chatStore | 加 `seenEventIds` / `lastSequenceBySession` / `gapDetected` / `currentRequestId` / `activeSessionId` state；handleEvent envelope-aware 去重 + session 隔离 + gap 检测 |
| 未改 | `sendPrompt`（仍用同步 /api/prompt）/ Stop 按钮（仍用旧 /api/abort）/ WS reconnect（B3 任务）|

### 运行命令

```bash
# B2 测试单独跑
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest \
  tests/test_web_event_envelope.py -v \
  -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"

# 验证旧 12 e2e 不回归
cd tests/e2e && npm run test:e2e
```

### Envelope schema（前后端契约）

```typescript
// frontend/src/types/events.ts
interface WebEventEnvelope {
  event_id: string         // "evt_<uuid4 hex>"
  request_id: string | null
  session_id: string | null
  sequence: number         // 全局单调递增
  type: string             // 与 payload.type 冗余
  timestamp: string        // ISO8601 UTC
  payload: Record<string, any>  // 原 AgentEvent serialize_event 结果
}
```

---

## P1-A Real Environment Validation（2026-07-12）

P0 MVP freeze 后的真实环境验证阶段。把上面 25 项 checklist 中尚未自动化的项补完，并新增 MCP tool enable/disable 真链路 E2E + 真实 GLM 多轮 tool_use smoke。

详细报告：[`docs/P1_A_BROWSER_SMOKE_REPORT.md`](P1_A_BROWSER_SMOKE_REPORT.md) + [`docs/P1_A_VALIDATION_REPORT.md`](P1_A_VALIDATION_REPORT.md)。

### 25 项 checklist 当前状态

| ID | 状态 | 验证方式 |
|---|---|---|
| #1–3, #5–7, #11–15, #20, #24–25 | PASS | P0 e2e（10 smoke）|
| #4 Shift+Enter 换行 | **PASS** | P1-A2 `mcp-tool-lifecycle.spec.ts` Smoke 11 |
| #8 view_file → FileRead card | Agent 层 PASS | P1-A3 真实 GLM tool_use smoke（Web 层留 P1-B+） |
| #9 上传图片 → 不支持回复 | Agent 层 PASS | 同上（图片走 unsupported 分支已单测覆盖）|
| #10 上传 PDF → 未解析回复 | Agent 层 PASS | 同上（PDF 走 unsupported 分支已单测覆盖）|
| #16 Add server → 字段清空 | **PASS** | P1-A2 `mcp-tool-lifecycle.spec.ts` Smoke 10 |
| #17 Test → "✓ N tools detected" | **PASS** | 同上 |
| #18 Enable server → badge `attached · N tools` | **PASS** | 同上 |
| #19 Disable 单个 tool → badge disabled | **PASS** | 同上 |
| #21 并发 prompt → 409 | **PASS** | P1-A1 API smoke（threading.Barrier 严格并发） |
| #22 重名 SKILL.md → 409 | **PASS** | P1-A1 API smoke |
| #23 MCP Test 不存在命令 → 502 | **PASS** | P1-A1 API smoke |

### 新增自动化资产

- `tests/e2e/mcp-tool-lifecycle.spec.ts` — MCP tool enable/disable 真链路（含 Shift+Enter）
- `tests/integration/test_real_glm_tool_use.py` — 真实 GLM e2e_probe round-trip（slow+integration）
- `scripts/smoke_real_glm_tool_use.py` — 独立 smoke 脚本（不依赖 pytest）

### 运行命令

```bash
# 真实 GLM tool_use（需要 ANTHROPIC_AUTH_TOKEN / GLM_API_KEY / ANTHROPIC_API_KEY 至少一个）
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest \
  tests/integration/test_real_glm_tool_use.py -v -m "slow and integration" \
  -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" \
  --no-cov

# 独立 smoke 脚本（输出事件摘要）
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe scripts/smoke_real_glm_tool_use.py

# Playwright MCP tool lifecycle 真链路
cd tests/e2e && npx playwright test mcp-tool-lifecycle.spec.ts

# Playwright 全套（不回归）
cd tests/e2e && npm run test:e2e
```

### 失败排查

| 现象 | 查看 |
|---|---|
| Playwright 失败 | `tests/e2e/test-results/<spec>/trace.zip` + `screenshot` + `video` |
| uvicorn log | `tests/e2e/test-results/<spec>/error-context.md` |
| MCP subprocess stderr | webServer stdout/stderr（pipe 到 playwright 输出） |
| 真实 GLM 失败 | `scripts/smoke_real_glm_tool_use.py` 输出的 events 摘要 |

### 安全说明

- 所有报告 / 截图 / 日志严格不打印 GLM API key
- MCP env value 仅在 type=password 输入，提交后立即清空，不出现在 body innerText
- e2e_probe 工具仅用于测试，不注册进产品默认工具集

---

## fixture 设计

### `web_client`

最常用——单 harness / app / TestClient，结束自动 dispose。

```python
@pytest.fixture
def web_client():
    harness = _make_harness()
    app = create_app(harness)
    client = TestClient(app)
    try:
        yield client, harness, app
    finally:
        client.close()
        dispose_app(app)
```

`client.close()` 会触发 FastAPI lifespan 的 shutdown 钩子，自动 `remove_on_event_hook`。
`dispose_app(app)` 兜底——不进 `with TestClient(...)` 的场景必备。

### `web_client_with_preview`

带 `allow_prompt_preview=True`。供 Skills prompt preview 正向测试用。

---

## `dispose_app(app)` 使用场景

`create_app(harness)` 会把 `_web_event_hook` 通过 `harness.add_on_event_hook(...)` 永久加入 `harness.on_event_hooks`。FastAPI lifespan 的 shutdown 钩子会清理——但**只在 TestClient / uvicorn 进入了 lifespan 时**才触发。

下面这些场景需要手动调 `dispose_app(app)`：

```python
# 1. 直接构造 app 不进 with —— lifespan 不跑
app = create_app(harness)
try:
    assert app.state.web.event_buffer.max_size == 10
finally:
    dispose_app(app)

# 2. 同一 fixture 多次 create_app 测试 hook 行为
def test_xxx():
    harness = _make_harness()
    apps = [create_app(harness) for _ in range(3)]
    try:
        assert len(harness.on_event_hooks) == 3
    finally:
        for app in apps:
            dispose_app(app)
    assert len(harness.on_event_hooks) == 0
```

幂等——多次调用不抛错。

---

## `event_buffer_max_size` 如何设置

默认 `maxlen=1000`（`TraceEventBuffer` 默认）。两种覆盖方式：

```python
# 方式 1：create_app 入参
app = create_app(harness, event_buffer_max_size=500)

# 方式 2：直接换 buffer 实例（不推荐，绕过 create_app 校验）
state = app.state.web
state.event_buffer = TraceEventBuffer(max_size=500)
```

超过 `max_size` 时 deque 自动丢最旧（这是 `collections.deque(maxlen=N)` 的内建行为）。

---

## SSE limit 测试技巧

### `?limit=0` —— 立即关闭

```python
with client.stream("GET", "/api/stream?limit=0", timeout=10.0) as resp:
    assert resp.status_code == 200
    for line in resp.iter_lines():
        chunks.append(line)
```

### `?limit=1` —— 配合 background thread POST

```python
import threading

def trigger() -> None:
    time.sleep(0.5)
    client.post("/api/prompt", json={"text": "trigger"})

threading.Thread(target=trigger, daemon=True).start()

with client.stream("GET", "/api/stream?limit=1", timeout=20.0) as resp:
    for line in resp.iter_lines():
        chunks.append(line)
```

TestClient 是同步阻塞——SSE 必须用 background thread 触发 prompt。

---

## uvicorn subprocess 端口 retry

`_pick_free_port()` 用 `bind(127.0.0.1, 0)` 取空闲端口；释放后子进程 bind 时可能被抢。测试加 3 次 retry。失败时 `pytest.fail` 打印最近一次 stderr 帮助诊断。

---

## 常见问题

### 测试挂住不退出

99% 是 SSE 无限流。两个修复：

1. 加 `?limit=N`：`/api/stream?limit=0` 立即关
2. 加 `timeout=N.0`：`client.stream(..., timeout=10.0)`

### hook 累积导致事件数翻倍

检查 `web_client` fixture 退出时是否调了 `dispose_app(app)` 或 `client.close()`。

### WebSocket 测试报 `WebSocketDisconnect`

TestClient 的 WS 用 `with client.websocket_connect("/ws/events") as ws:`。

### POST `/api/prompt` 一直返回 500

检查 `state.last_error`。常见原因：FakeClient scripts 用完 → 模型 ErrorEvent → loop 包成 `stop_reason="error"` assistant → 但 `harness.run_prompt` 本身不抛，POST 应该 200。如果 500 多半是测试 fixture 没准备 FakeClient。

### MCP test connection 测试失败

`tests/fixtures/fake_mcp_stdio_server.py` 是 fixture。检查：
- `sys.executable` 在 conda 环境里指向 pipy 的 python
- fixture 文件路径相对 `tests/test_web_mcp_api.py`（`Path(__file__).parent / "fixtures" / ...`）
- Windows 下子进程启动可能慢；test 默认 timeout 10s（`MCPServerConfig.timeout_s`）

### Skills upload 测试 422

`harness.skill_registry is None` → 调 `/api/skills/upload` 前必须先 `harness.attach_skills([])` 创建空 registry。

---

## Browser smoke tests / Playwright（新增 2026-07-07）

`tests/e2e/` 目录下的 Playwright 测试覆盖 Web Claude P0 MVP 的真实浏览器路径。

### 范围

- **5 个核心 smoke 通过**：layout / chat / file upload / image unsupported / modals + env non-echo
- **1 个 optional skip**：Skill upload + Use this turn（`test.describe.skip`，时序问题待修）
- 真实 Chromium 启动；后端用 `start_test_web_app.py`（FakeClient，**不依赖**真实 GLM / MCP）
- webServer 自动启动 Python 后端，复用已存在的 server

### 首次运行

```bash
# 1. 前端必须先 build（webServer 启动的 Python 后端托管 ../static/）
cd src/pi_agent_core_py/web/frontend
npm install && npm run build

# 2. 装 Playwright + Chromium（一次性）
cd ../../../tests/e2e
npm install
npx playwright install chromium

# 3. 跑 e2e（webServer 会自动启动 Python 后端）
npm run test:e2e
```

### 常用命令

```bash
cd tests/e2e

# 默认（headless）
npm run test:e2e

# 有头模式（看浏览器操作）
npm run test:e2e:headed

# Debug 模式（Playwright Inspector）
npm run test:e2e:debug

# 显式指定 Python 解释器（默认 D:/miniconda/envs/pipy/python.exe；Linux/macOS 需要覆盖）
E2E_PYTHON=python npm run test:e2e

# 显式指定 baseURL（如果 8000 被占，可在 start_test_web_app.py 里 PORT=xxxx 改）
E2E_BASE_URL=http://127.0.0.1:9000 npm run test:e2e
```

### 当前覆盖的 smoke 用例

| # | 用例 | 关键断言 |
|---|---|---|
| 1 | 两栏 UI | sidebar + chat-panel + new-chat + skills/mcp button 存在；body 文本不含 "Trace Viewer" / "DeveloperDrawer" / "Raw JSON" / "Event Stream" / "Policy Audit" |
| 2 | 新建 session + 普通消息 | 输入 "hello from playwright" → Send → user-message 可见 → assistant-message 含 FakeClient 文本 "hello from fake backend" 可见；无 error-card |
| 3 | 文件上传 md | setInputFiles(sample.md) → attachment-bar FileChip → Send → user-message 内仍显示 FileChip → attachment-bar 清空 |
| 4 | 图片 unsupported | setInputFiles(sample.png, 1x1 PNG) → chip 出现 + label 含 "unsupported" |
| 5 | Skills/MCP Modal + env 不回显 | Skills modal 打开关闭 → MCP modal 打开 → 填 name/command/env (SECRET_KEY + super-secret-value-zzz-12345) → 提交前后 body innerText 都不含 secret value → server list 显示 "SECRET_KEY" 但不含 value |
| 6 | Skill upload + Use this turn | 上传 SKILL.md → skill-card 出现 → 勾选 Use this turn → 关闭 modal → 发 prompt → SkillUsedCard 出现 |
| 7 | drag-drop 上传 | DataTransfer + dispatchEvent('drop') 模拟真实拖放 → attachment-bar FileChip 出现 |
| 8 | Stop 按钮 | 发送瞬间 Send 变 Stop（FakeClient 太快可能转瞬即逝）；最终 Send 恢复 |
| 9a | session rename | hover session → 点 ✎ → window.prompt 接受 "renamed by e2e" → session 标题更新 |
| 9b | session delete | hover session → 点 × → window.confirm 接受 → session 数量减 1 |

### 失败诊断

测试失败时 Playwright 自动保留 trace / screenshot / video：

```bash
# 看最近一次失败的 trace
npx playwright show-trace test-results/<failed-test-name>/trace.zip

# 看 screenshot 直接打开 PNG
test-results/<failed-test-name>/test-failed-1.png

# 看 video
test-results/<failed-test-name>/video.webm
```

### 当前不覆盖（边界）

- ❌ 真实 GLM（FakeClient 提供确定性文本）
- ❌ 真实 MCP server（MCP 测试只测 form UI + env 防回显；MCP tool enable 真链路需要 fake stdio subprocess，flaky 风险高，本轮跳过——后续 P1 加）
- ❌ 真实 abort 链路（FakeClient 完成太快，Stop 按钮只测 UI 状态切换）
- ❌ Cross-browser（仅 Chromium）
- ❌ Mobile / 视口矩阵
- ❌ Visual regression / 性能测试
- ❌ Multi-user / 多 session 并发

### webServer 配置说明

`tests/e2e/playwright.config.ts` 的 webServer 段：

```ts
webServer: {
  command: `${process.env.E2E_PYTHON ?? "D:/miniconda/envs/pipy/python.exe"} start_test_web_app.py`,
  cwd: __dirname,
  url: "http://127.0.0.1:8000/api/state",
  reuseExistingServer: !process.env.CI,
  timeout: 60_000,
}
```

- 默认 Windows 下用 conda pipy 的 python（CLAUDE.md 注明路径）
- Linux/macOS 需 `E2E_PYTHON=python npm run test:e2e`
- `reuseExistingServer: !CI`——本地调试时复用已启动的 server；CI 每次新启
- start_test_web_app.py 用 FakeClient + 临时 sqlite + 临时 uploads_dir；不污染主仓库

---

## 已知限制

- `POST /api/prompt` 同步阻塞——慢 LLM 会让请求挂住；前端通过 WS 看实时事件，但 POST 仍要等返回
- 无鉴权 / 无多用户——仅 localhost
- MCP / Skill 配置不持久化——重启即丢
- 没有浏览器端 e2e 自动化测试——P0 仅手动 smoke checklist
- WebSocket 不做 event_id 去重 / 重连补播（v0.0.22 已知限制沿用）
- **Playwright Smoke 6（Skill upload + Use this turn）当前 skip**——E2E 下 @change.prevent 时序问题；frontend unit / 后端 test_web_skills_api.py 已覆盖该路径


## fixture 设计

### `web_client`

最常用——单 harness / app / TestClient，结束自动 dispose。

```python
@pytest.fixture
def web_client():
    harness = _make_harness()
    app = create_app(harness)
    client = TestClient(app)
    try:
        yield client, harness, app
    finally:
        client.close()
        dispose_app(app)
```

`client.close()` 会触发 FastAPI lifespan 的 shutdown 钩子，自动 `remove_on_event_hook`。
`dispose_app(app)` 兜底——不进 `with TestClient(...)` 的场景必备。

### `web_client_with_skill`

带 skill + `allow_prompt_preview=True`。供 prompt preview 正向测试用。

---

## `dispose_app(app)` 使用场景

`create_app(harness)` 会把 `_web_event_hook` 通过 `harness.add_on_event_hook(...)` 永久加入 `harness.on_event_hooks`。FastAPI lifespan 的 shutdown 钩子会清理——但**只在 TestClient / uvicorn 进入了 lifespan 时**才触发。

下面这些场景需要手动调 `dispose_app(app)`：

```python
# 1. 直接构造 app 不进 with —— lifespan 不跑
app = create_app(harness)
try:
    # 断言...
    assert app.state.web.event_buffer.max_size == 10
finally:
    dispose_app(app)

# 2. 同一 fixture 多次 create_app 测试 hook 行为
def test_xxx():
    harness = _make_harness()
    apps = [create_app(harness) for _ in range(3)]
    try:
        assert len(harness.on_event_hooks) == 3
    finally:
        for app in apps:
            dispose_app(app)
    assert len(harness.on_event_hooks) == 0
```

`dispose_app(app)` 内部：

```python
def dispose_app(app: FastAPI) -> None:
    hook = getattr(app.state, "web_event_hook", None)
    harness = getattr(getattr(app.state, "web", None), "harness", None)
    if hook is not None and harness is not None:
        harness.remove_on_event_hook(hook)
        app.state.web_event_hook = None
```

幂等——多次调用不抛错。

---

## `event_buffer_max_size` 如何设置

默认 `maxlen=1000`（`TraceEventBuffer` 默认）。两种覆盖方式：

```python
# 方式 1：create_app 入参
app = create_app(harness, event_buffer_max_size=500)

# 方式 2：直接换 buffer 实例（不推荐，绕过 create_app 校验）
state = app.state.web
state.event_buffer = TraceEventBuffer(max_size=500)
```

超过 `max_size` 时 deque 自动丢最旧（这是 `collections.deque(maxlen=N)` 的内建行为）。

测试断言参考 `test_event_buffer_drops_old_when_over_maxlen`：

```python
buf = TraceEventBuffer(max_size=3)
for i in range(5):
    buf.append({"i": i})
events = buf.list()
assert len(events) == 3
assert [e["i"] for e in events] == [2, 3, 4]
```

---

## SSE limit 测试技巧

### `?limit=0` —— 立即关闭

最简单：连接后 hello 一发出就关闭 stream。

```python
with client.stream("GET", "/api/stream?limit=0", timeout=10.0) as resp:
    assert resp.status_code == 200
    for line in resp.iter_lines():
        chunks.append(line)
```

### `?limit=1` —— 配合 background thread POST

SSE 不会主动产生 event——需要后台线程 POST `/api/prompt` 让 hook 广播。

```python
import threading

def trigger() -> None:
    time.sleep(0.5)  # 给 SSE 时间连上 hook 广播
    client.post("/api/prompt", json={"text": "trigger"})

threading.Thread(target=trigger, daemon=True).start()

with client.stream("GET", "/api/stream?limit=1", timeout=20.0) as resp:
    for line in resp.iter_lines():
        chunks.append(line)
```

TestClient 是同步阻塞——SSE 必须用 background thread 触发 prompt。

---

## uvicorn subprocess 端口 retry

`_pick_free_port()` 用 `bind(127.0.0.1, 0)` 取空闲端口；释放后子进程 bind 时可能被抢。测试加 3 次 retry：

```python
for attempt in range(1, max_attempts + 1):
    port = _pick_free_port()
    launcher = tmp_path / f"launcher_{attempt}.py"
    launcher.write_text(...)
    proc = subprocess.Popen([sys.executable, str(launcher)], ...)
    try:
        # 等端口就绪 5s
        # 端口被抢 / launcher import 失败 → break 重试
    finally:
        proc.terminate()
        proc.wait(timeout=5)
```

失败时 `pytest.fail` 打印最近一次 stderr 帮助诊断。

---

## 常见问题

### 测试挂住不退出

99% 是 SSE 无限流。两个修复：

1. 加 `?limit=N`：`/api/stream?limit=0` 立即关
2. 加 `timeout=N.0`：`client.stream(..., timeout=10.0)`

### hook 累积导致事件数翻倍

检查 `web_client` fixture 退出时是否调了 `dispose_app(app)` 或 `client.close()`。`client.close()` 会触发 lifespan shutdown；`dispose_app` 是显式兜底。

### WebSocket 测试报 `WebSocketDisconnect`

TestClient 的 WS 用 `with client.websocket_connect("/ws/events") as ws:`。连接外 send 会抛；包在 with 内即可。

### POST `/api/prompt` 一直返回 500

检查 `state.last_error`。常见原因：FakeClient scripts 用完 → 模型 ErrorEvent → loop 包成 `stop_reason="error"` assistant → 但 `harness.run_prompt` 本身不抛，POST 应该 200。如果 500 多半是测试 fixture 没准备 FakeClient。
