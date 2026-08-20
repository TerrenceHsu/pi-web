# pi-agent-core-py

`@earendil-works/pi-agent-core` 的 Python 移植与本地 Web Agent 工作台。项目包含事件驱动 Agent Runtime、工具与 MCP、Skills、Provider 切换、Session 工作区、Knowledge/RAG、审批、上下文预算和可恢复的浏览器聊天界面。

> 当前代码事实以 [`STATUS.md`](STATUS.md) 为准；未完成事项只维护在 [`TODO.md`](TODO.md)。本项目是 **localhost-only** 本地开发工具，不是公网 SaaS。

## 当前能力概览

### Agent Runtime

- 异步流式 LLM 调用、ToolCall、并行/顺序工具批次、abort signal 和事件订阅
- 一个 Turn 严格等于“一次 LLM 调用 + 该调用产生的当批工具”
- `RequestSnapshot` 保存一次请求，内部 `TurnSnapshot[]` 保存真实 LLM 轮次
- Tool、before hook、after hook 接收协作式 `signal`
- 独立 steering / follow-up 控制队列，支持 `all` / `one-at-a-time` 消费模式
- 全局 `tool_execution` 可强制批次串行，逐工具 execution mode 可进一步收紧
- `tool_execution_update` 支持工具执行中的增量状态
- `prepare_next_turn` 与 `should_stop_after_turn` 提供 Turn 边界控制
- `length + tool_calls`、非法工具参数和工具异常统一转为安全 ToolResult，不让 loop 崩溃
- ToolResult 保留工具自身 usage 与 deferred-tool 加载点元数据，并贯穿事件、快照和持久化
- text/thinking/tool-call 提供完整 start/delta/end 生命周期；thinking 保留 provider signature/redacted payload

### Web 工作台

- 本地账号登录；每账号独立 Session、文件、Skills、MCP、Knowledge 和 Provider 配置
- `/chat/{session_id}` 路由；整页刷新恢复 Session、历史、文件树及当前进程中的 active request
- Prompt、Stop、Regenerate 最新 Assistant、Markdown Export、SSE/WebSocket 实时事件
- Human Approval：高风险 ToolCall 在当前 Turn 内暂停，支持 Approve once / Deny
- Context Budget：完整输入估算、70/85/95% 分级、hard stop、手动 Turn-safe compaction
- Assistant Markdown 渲染、usage、总 latency 与 TTFT 展示
- ToolCall/ToolResult 卡片保持原始跨轮顺序；MCP stdio 端到端严格 UTF-8

### Session 工作区与记忆

- 对话历史以 append-only entry tree 持久化；命名 lane 支持 fork、branch、label 与重启后 active leaf 恢复
- lane operation 使用 append-only intent/effect/finish records；Checkpointer 可在进程退出后凭 source leaf/hash 幂等前滚
- 当前 active lane 会物化为兼容消息视图，因此现有聊天、Regenerate、Export 与 Context 工具无需理解树结构
- 每个 Session 初始化独立目录和唯一根 `AGENT.md`
- 用户上传文件保存在当前 Session；Agent 可调用 `list_files`、`view_file`、`write_file`
- 文件树可查看、下载、删除和刷新；`AGENT.md`、`Memory.md` 可用 SHA-256 乐观锁编辑
- `/checkpointer` 使用当前 Session 绑定的 LLM 总结对话到累计 `Memory.md`；文件以 immutable generation 原子发布，成功后在同一 SQLite 事务清空原 lane 并完成 operation
- 重新登录或重启后恢复同账号的 Session、历史消息、受管文件和配置

### Provider 与凭证

- Provider adapters：GLM/Anthropic-compatible 与 OpenAI-compatible；Web UI 管理 GLM、Qwen、Kimi Profile
- Provider Profile、Model、Session binding、Context Window、Max Output Tokens 持久化
- API Key 可存 OS Keyring、session-only memory 或显式环境变量；不写入 SQLite 明文
- 开发启动器默认要求持久化 Keyring，并在监听端口前执行非敏感 write/read/delete 探针
- 未配置可用 Profile 时，开发启动器保留 delayed FakeClient 作为本地 UI fallback；真实回答需要在 Providers 中保存并绑定真实 Profile

### MCP 与网络搜索

