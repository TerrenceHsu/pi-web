# Project Status

> 当前事实快照，校准日期：**2026-08-20**。本页只描述当前代码基线；阶段性测试数字和历史决策保留在 `docs/validation/`、`CHANGELOG.md` 与归档计划中。

## 基线身份

| 项 | 当前事实 |
|---|---|
| 代码基线 | `b6baea8` — `feat(tools): preserve result usage metadata` |
| 分支 | `master` |
| 最新 release tag | `v0.0.27-secure-credentials` @ `de05c66`；当前代码基线尚未打新 tag |
| Python / API 版本 | `0.0.28`（Python `__version__`、workspace FastAPI 与 Auth gateway 共用同一来源） |
| 前端包版本 | `0.0.28`（`package.json` 与 lockfile 一致） |
| 许可证 | MIT；根 `LICENSE` 为标准正文，`pyproject.toml` 与 wheel 均直接引用/携带该文件 |
| Git remote | 当前仓库**未配置 remote**，因此尚无可执行的 push 目标 |
| Python | 声明支持 `>=3.11`；本机验证使用 Python 3.12.13（conda `pipy`） |
| 产品边界 | localhost-only 本地 Agent 工作台；不是公网 SaaS |

> `0.0.28` 是当前尚未打 tag 的 package baseline；最新既有 tag 仍是旧基线 `v0.0.27-secure-credentials`，不能据此声称已经发布 `0.0.28`。

## 当前交付状态

当前没有正在实施且未完成的产品阶段。以下能力均已进入 `master`：

| 能力 | 状态 | 代表性基线 |
|---|---|---|
| Step 1–21 Core Runtime | ✅ 完成 | 归档见 `docs/archive/legacy-plans/PLAN_STEP_1_21.md` |
| Multi-provider Runtime / UI | ✅ 完成 | GLM、Qwen、Kimi；Anthropic-compatible 后端能力 |
| Knowledge PDF → Markdown → FTS5 → Citation | ✅ 完成 | P2-R4 final `7faf635`；P2-R5 final `6527be5` |
| 登录与账号工作区隔离 | ✅ 完成 | `b529bbc` |
| Session Folder、`AGENT.md`、`Memory.md`、`/checkpointer` | ✅ 完成 | `b529bbc` |
| P0 Runtime 上游契约对齐 | ✅ 完成 | `b529bbc` |
| Session URL 与整页刷新恢复 | ✅ 完成 | `f30da56` |
| Human Approval + Context Budget/Compaction UI | ✅ 完成 | `924b047` |
| B7 SQLite 初始化失败资源清理 | ✅ 完成 | `688cf08` |
| Keyring 真实可写预检与启动 fail-fast | ✅ 完成 | `ada31fc` |
| ToolResult 顺序与 MCP UTF-8 | ✅ 完成 | `e7bf8f3` |
| Agent 语义正确性与控制队列 | ✅ 完成 | `c214d28`；串行 preflight、终态收敛、steering/follow-up、全局 tool execution |
| Thinking 与细粒度流生命周期 | ✅ 完成 | `c214d28`；text/thinking/tool-call start/delta/end |
| Agent 公开运行时状态 | ✅ 完成 | `848ae1d`；model / thinking level / streaming message / pending tool calls / error message |
| ToolResult usage 与 deferred-tool metadata | ✅ 完成 | `b6baea8`；事件、LLM 边界、Snapshot、Session/SQLite 与 Web JSON 全链路保留 |
| 全仓 Ruff / strict Mypy / CI 收敛 | ✅ 完成 | Ruff 0；Mypy 114 files / 0 issues；Python CI timeout 30 分钟 |
| Release metadata 与 MIT License | ✅ 完成 | `8a6ff2e`；Python/API/前端统一 `0.0.28`，wheel 携带根许可证 |
| 当前发布前浏览器/联网门禁 | ✅ 完成 | `51ce3c7`；Playwright 45/45，DDGS + GLM 真实 smoke 3/3 |

## 当前产品能力

