# Web Agent Core 全模块审计

> 日期：2026-09-03
>
> 范围：`src/pi_agent_core_py` 及直接承担 Web 产品装配的 `src/coding_agent_app`
>
> 产品前提：localhost-only Web Agent；不建设独立 CLI/TUI、远程 Agent Client/Server 或跨进程 Protocol 产品。HTTP MCP 是 Web Agent 的扩展 transport，属于产品范围。

## 审计原则

1. 只保留能进入当前 Web 请求、持久化、安全、观测或页面中心 Wiki 主链的实现。
2. 同一能力只保留一个产品入口；测试 seam 与公开导入兼容层可以保留，但不得成为生产依赖。
3. 不为产品范围外能力保留“未来可能实现”的空壳；配置应在边界处明确拒绝。
4. 缺口只补最小闭环，并由行为或结构测试固定。

## 模块结论

| 模块 | Web 适配结论 | 处理结果 |
|---|---|---|
| `ai` | 必需 | 保留消息、模型客户端、流事件和 Provider adapters；Web/Coding Agent 直接依赖 `ai.providers` canonical owner。 |
| `agent` | 必需 | 保留 loop、状态、hooks、工具契约、Harness、Skills、Compaction 与 Session；删除重复 Tavily 工具和失效的 Chunk Knowledge Prompt 提示。 |
| `mcp` | 必需 | 保留 stdio、实现 Streamable HTTP client/transport，并保留 registry、prompts、adapter、Fake 测试 seam 与 DDGS server；全局目录由 Workspace/Session 选择后进入请求级资源快照。 |
| `policy` | 必需 | 保留权限、审批审计和文件/Sandbox 边界；搜索类名称仍参与通用只读权限分类。 |
| `providers` | 兼容层 | 11 个平铺模块仅重导出 `ai.providers`，对象身份兼容仍有价值；生产组合不再反向依赖这些 facade。 |
| `secrets` | 必需 | 保留 OS Keyring、环境变量与 session-only memory 的统一凭证边界；无明文 SQLite 降级。 |
| `session_backends` | 必需 | 保留 SQLite Repository/Storage/Search、migration、branch/fork、writer lease 与统计；每个 Web Runtime Session 持有独立可释放 handle。 |
| `telemetry` | 必需 | 保留 Core Context/Span/schema、memory/noop 与 SQLite Recorder；最小补齐 Prompt、Regenerate、Checkpointer 的通用无正文 span。 |
| `tools` | 部分必需 | 保留文件与 Coding Sandbox 兼容入口；删除 Tavily `web_search`，产品网络搜索只走 DDGS MCP。 |
| `web` | 产品组合根 | 保留 Auth、Session、Provider/Credential、Workspace、MCP、Telemetry Admin、Wiki 与当前 Vue 应用；删除 Chunk-RAG 子包和未挂载旧前端层。 |
| 根平铺模块 | 仅兼容 | 保留公开/测试仍引用的 thin facade 与模块别名；新增 AST 门禁，禁止产品 runtime 依赖这些旧路径。 |

审计后的 Python 规模（只计 `.py`）为：`agent` 26 文件、`ai` 17、`mcp` 10、`policy` 4、`providers` 11、`secrets` 7、`session_backends` 21、`telemetry` 7、`tools` 5、`web` 63。规模不是保留依据，只用于后续漂移检查。

## 删除清单

- 删除 `web/knowledge` 的 25 个 Python 文件：旧 Chunk DB、ingestion/indexing worker、PDF parser、canonical Markdown、FTS Chunk search、citation/evidence、REST 与 Tool 装配全部退出运行时。
- 删除 `[rag]` optional extra 及对应 lock marker；`pypdf` 因当前 Session 文档/Web 能力仍在 `[web]` 中保留。
- 删除 Harness Tavily 实现、顶层 `tools.web_search` facade 与专属测试；DDGS MCP 成为唯一 Web Search 产品路径。
- 删除过往没有真实协议行为的 HTTP MCP placeholder；后续以完整 Streamable HTTP transport 重新实现，并补齐 Web 配置、持久化和 Workspace 选择。
- 删除 11 个未挂载的顶层 Vue inspector 组件、未使用的 `api/index.ts` barrel，以及仅服务这些旧组件的 API/type 兼容面。

本轮净变更约删除 14,500 行，主体是已退出产品组合的 Chunk-RAG，而不是压缩仍在使用的 Web 主链。

## 最小闭环

| 缺口 | 闭环 |
|---|---|
| 删除旧 Knowledge 后的数据态度 | 产品启动不打开、不迁移、不删除旧库；`wiki/legacy.py` 只在显式归档流程中识别并移动历史数据。 |
| 搜索入口重复 | 只保留 DDGS MCP；它继续支持持久配置、启停、生命周期与前端参数。 |
| HTTP MCP 空壳容易形成错误承诺 | 不保留空壳；实现 JSON/SSE、Session/版本头、initialized notification、关闭清理和安全 header env 引用，配置接受 `stdio`/`http`。 |
| 全局 MCP/Skills 会串入所有会话 | 账号级目录与 Workspace 选择分离；每个 Session 请求冻结自己的 Skill/MCP Tool 快照。 |
| Regenerate / Checkpointer 缺少请求级观测 | 统一复用 `_run_observed_web_operation`，记录 operation、outcome、duration 与有界标识，不记录 Prompt、消息、工具 payload 或异常正文。 |
| 产品代码仍可能绕回旧 facade | 生产 import 改到 `ai/agent/session_backends` canonical owner；结构测试扫描 `coding_agent_app`、Web、MCP、Policy 并拒绝旧依赖。 |

## 有意不做

- 不引入 pi 的 `client/protocol/server/tui` 目录，因为当前唯一产品面是同进程 FastAPI + Vue Web Agent。
- 不把 HTTP MCP 扩展为独立远程 Agent/Protocol 产品；它只通过当前 Web Agent 的全局目录和 Workspace 选择使用。
- 不删除仍被公开导入和兼容测试依赖的 thin facade；它们不承载业务实现，且由结构门禁隔离在生产组合之外。
- 不把 Evals 或 Telemetry 扩展成远程服务；两者分别保持本机离线行为门禁和被动、内容安全的运行观测。

## 验证

| 门禁 | 结果 |
|---|---|
| Ruff | `ruff check src tests scripts evals`：PASS |
| strict Mypy | `mypy src evals`：263 source files / 0 issues |
| Wiki Parser Worker | Ruff PASS；strict Mypy 18 source files / 0 issues |
| Lockfile | `uv lock --check --offline`：104 packages，PASS |
| Local Evals | 5 suites / 10 observations；candidate gate PASS |
| Backend | 2201 passed / 7 skipped / 9 deselected；coverage 76.63%（门槛 75%） |
| Frontend | Vitest 29 files / 188 tests；ESLint、vue-tsc、production build 全部 PASS |
| Browser E2E | Chromium 20/20；0 retry / 0 failure；post-test production build PASS |

删除边界另由 MCP 配置拒绝、产品 import AST、Web Telemetry Admin 集成及现有 Web/Wiki/Session 回归固定。所有离线门禁未加载真实 Provider 凭证、未访问外网，也未启动 E2B 或 Docker。