- MCP client 支持 stdio tools/prompts、server 生命周期、工具 enable/disable 和持久化恢复
- 内置 `ddgs` MCP 随开发工作区固定创建且不可删除
- DDGS 前端可调整返回数、地区、安全搜索、时间范围、后端等参数
- MCP 子进程强制 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`；非法 UTF-8 作为协议错误拒绝
- HTTP MCP transport 仍是 placeholder；当前生产可用 transport 为 stdio

### Knowledge / RAG

- Knowledge Library/Document 管理、Session binding 和账号隔离
- PDF 流式上传、文本提取、Canonical Markdown、heading-aware chunk、SQLite FTS5/BM25
- 后台 ingestion/indexing worker、重启恢复、状态/重试/delete guard
- `search_knowledge` Agent Tool 按当前 Session Library ACL 检索
- Assistant 中的 `[cite:E1]` 转为稳定编号和 Sources footer
- Knowledge Manager UI 支持上传、状态轮询、Markdown 查看和搜索

Knowledge 是纯 FTS5 路线，不使用 embedding、向量数据库或外部模型下载。扫描 PDF 进入 `needs_ocr`，当前不做 OCR。

## 关键数据边界

Session 文件和 Knowledge 文档用途不同：

| 能力 | Session Folder | Knowledge Library |
|---|---|---|
| 作用域 | 单个 Session | 账号内 Library，可绑定多个 Session |
| 输入 | 任意受配额文件 | PDF |
| Agent 读取 | 文本/Markdown/HTML/CSV/Parquet；PDF 仅元信息 | `search_knowledge` 返回索引片段 |
| 持久化 | `uploads/{session_id}/` + workspace metadata | `knowledge.db` + `knowledge/libraries/` |
| 图片/OCR | 不支持 | 扫描 PDF → `needs_ocr` |

`AGENT.md` 是当前 Session 的行为指令；`Memory.md` 是当前 Session 的对话摘要。两者都不是跨 Session 用户画像，也不能覆盖平台安全规则。

## 技术栈

| 层 | 技术 |
|---|---|
| Runtime | Python >=3.11、asyncio/anyio、Pydantic v2 |
| LLM | anthropic SDK、openai SDK、httpx |
| Web 后端 | FastAPI、uvicorn、WebSocket/SSE |
| Web 前端 | Vue 3、Pinia、Vite、TypeScript |
| 持久化 | aiosqlite、OS Keyring、受管文件目录 |
| Knowledge | pypdf、SQLite FTS5/BM25 |
| MCP 搜索 | ddgs 9.x，stdio JSON-RPC |
| 测试 | pytest/pytest-asyncio、Vitest、Playwright |

## 快速开始

以下命令针对当前 Windows 工作区和项目约定。Python 必须使用：

```powershell
D:\miniconda\envs\pipy\python.exe
```

### 1. 安装 Python 与前端依赖

```powershell
Set-Location D:\LLMTutorial\test
D:\miniconda\envs\pipy\python.exe -m pip install -e ".[dev,web,rag]"
npm --prefix src/pi_agent_core_py/web/frontend ci
```

### 2. 启动后端

必须从当前 Windows 交互式登录用户会话启动，确保 Credential Manager 可用：

```powershell
Set-Location D:\LLMTutorial\test
$env:PYTHONPATH = "src"
D:\miniconda\envs\pipy\python.exe scripts/dev_web_app.py
```

默认地址：`http://127.0.0.1:8000`。默认数据目录：`.pi-agent-data/`。

开发启动器默认 `PI_AGENT_SECRET_BACKEND=keyring`。Keyring 探针失败时会在监听端口前退出；如果明确接受 API Key 不持久化，可临时使用：

```powershell
$env:PI_AGENT_SECRET_BACKEND = "memory"
```

该模式不会把 Key 自动降级写入 SQLite。

### 3. 启动前端开发服务器

另开一个 PowerShell：

```powershell
Set-Location D:\LLMTutorial\test
npm --prefix src/pi_agent_core_py/web/frontend run dev
```

打开 `http://127.0.0.1:5173/`。Vite 会把 `/api` 和 `/ws` 代理到后端 8000。

### 4. 登录与配置真实模型

空认证库首次启动会创建：

```text
Username: admin
Password: 123456
```

登录后：

1. 打开左侧 `Providers`。
2. 为 GLM、Qwen 或 Kimi 创建 Profile，填写 Model ID 与 API Key。
3. 保存后将 Profile 设为当前 Session binding。
4. 新建或选择 Session，再发送消息。