- 未登录先进入 Username/Password 页面；空认证库幂等创建本地初始账号 `admin / 123456`
- 每个账号拥有独立的 Session、消息、文件、Skills、MCP、Knowledge、Provider/Credential 配置
- Session 路由为 `/chat/{session_id}`；刷新恢复准确 Session、历史、文件树、`AGENT.md`、`Memory.md` 及当前请求状态
- 每个 Session 初始化独立文件夹和唯一根 `AGENT.md`；Agent 可 `list_files`、`view_file`、`write_file`
- `/checkpointer` 使用当前 Session Provider 把对话累计总结到 `Memory.md`，成功后清空当前消息窗口
- 支持 Prompt、Stop、Regenerate 最新 Assistant、Markdown Export、实时事件、请求恢复和精确 ToolCall 审批
- Provider Profile、Session Model Binding、Context Window 与 Max Output Tokens 持久化；UI 管理 GLM/Qwen/Kimi
- 持久化凭证默认进入 OS Keyring；开发启动器在监听端口前执行 write/read/delete 探针
- MCP 支持 stdio tools/prompts；内置 DDGS 固定存在、不可删除，可修改返回数、地区、安全搜索、时间范围等参数
- Knowledge Manager 支持 Library、PDF 上传、后台解析/索引、Session binding、FTS5 搜索和 Agent citation
- Core Runtime 的一个 Turn 等于“一次 LLM 调用 + 该调用产生的当批工具”；Snapshot 分为 RequestSnapshot 与 TurnSnapshot
- 并行工具批次先按源序串行完成 hook、权限、审批与参数校验，再并行执行已放行工具；hook 不得改写 tool-call ID
- Agent 支持独立 steering / follow-up 队列及 `all` / `one-at-a-time` 消费模式；活跃请求期间普通 prompt/continue 明确拒绝
- 全局 `tool_execution` 可强制批次串行；逐工具 `execution_mode="sequential"` 可在并行全局模式下收紧执行
- Assistant thinking/reasoning 内容可保留 provider signature/redacted payload；text/thinking/tool-call 均有完整 start/delta/end 流事件
- `AgentState` 公开 secret-free model 身份、thinking level、请求流状态、当前 partial message、执行中 tool-call ID 与最近 assistant error；Web `/api/state` 使用同一事实源
- ToolResult 可携带工具自身 usage 与 `added_tool_names`；usage 不并入主 LLM 上下文计费，added names 只标记 `Context.tools` 的 provider 加载点且不能由 after hook 伪造

## 数据与生命周期

默认开发数据根目录为 `.pi-agent-data/`：

| 数据 | 默认位置/生命周期 |
|---|---|
| 账号与登录 Session | `.pi-agent-data/auth.sqlite`；账号保留，登录 Session 在后端重启时撤销 |
| 账号工作区 | `.pi-agent-data/users/{user_id}/` |
| Session/消息/Skills/MCP/Provider metadata | 用户目录下 `workspace.sqlite` |
| Session 文件 | 用户目录下 `uploads/{session_id}/` |
| Knowledge | 用户目录下 `knowledge/knowledge.db` 与 `knowledge/libraries/` |
| API Key | OS Keyring、显式 session-only memory 或显式 env；不写入 SQLite 明文 |
| Active request / pending approval / event subscribers | 当前后端进程内存；后端重启不恢复执行 |

## 2026-08-20 当前验证基线

Backend 全量数字基于 `b6baea8` 实际复跑；版本/许可证提交 `8a6ff2e`
另行通过 wheel 元数据验证；`51ce3c7` 的等价内容通过完整 Playwright 与最终
真实网络 smoke：

| 验证 | 结果 | 备注 |
|---|---|---|
| Ruff 全量 | **PASS** | `ruff check src tests`；0 errors |
| strict Mypy 全量 | **PASS** | `mypy src/pi_agent_core_py`；114 files / 0 issues |
| Backend CI 全量 + coverage | **3665 passed, 6 skipped, 12 deselected** | `pytest tests -m "not slow" --tb=short -q`；83.78% coverage；1028.16s；58 warnings；工作区 `basetemp` + cacheprovider disabled |
| ToolResult metadata 定向回归 | **173 passed** | usage / added names、hook 边界、消息/事件、provider context、Snapshot、Session/SQLite、Web serializer 与旧数据默认值 |
| Agent 公开状态定向回归 | **137 passed** | Agent、Harness、stream、Provider runtime 与 Web state；系统 temp ACL 阻断项改用工作区 `basetemp` 后通过 |
| DDGS + GLM 真实 smoke | **3/3 passed** | 固定 secret-safe 脚本；DDGS 1 项 + GLM 2 项；24.63s；未输出凭证 |
| 真实测试门禁回归 | **3 skipped** | 手工选择 `-m integration` 但未设置 `PI_RUN_INTEGRATION=1`，确认不触网 |
| Frontend Vitest 全量 | **394/394 passed** | 26 files；2026-08-20 实际复跑 |
| Frontend typecheck | **PASS** | `vue-tsc --noEmit` |
| Frontend ESLint | **PASS** | `eslint . --max-warnings=0` |
| Frontend production build | **PASS** | `vite build`；仅既有 mixed dynamic/static import warning |
| 版本/许可证一致性回归 | **3 passed** | Python、两个 FastAPI、前端 package/lockfile 与根 LICENSE 元数据一致 |
| Python wheel 构建 | **PASS** | `pi_agent_core_py-0.0.28-py3-none-any.whl`；METADATA 与归档内 LICENSE 均已核验 |
| B7 定向回归 | **248/248 passed** | SQLite Store lifecycle/open failure |
| Keyring 定向回归 | **94/94 passed** | Runtime、launcher 与 restart 范围 |
| 真实 Windows Keyring 探针 | **write/read = true；cleanup = true** | 随机非用户值，执行后删除 |
| MCP/DDGS UTF-8 定向回归 | **34 passed, 1 deselected** | 含真实 Python 子进程中文 round-trip |
| Browser E2E | **45/45 passed** | Chromium；单 worker；`CI=1`；独立端口 8012；2.5m；0 retry / 0 failure |