当前没有 Users 管理模块。初始凭据只适合 localhost 本地开发。

### 5. 由 FastAPI 托管构建后的前端

```powershell
npm --prefix src/pi_agent_core_py/web/frontend run build
$env:PYTHONPATH = "src"
D:\miniconda\envs\pipy\python.exe scripts/dev_web_app.py
```

然后访问 `http://127.0.0.1:8000/`。

## 常用工作流

1. `+ New chat` 创建 Session；URL 自动切换为 `/chat/{session_id}`。
2. `Folder` 查看 Session 文件树并编辑 `AGENT.md`。
3. 拖放或选择文件上传；Agent 在下一轮可用文件工具读取。
4. 输入 `/checkpointer` 生成或累计更新 `Memory.md`。
5. `Skills` 上传并启用 `SKILL.md`，按 Turn 选择使用。
6. `Knowledge` 创建 Library、上传 PDF、等待 ready、绑定当前 Session。
7. `MCP` 配置自定义 stdio server；内置 DDGS 参数可直接编辑。
8. `Providers` 管理凭证/Profile/模型窗口，并切换当前 Session binding。
9. 对最新 Assistant 使用 Regenerate；从 Session 菜单导出 Markdown。
10. 遇到高风险工具，在内联 Approval Card 中选择 Approve once 或 Deny。

## 持久化布局

`scripts/dev_web_app.py` 默认使用：

```text
.pi-agent-data/
├── auth.sqlite
└── users/
    └── {user_id}/
        ├── workspace.sqlite
        ├── uploads/
        │   └── {session_id}/
        │       ├── AGENT.md
        │       ├── Memory.md        # 首次 checkpointer 后存在
        │       └── ...
        └── knowledge/
            ├── knowledge.db
            └── libraries/
```

可通过 `PI_AGENT_DATA_DIR` 修改根目录。后端重启会撤销旧登录 Session，但不会删除账号或工作区数据。

## API 与事件

完整接口见 [`docs/api/web-api.md`](docs/api/web-api.md)。主要分组：

- `/api/auth/*`：登录、恢复身份、退出
- `/api/sessions*`：Session CRUD、append-only tree/lane、消息、Regenerate、Export、Context Budget/Compaction
- `/api/sessions/{sid}/files*`：上传、文件树、下载、受管文本编辑
- `/api/slash-commands`：命令目录与 `/checkpointer`
- `/api/requests*`：active request、abort、approval
- `/api/provider-*`、`/api/credentials`：Provider/Profile/Binding/Credential
- `/api/mcp/*`、`/api/skills/*`：MCP 与 Skills
- `/api/knowledge/*`：Library、Document、PDF ingestion、search、Session binding
- `/api/stream` 与 `/ws/events`：SSE/WebSocket 实时事件

所有工作区 API 均受登录网关保护；未认证 HTTP 返回 401，WebSocket 关闭码为 4401。

Compaction 默认只在完整 turn 边界切分，使用与 preflight 相同的 token estimator，
并支持前序摘要折叠及瞬时 Provider 错误的显式重试策略。契约见
[`docs/COMPACTION_SEMANTICS.md`](docs/COMPACTION_SEMANTICS.md)。

## 测试

当前代码基线的准确数字见 [`STATUS.md`](STATUS.md#2026-08-20-当前验证基线)。2026-08-20 校准结果：Backend **3665 passed / 6 skipped / 12 deselected**、coverage **83.78%**，Ruff/Mypy 全绿；Frontend lint/typecheck/build 全绿，Vitest **394/394**；完整 Playwright **45/45** 与真实 DDGS + GLM **3/3** 沿用最近发布前门禁结果。

### Backend offline

```powershell
Set-Location D:\LLMTutorial\test
New-Item -ItemType Directory -Force .test-tmp/pytest | Out-Null
$env:PYTHONPATH = "src"
D:\miniconda\envs\pipy\python.exe -m pytest tests -q --no-cov --basetemp=.test-tmp/pytest
```

默认 marker 排除 `slow`、`integration` 和 `docker`，不会调用真实 LLM、外部 DDGS 或 Docker。

### DDGS + GLM 真实 smoke

必须从当前 Windows 交互式登录用户会话运行，确保真实网络和 Credential Manager
都可用：

```powershell
Set-Location D:\LLMTutorial\test
D:\miniconda\envs\pipy\python.exe scripts/run_live_integration_tests.py
```

固定脚本只运行 1 个 DDGS 与 2 个 GLM smoke，并自动设置真实测试门禁。
即使手工把 pytest marker 改为 `-m integration`，缺少
`PI_RUN_INTEGRATION=1` 时仍会 skip，不会误访问真实服务。

GLM 配置优先级为：显式 `PI_AGENT_TEST_GLM_*` → 当前 Web 默认 GLM
Profile/Keyring → 当前进程已有的 GLM/Anthropic 环境变量。测试不会自动加载根
`.env`，因此旧 `.env` API Key 不会再覆盖 Web 中实际可用的凭证。多工作区时可用
`PI_AGENT_TEST_WORKSPACE_DB` 显式指定 `workspace.sqlite`。

### Frontend

```powershell
npm --prefix src/pi_agent_core_py/web/frontend test
npm --prefix src/pi_agent_core_py/web/frontend run typecheck
npm --prefix src/pi_agent_core_py/web/frontend run lint
npm --prefix src/pi_agent_core_py/web/frontend run build
```

### Browser E2E

Playwright 测试位于 `tests/e2e/`，覆盖基础聊天、异步流、刷新路由、Regenerate、MCP、Approval、Context Compaction 与扩展持久化。运行说明见 [`docs/guides/web-testing.md`](docs/guides/web-testing.md)。

当前发布前基线为 Chromium 单 worker **45/45 passed**；全套件使用独立端口与 FakeClient，不访问真实 Provider。

## 安全边界

- 只绑定 localhost；不要直接暴露公网
- 密码保存为 PBKDF2-SHA256 + 随机盐；登录 token 服务端只保存 SHA-256
- Cookie 为 HttpOnly + SameSite=Strict
- API Key 不在 API response、日志、SQLite main/WAL/SHM 中回显或明文持久化
- MCP env values 不回显；配置后前端立即清空输入
- Prompt preview 默认关闭；开发启动器只对受信任本地 origin 开启
- MCP command、Session 文件和 Knowledge 文档均视为不可信输入
- Pending approval 与 active request 是进程内状态，不是永久授权或审计数据库

## 当前限制

- 单账号单 harness、单 active request；没有并行 Session 生成
- Context estimator 不是 Provider 官方 tokenizer；compaction 默认手动、规则式
- Regenerate 只支持最新 Assistant；没有 revision history UI
- Session Folder 不解析 PDF 正文；PDF RAG 必须通过 Knowledge Library 上传
- 无 OCR、图片理解、向量检索、Multi-Agent、RBAC/OAuth 或公网部署
- MCP HTTP transport 未实现；只支持 stdio
- 历史数据中已经存在的 `U+FFFD` 无法自动恢复原字符
- 当前 package baseline 为 `0.0.28`，Python、FastAPI/Auth 与前端版本已统一；Git remote 仍待配置

## 项目结构

```text
.
├── src/pi_agent_core_py/
│   ├── agent.py / loop.py / harness.py / snapshot.py
│   ├── providers/              # GLM / Anthropic / OpenAI-compatible
│   ├── mcp/                    # MCP client、transport、DDGS server
│   ├── policy/                 # permission 与 sandbox helpers
│   ├── tools/                  # list/view/write/web search
│   └── web/
│       ├── app.py              # 工作区 FastAPI composition root
│       ├── auth/               # 本地账号与登录网关
│       ├── credentials/        # Credential metadata/runtime
│       ├── providers/          # Profile/Binding/runtime
│       ├── knowledge/          # PDF ingestion、FTS5、citation
│       └── frontend/           # Vue 3/Vite/Pinia
├── scripts/dev_web_app.py
├── tests/
├── tests/e2e/
├── docs/
└── steps/
```

## 文档职责

- [`STATUS.md`](STATUS.md)：唯一当前事实与验证快照
- [`TODO.md`](TODO.md)：唯一未完成事项清单
- [`ROADMAP.md`](ROADMAP.md)：未来方向与历史路线
- [`CHANGELOG.md`](CHANGELOG.md)：release 历史
- `docs/validation/`：阶段性冻结证据；其中的历史测试数字不代表当前 HEAD
- [`CLAUDE.md`](CLAUDE.md)：本仓库协作和运行约束

## License

本项目采用 MIT License，完整条款见 [`LICENSE`](LICENSE)。Python wheel 也携带同一许可证文件。