默认 pytest marker 排除真实 LLM、真实外网 integration 和 Docker；额外门禁还要求
`PI_RUN_INTEGRATION=1`。因此 Offline Backend 基线不依赖 API Key、DDGS 网络或
Docker；真实 smoke 必须通过 `scripts/run_live_integration_tests.py` 在当前 Windows
交互式会话运行。

## 已知限制与债务

### 安全与部署

- 仅面向 localhost；没有 TLS、RBAC、OAuth、企业多租户或横向扩展
- 初始 `admin / 123456` 仅适合本地首次启动；当前没有 Users 管理模块或强制改密流程
- OS Keyring 安全性依赖宿主系统；`session_only` 在进程退出后按设计丢失
- MCP stdio command 是本地代码执行能力，只应配置可信命令

### Runtime / Web

- 每账号单 harness、单 active request；不支持同账号多个 Session 并行生成
- active request 与 pending approval 不跨后端重启恢复；浏览器刷新只恢复仍在当前进程运行的请求
- Human Approval 只有 Approve once / Deny；没有永久授权
- Context Budget 是带安全余量的确定性近似，不是 Provider 官方 tokenizer
- Context compaction 由用户手动触发，默认摘要器为本地规则式；`/checkpointer` 才调用当前 LLM
- Regenerate 仅支持最新 Assistant，不提供历史 revision 切换 UI
- MCP HTTP transport 仍是 placeholder；当前可用 transport 为 stdio
- `added_tool_names` 已保留到 provider 边界，但当前 OpenAI/Anthropic-compatible adapters 不实现原生 deferred tool loading，仍按完整 ToolRegistry 发送工具

### 文件与 Knowledge

- Session Folder 的 `view_file` 只对文本/Markdown/HTML/CSV/Parquet提供正文或结构化预览；Session PDF 只返回元信息
- Knowledge 子系统可解析文本型 PDF；扫描件进入 `needs_ocr`，当前无 OCR、图片理解或视觉模型
- Knowledge 检索使用 SQLite FTS5/BM25，不使用向量数据库或 embedding
- 旧记录中已经写入的 Unicode replacement character `U+FFFD` 无法从现有数据反推出原字符；UTF-8 修复只阻止新的静默损坏

### 工程债务

- 当前无 Git remote；tag/push 需要先决定版本并配置 remote
- 全量 Backend 有 58 条 warning，主要是 Starlette/httpx deprecation 和错误的同步测试 `asyncio` marker
- 本机默认 pytest temp/cache 目录存在 ACL 限制；当前开发验证固定使用仓库内 `--basetemp` 并关闭 cacheprovider，避免把环境错误误判为代码失败

## 当前阻塞项

无功能实现阻塞。版本、许可证、Browser E2E 与最终真实 smoke 发布门均已关闭；正式 release 只剩决定 tag 名，并在需要 push 时配置 Git remote。

## 建议下一步

1. 继续 pi-agent 对齐：评估 append-only 会话树或 lane-based Session，明确 branch、fork、label 与 active leaf 语义。
2. `0.0.28` tag/push 仍需单独决定；仓库当前尚无 remote。

未完成事项的唯一清单见 [`TODO.md`](TODO.md)。使用与架构说明见 [`README.md`](README.md)。
