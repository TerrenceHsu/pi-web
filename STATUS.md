# Project Status

> 当前事实快照，校准日期：**2026-09-06**。本页只描述当前代码基线；阶段性测试数字和历史决策保留在 `docs/validation/`、`CHANGELOG.md` 与归档计划中。

## 基线身份

| 项 | 当前事实 |
|---|---|
| 代码基线 | `0.0.29` 发布基线之后的 `master`；新增三类意图路由、Planner–Executor–Verifier Plan Mode、Sandbox 审阅/冲突恢复，以及 pi `ai` / `agent` / `coding-agent` / `session-backends/sqlite-node` / `telemetry` / `evals` 核心边界对齐与 Admin Telemetry 前端 |
| 分支 | `master` |
| 最新 release tag | `0.0.29`；annotated tag 指向本次统一版本与发布验证提交 |
| Python / API 版本 | `0.0.29`（Python `__version__`、workspace FastAPI 与 Auth gateway 共用同一来源） |
| 前端与 Worker 版本 | `0.0.29`（Web package/lock 与 Wiki Parser Worker package/lock/OCI/compliance metadata 一致） |
| 许可证 | MIT；根 `LICENSE` 为标准正文，`pyproject.toml` 与 wheel 均直接引用/携带该文件 |
| Git remote | 当前仓库**未配置 remote**，因此尚无可执行的 push 目标 |
| Python | 声明支持 `>=3.11`；本机验证使用 Python 3.12.13（conda `pipy`） |
| 产品边界 | localhost-only 本地 Agent 工作台；不是公网 SaaS |

> `0.0.29` 是当前 Python、API、前端、Wiki Parser Worker 与 release tag 的统一发布基线。

## 当前交付状态

**阶段 2D：Web Coding/Plan 已接入任务审批与调度**。Coding 每请求确认执行范围，Plan 以一次确认
原子批准精确计划版本与执行许可；准备/批准不创建容器，启动前复核原快照、请求、配置与资源选择。
任务内工具共用获准副本，Planner/Verifier 不获得执行权限；旧直接启动/计划单独批准入口拒绝绕过，
验证与冻结由请求调度器执行，运行期 UI diff 只获得只读上下文。
任务结束回收计算资源，冻结制品仍交给原有 Changes 审阅；E2B 保留独立确认发布，Docker 发布暂不可用。
Workspace 增加 revision-CAS 后端选择；选择变更/撤销、Stop、到期和能力变更使许可失效，不跨请求复用。

本地 Docker 需部署者显式配置固定镜像/CLI 并由用户选择，**本轮未修改真实部署配置或自动启用**；
没有静默迁移 E2B，也没有启用独立 `run_bash`。Python 分析原有逐次授权不变。
新增 11 项离线 Web 专项、2 条真实 Docker Web Coding/Plan 链路和前端全量 221 项通过；
最终门禁见 [阶段 2D 记录](docs/validation/workspace-bash-stage2d-2026-09-06.md)。
阶段 2C 的 398 项相关回归与 25 项后端 Docker smoke 保留为历史证据，不与本轮两条 Web 链路合并计数。

**仍待完成**：独立 Bash 完整脚本确认及注册、Docker 发布、后台孤儿/TTL 与快照缓存回收、私有运行记录、
跨账号全局配额、管理员执行前端与执行 Telemetry。重启不恢复执行；未知资源保留清理债务，不假装已清理。
Docker 数据仍位于 `D:\DockerData\DockerDesktopWSL`；没有重启业务解析容器。
`9518499` 是前置功能/阶段 2B 本地提交；随后阶段 2C 和本轮 2D 改动均尚未提交、未推送。

新增 **Web Context Compaction**：按持久化视图、工具输出外置、结构化自动摘要、
Web/Telemetry/Evals 四步实现。原始 SQLite 消息与来源 ID 不变，Memory 不由压缩改写；
实际逐次模型调用执行有效预算检查，失败保留旧视图。支持独立工作摘要、来源回查与会话自动开关。
设计及边界见 [`context-compaction.md`](docs/design/context-compaction.md)。
最终门禁：Backend 2378 passed / coverage 77.19%，Frontend 218 passed，Chromium 24/24
（零重试）；Ruff、strict Mypy 284 files、Evals、lock 与生产构建通过。见
[`验收记录`](docs/validation/context-compaction-2026-09-05.md)。

新增 **Session History / Structured Memory（阶段 1–4）**：Agent 通过两个 Session 绑定只读工具
检索原始 SQLite 历史；正文 FTS/中文短词回退、分页、只读连接与事务初始化已接入。
每轮输出结构化 Memory 增量，支持 no-change、来源 ID、用户纠正/固定和分支失效标记，沿用失败恢复。
设计见 [`session-history-memory.md`](docs/design/session-history-memory.md)。其后的压缩阶段见上。

新增 **Python Data Analysis**：Agent 通过 `run_python_analysis` 在独立 `.venv-analysis`
执行用户逐次批准的 Python，完整代码/来源确认、拒绝/取消、中文 stdout、表格/PNG、历史与显式保存
已接通；None/AllowAll 权限也不能跳过确认。不需要 Docker，依赖隔离不是安全沙箱，仅供受信本机使用。
设计见 [`python-data-analysis.md`](docs/design/python-data-analysis.md)。

新增可选本机 **Data Analysis**：在 Workspace → extensions → Tools 启用 `analyze_data`，
通过聊天或 analysis 页执行 CSV/TSV/XLSX/Parquet 的固定统计、分组、时间汇总和图表。
独立工作进程可取消、有资源限制；用户显式保存后才向 Workspace 追加报告/CSV/PNG/manifest，
不改原件。工具默认关闭，选择、任务和结果按账号/Session 隔离；具体限制见
[`data-analysis.md`](docs/design/data-analysis.md)。

LLM Wiki 阶段 0–10 已完成：PDF 采用隔离 MinerU Worker 与三档固定配置，HTML
保持零网络；Raw、Agent Summary、入口/主题页面、Change Set 审批、页面 FTS5、知识图谱、每 Space
多 Knowledge 对话、独立前端、来源保留、Space 生命周期和归档只读门禁均已接通。产品组合不再启动
旧 Chunk Knowledge DB/Worker/Tool/API/UI：

| 能力 | 状态 | 代表性基线 |
|---|---|---|
| Step 1–21 Core Runtime | ✅ 完成 | 归档见 `docs/archive/legacy-plans/PLAN_STEP_1_21.md` |
| Multi-provider Runtime / UI | ✅ 完成 | GLM、Qwen、Kimi；Anthropic-compatible 后端能力 |
| pi `ai` 核心契约对齐（第一项） | ✅ P0 差距已补齐 | provider-aware 历史转换、精确 provider/API/model 身份、跨模型 thinking 安全降级、text/image 内容块、工具调用配对修复、cache/reasoning Usage、首事件前可中止重试；详见 `docs/validation/pi-ai-parity-2026-08-31.md` |
| pi `agent` 核心契约对齐（第二项） | ✅ P0 差距已补齐 | 生命周期终化、下一轮控制顺序、文本/图片/AgentMessage prompt、signal-aware context、自定义 LLM 转换、工具参数预处理与 hook patch、assistant-tail 队列续作、thinking/运行时替换和公开状态同步；详见 `docs/validation/pi-agent-parity-2026-08-31.md` |
| pi `agent` package 结构对齐 | ✅ 完成 | canonical 实现迁入 `ai/`、`agent/`、`agent/harness/{compaction,session,tools}` 与 `session_backends/sqlite/`；旧平铺模块保留 thin facade/模块别名，兼容导入保持对象身份；AST 依赖边界、wheel 内容与隔离安装导入均通过；详见 `docs/validation/pi-agent-package-structure-2026-09-01.md` |
| pi `coding-agent` 产品组合边界（第三项） | ✅ 阶段 1–3 完成 | 新增 `coding_agent_app.core` 的 Application/Runtime/Session/Services/Settings/Resources/Toolset/Prompt/SDK 边界；Web Session ID 真实映射独立 Runtime Session/Harness，不同 Session 可并行；Provider、Skill、MCP、Workspace 与 Prompt 统一经请求 composition 快照装配；Sandbox automation/workspace 迁入产品包并保留旧导入对象身份。详见 `docs/validation/pi-coding-agent-parity-2026-09-01.md` |
| pi `session-backends/sqlite-node` 持久化边界（第四项） | ✅ 核心差距已补齐 | 新增 Harness 级 Repository/Storage/Search 协议、全局 append-only log、typed query、统计、writer lease/fence/heartbeat、branch cache/repair、有序事务 migration 与 writer 初始化的 FTS5；共享 SQLite 服务按 connection 串行并在异常/取消时回滚；每个已启动 Web Runtime Session 持有独立可释放 Storage handle。详见 `docs/validation/pi-session-backend-parity-2026-09-03.md` |
| pi `telemetry` 运行观测（第五项） | ✅ 核心与 Admin 产品面完成 | 对齐 Context/Span、noop/memory/schema 与 passive callback 语义；增加共享 SQLite Recorder、Web/Coding Agent 安全指标、Admin-only summary/list/detail API 和 Vue 仪表盘。正文、工具参数/输出、凭证与异常正文不落盘。详见 `docs/validation/pi-telemetry-parity-2026-09-03.md` |
| pi `evals` 本机行为评测 | ✅ 离线核心完成 | 独立私有包在真实 Coding Agent Application/Runtime/Session 上运行 Prompt/Reload；每个 Observation 隔离临时 Workspace/SQLite，以 Fake Provider 执行确定性 Judge、baseline/candidate 配对、pass-rate/token/latency/cost 汇总和默认脱敏产物。详见 `docs/validation/pi-evals-parity-2026-09-03.md` |
| 旧 Chunk Knowledge PDF → Chunk FTS5 → Citation | 🗑️ 已移除 | 运行时、REST、工具、前端与 `rag` extra 均已删除；只保留 `wiki/legacy.py` 识别并归档旧磁盘数据 |
| Web-only Agent Core 模块审计 | ✅ 完成并校正 MCP 产品边界 | 保留 Web 主链需要的 `ai/agent/mcp/policy/secrets/session_backends/telemetry/web`；移除 Chunk-RAG、Tavily 重复搜索与死前端兼容层；HTTP MCP 已按产品设计实现为 Streamable HTTP，MCP/Skills 使用账号级目录 + Workspace 选择，并以结构测试固定产品代码只依赖 canonical 模块 |
| 登录与账号工作区隔离 | ✅ 完成 | `b529bbc` |
| Session Workspace、`AGENT.md`、`Memory.md`、`/checkpointer` | ✅ 完成 | `WorkspaceStore` 为唯一规范事实源；新旧 Session 幂等初始化两个固定根文件，保留旧正文/file id；设计见 `docs/design/workspace-sandbox-integration.md` |
| Workspace 模块分离 | ✅ 阶段 1 完成 | 规范 Store、固定文档转换和 provider-neutral continuity 已迁移到顶层 `agent_workspace`；Workspace/Sandbox adapter 位于 `coding_agent_app`，旧 Core 路径只保留兼容层；独立 import boundary 与 wheel 内容验证通过，Ruff、strict Mypy 177 files、既有定向 133 passed |
| 每轮自动 Session Memory | ✅ 阶段 2 完成 | Prompt/Regenerate 以有界 turn evidence 和串行 durable operation 累计更新 `Memory.md`；失败不影响主回答并在下一轮 preflight 恢复，Coding 待审批期间按 Sandbox blocker 延迟以保护 Workspace revision，Knowledge 模式跳过；产品入口默认启用 |
| Coding Workspace 内容分类 | ✅ 完成并更新上传路由 | `WorkspacePathPolicy` 统一所有权与写入/发布边界；所有新上传原件进入只读 `upload/**`，工作代码进入 `scripts/**`，Agent 非代码产物进入 `artifacts/**`；空目录保持惰性，历史 `inputs/**` 与文档原件原位兼容 |
| 已发布代码流程总结 | ✅ 阶段 4 完成 | revision-bound `scripts/**` 生成固定 architecture/code-flow/validation；mutation 先 stale、全套持久化后 current，并发/失败 fail closed；真实 Sandbox evidence 与普通上传“未验证”严格区分；Backend 30 + 邻接 85 passed，Frontend 6/6，静态检查通过 |
| 无聊天上下文续作 | ✅ 阶段 5 完成 | 单一 provider-neutral assembler 为 Prompt/Regenerate/Context Budget 装配 revision-bound Workspace；stale 摘要与未发布 Artifact fail closed/显式标记，公开 secret-free context hash/audit；零历史重启与复合恢复风险已验收 |
| Workspace 代码/Markdown 规则与 revision | ✅ 完成 | 代码统一映射到逻辑 `scripts/**`；Markdown CRUD、逐文件 SHA 与持久 Workspace revision 冲突契约已接入 Store/Web/Agent/Frontend API |
| 右侧 Workspace 成果面板 | ✅ 完成 | 桌面三栏/窄屏 drawer；Agent `write_file` 完成后按 file id/revision 自动聚焦成果，支持 Markdown 预览编辑、代码查看、上传下载及 Sandbox/Changes |
| P0 Runtime 上游契约对齐 | ✅ 完成 | `b529bbc` |
| Session URL 与整页刷新恢复 | ✅ 完成 | `f30da56` |
| Human Approval + Context Budget/Compaction UI | ✅ 完成 | `924b047` |
| B7 SQLite 初始化失败资源清理 | ✅ 完成 | `688cf08` |
| Keyring 真实可写预检与启动 fail-fast | ✅ 完成 | `ada31fc`；Windows 实机 smoke 见 `docs/guides/windows-keyring-preflight-smoke.md` |
| ToolResult 顺序与 MCP UTF-8 | ✅ 完成 | `e7bf8f3` |
| Agent 语义正确性与控制队列 | ✅ 完成 | `c214d28`；串行 preflight、终态收敛、steering/follow-up、全局 tool execution |
| Thinking 与细粒度流生命周期 | ✅ 完成 | `c214d28`；text/thinking/tool-call start/delta/end |
| Agent 公开运行时状态 | ✅ 完成 | `848ae1d`；model / thinking level / streaming message / pending tool calls / error message |
| ToolResult usage 与 deferred-tool metadata | ✅ 完成 | `b6baea8`；事件、LLM 边界、Snapshot、Session/SQLite 与 Web JSON 全链路保留 |
| Durable operation / recovery | ✅ 完成 | `8a5c569`；append-only operation records、Checkpointer restart recovery、原子文件 generation、JSON journal |
| Complete-turn Compaction semantics | ✅ 完成 | `0c72679`；完整 turn、token/window 审计、previous-summary envelope、瞬时错误重试 |
| 全仓 Ruff / strict Mypy / CI 收敛 | ✅ 完成 | Ruff 0；strict Mypy 170 source files / 0 issues；Python CI timeout 30 分钟 |
| `0.0.29` Release metadata 与许可证 | ✅ 完成 | Python/API/前端/Worker 统一版本；主 wheel 与 Worker 携带 MIT，Worker 另附 MinerU 第三方许可、Notice 与 SBOM |
| 当前发布前浏览器/联网门禁 | ✅ 完成 | 精简 Playwright 19/19、0 retry/flaky；真实 E2B Managed Sandbox/审批/WorkspaceStore 回写 PASS；此前 DDGS + GLM 真实 smoke 3/3 |
| Managed Coding Sandbox P0 0–10 | ✅ 完成 | 独立包、快照、E2B、代码工具、固定验证、签名制品、本机事务 Publisher、Web 生命周期/状态恢复/审批发布 UI，以及真实 E2B、完整 CI、攻击矩阵和 Browser E2E 验收 |
| 自动 Coding 请求编排 | ✅ 已接入任务审批 | Chat `Code` 每请求批准精确范围后才启动隔离副本，仅暴露 9 个 `coding_*` 工具；任务内可修复重试，结束后独立重验/冻结并释放执行资源，绝不自动发布或跨请求复用执行许可 |
| 三类意图路由 | ✅ 完成 | 产品入口确定性路由 `read_only/coding/knowledge`；Knowledge 由 Conversation binding 强制决定，Coding 复用 Sandbox 状态机，只读路线裁剪 mutation/execute 工具；API/Context Budget/Turn 卡公开决策审计并支持显式覆盖 |
| Planner–Executor–Verifier Plan Mode | ✅ 完成 | Plan 是 Coding 路由的执行方式；PlanStore 持久化 DAG/版本/任务/事件，Planner 只提交结构化计划，Executor 复用单一 Session Sandbox，Verifier 按真实 diff 验收；最终仍停在签名 Artifact 用户审批门禁 |
| Sandbox 审阅与发布冲突恢复 | ✅ 完成 | 冻结文件可在发布前逐项预览/下载；`publish_conflict` 保留签名制品并支持重新冻结或在冲突解除后重试发布；空变更不再生成可批准 Artifact，自动 Coding 可执行一次修复重试 |
| Workspace / Chat 交互收敛 | ✅ 完成 | 聊天区直接显示 Sandbox 审批条；Workspace 文件树合并待发布冻结文件并支持 Python 安全高亮；桌面三栏和 Workspace 上下分区可拖拽并持久化；Thinking 在首段正文前也能流式展示 |
| Coding Sandbox 阶段 4A 状态机/TOCTOU | ✅ 完成 | Backend 单一转换表与公开 actions/transitions、SQLite 完整记录 CAS、UI 动作投影；Validation→Freeze 屏障前 stale 可重验，屏障后 `artifact_stale` fail-closed 终止 |
| Coding Sandbox 阶段 4B Workspace baseline | ✅ 完成 | WorkspaceStore mutation lock 内按 logical path 物化 revision-bound 树，逐文件稳定 stat/SHA 校验；operation 记录源 revision/tree SHA，主应用不再以独立项目目录作为输入事实源；4C 前发布 fail closed |
| Coding Sandbox 阶段 4C Workspace 发布 | ✅ 完成 | 签名 Artifact 在隔离镜像复验后，通过 WorkspaceStore journaled multi-file transaction 发布；路径白名单、目标端 revision/tree/content TOCTOU、rollback/recovery、单次 revision 和 `workspace_changed` 右栏刷新均已接通 |
| Session Workspace 固定文档转换 | ✅ 完成 | PDF/DOCX/XLSX 原件进入 `upload/<filename>` 且不可变；本地固定转换器从 revision 快照在 `documents/<stem>-<source-id>/` 生成只读 Markdown/CSV/schema/assets/manifest，并通过 WorkspaceStore 单 revision 事务发布；OCR 延期，完全独立于 LLM Wiki Parser |
| Backend warning / pytest 状态目录 / ToolResult UTF-8 E2E | ✅ 完成 | Backend `-W error` 0 warning；cache/temp 固定到工作区；Playwright 48/48 |
| Frontend warning 收敛 | ✅ 完成 | Modal/Teleport attrs、Vite mixed import 与 Playwright color env 三类提示归零；Vitest 404/404；Playwright 48/48 |
| 历史消息 U+FFFD 完整性标记 | ✅ 完成 | 读取时递归检测并返回计数/RFC 6901 路径，不改写 SQLite、不伪造恢复；消息与工具卡可见，刷新保持；Playwright 49/49 |
| LLM Wiki MinerU Parser | 🟡 源码闭环完成，真实镜像 Gate 待跑 | Contract v2 固定 `pipeline/gpu-medium/gpu-high`，每个 Job 单次 MinerU attempt；主应用通过 file queue 连接隔离 Worker，逐页制品、质量、同源 SHA 与 hash-pinned config 均保留；详见 `docs/design/llm-wiki-mineru-parser.md` 与 `docs/validation/wiki-mineru-parser-2026-09-05.md` |
| LLM Wiki Worker 合规包 | 🟡 `runtime_ready=false` | Worker 自身 MIT，MinerU 3.4.5 独立许可；132 包 uv lock、131 包 hash requirements、CPU/GPU Compose、构建期模型下载、确定性源码归档、SPDX SBOM 与 wheel verifier 已更新；新 OCI 镜像、断网和代表性 CPU/GPU PDF 尚未复验 |
| LLM WikiStore、目录与旧库退役 Gate | ✅ schema v8 | 三档 MinerU mode 进入 Jobs/Attempts/Revisions CHECK；旧 schema v7 明确要求重建；页面中心 Store 继续覆盖 Summary/Proposal/Page/Revision/Change Set/FTS5/Edge/Conversation |
| LLM Wiki Raw Ingestion | ✅ 源码闭环 | 不可变 PDF/HTML、零网络 HTML、MinerU Contract v2、Raw parse revisions、不可信 artifact、恢复，以及 Space/Source/状态/Raw artifact 前端均已完成 |
| LLM Wiki 页面中心主链路 | ✅ 完成 | Agent Summary → 入口页/主题页 Proposal → 单一 Change Set diff → 用户审批 → Page/Revision/FTS5/Graph 原子发布 |
| 独立 Knowledge 页面与 Agent | ✅ 完成 | `/knowledge` 提供 Pages/Sources/Graph/Changes/Conversations；每 Space 多对话复用 Agent 流事件，采用独立 Prompt/Skill/10 工具白名单 |
| 旧 Chunk Knowledge 产品退役 | ✅ 完成 | 删除运行时 package、REST、Worker、Store、`search_knowledge`、前端与 `rag` extra；产品只运行页面中心 LLM Wiki，旧磁盘数据仅可经 `wiki/legacy.py` 显式归档 |

## 当前产品能力

- 未登录先进入 Username/Password 页面；空认证库幂等创建本地初始账号 `admin / 123456`
- 每个账号拥有独立的 Session、消息、文件、Skills、MCP、Wiki Space/Conversation、Provider/Credential 配置
- Session 路由为 `/chat/{session_id}`；刷新恢复准确 Session、历史、文件树、`AGENT.md`、`Memory.md` 及当前请求状态
- 每个 Web Session ID 对应独立 `CodingAgentRuntime` Session 与 Agent/Harness 状态机；同 Session 单请求串行，不同 Session 可并行，Provider/Skill/MCP/Workspace/Prompt 在进入 Agent 前形成一次不可变请求装配
- 每个 Session 由唯一 `WorkspaceStore` 初始化独立文件夹和唯一根 `AGENT.md`、`Memory.md`；启动时幂等补齐旧 Session，保留已有正文/file id，两个根文件不可删除；`VirtualFileStore` 仅为同一实现的兼容别名
- 所有新上传原件（含代码）进入不可原地改写的 `upload/**`；聊天区与 Workspace 面板均支持拖拽，同名自动编号，目录首次成功上传时出现。代码原件复制到 `scripts/**` 才作为工作代码参与摘要；Agent 非代码交付物默认进入 `artifacts/**`，共享笔记使用 `docs/notes/**`
- 文件 API 和右侧 Workspace 面板直接消费 `WorkspacePathPolicy` 的分类、所有者、可编辑/移动/删除、Agent 写入、Sandbox 发布及不可变标记；系统连续性文件不能由普通用户/Agent 文件入口伪造
- Workspace revision 以隐藏状态持久化；上传、创建、更新、移动和删除可同时校验 revision 与逐文件 SHA，过期客户端收到 409 而不会静默覆盖
- 用户代码上传/删除和批准 Sandbox 发布会从实际 revision 更新三份只读工程摘要；每份记录代码树 SHA 和来源 revision，右栏公开 current/stale/failed，启动可恢复未完成或旧 Session 摘要
- PDF、DOCX、XLSX 上传会归档不可变原件并生成可审计只读制品；右栏优先打开 `content.md`，XLSX 公式不会在转换阶段执行，失败不会暴露半套输出
- 桌面右栏是 Agent 成果交付面：成功生成 `.md`、`.py` 等文件后立即刷新、选中并展示，整页刷新后仍从持久 ToolResult 恢复；窄屏使用带新成果提示的 drawer
- 每个成功普通 Session Prompt/Regenerate 自动使用当前 Provider 累计更新 `Memory.md` 且不清空消息；失败保留有界 evidence 并在下一轮 preflight 恢复，Prompt/request 的 `continuity` 和 `/api/state.auto_memory` 公开当前状态
- `/checkpointer` 保留为显式“总结完整当前对话并清空 lane”操作；接受时持久化 source leaf/hash，文件发布后原子清空原 lane，进程退出可幂等前滚
- 支持 Prompt、Stop、Regenerate 最新 Assistant、Markdown Export、实时事件、请求恢复和精确 ToolCall 审批
- Provider Profile、Session Model Binding、Context Window 与 Max Output Tokens 持久化；UI 管理 GLM/Qwen/Kimi
- Admin 可通过 `/telemetry` 查看跨账号 Agent 请求量、错误率、P95、token/cost、Provider、工具调用和结构化事件；服务端按持久 `is_admin` 鉴权
- 持久化凭证默认进入 OS Keyring；开发启动器在监听端口前执行 write/read/delete 探针
- MCP 支持 stdio 与 Streamable HTTP tools/prompts；MCP/Skills 由账号级全局目录配置、Workspace/Session 持久选择，请求级资源快照隔离；内置 DDGS 固定存在、不可删除，全局启用后为新 Workspace 默认项，可修改返回数、地区、安全搜索、时间范围等参数
- 独立 `/knowledge` 页面支持 Wiki Space、PDF/HTML Source、Raw artifacts、页面/revision、页面 FTS5、已发布图谱、统一 diff 审批与每 Space 多 Agent 对话
- `create_app(wiki_root=...)` 运行独立 Wiki Store/API/Worker；HTML 真实离线解析，PDF 未配置 Provider 时保持 `uploaded`。旧 Chunk Knowledge 已无运行时或 API 装配入口，不与 Wiki 共享表或业务数据
- Core Runtime 的一个 Turn 等于“一次 LLM 调用 + 该调用产生的当批工具”；Snapshot 分为 RequestSnapshot 与 TurnSnapshot
- Core Runtime 的 canonical package 自底向上为 `ai ← agent core ← agent.harness ← session_backends`（右侧依赖左侧）；旧 `messages.py`、`loop.py`、`harness.py`、`session_sqlite.py` 等路径只承担兼容导入，不再复制实现
- 并行工具批次先按源序串行完成 hook、权限、审批与参数校验，再并行执行已放行工具；hook 不得改写 tool-call ID
- Agent 支持独立 steering / follow-up 队列及 `all` / `one-at-a-time` 消费模式；活跃请求期间普通 prompt/continue 明确拒绝
- Agent prompt 支持文本+图片、单条或批量 `AgentMessage`；`CustomMessage` 可由应用自定义转换进入模型，并在 Harness、Snapshot 与 Session 中无损保留
- assistant 尾消息若已有 steering/follow-up 可由 `continue_()` 正确消费；`should_stop_after_turn` 先于 next-turn preparation，且 preparation 只在确有下一轮时执行
- 全局 `tool_execution` 可强制批次串行；逐工具 `execution_mode="sequential"` 可在并行全局模式下收紧执行
- 工具可在 schema 校验前执行拥有者级 `prepare_arguments`；before hook 可终止工具链，after hook 可逐字段覆盖结果，工具 promise 完成后的迟到 update 被丢弃
- Assistant thinking/reasoning 只有在来源 provider/API/model 与目标完全相同时才回放签名或 redacted payload；跨模型时明文 thinking 降为普通文本、密文块丢弃；text/thinking/tool-call 均有完整 start/delta/end 流事件
- AI 核心支持 provider-neutral base64 `ImageContent`，Anthropic/OpenAI adapter 各自生成协议内容；不支持视觉的 adapter 自动降级为明确占位文本，附件上传到视觉内容块的产品接线留在 `coding-agent` 模块阶段
- Provider 调用仅在首个流事件前对限流/网络瞬时错误做有界、可中止重试；一旦已有部分输出就不重放，避免重复文本和工具调用
- `AgentState` 公开 secret-free model 身份、system prompt、active tool names、thinking level、请求流状态、当前 partial message、执行中 tool-call ID 与最近 assistant error；Web `/api/state` 使用同一事实源
- thinking level 由 Agent loop 逐次传入 `ProviderRequest`，`prepare_next_turn` 可原子替换下一轮 context/system/client/tools/thinking；`AgentEndEvent` 同时公开兼容的完整 transcript 与明确的本轮 delta
- ToolResult 可携带工具自身 usage 与 `added_tool_names`；usage 不并入主 LLM 上下文计费，added names 只标记 `Context.tools` 的 provider 加载点且不能由 after hook 伪造
- SQLite Session 使用 Harness 级 Repository/Storage/Search 边界；entry/record/lane/fact 共享 append-only sequence，命名 lane 持久化 active leaf，支持 branch/fork、name/label fact、writer fence、统计、branch cache 修复、FTS5 搜索与重启恢复；`messages` 是 active lane 的兼容投影
- Session lane operation 使用 append-only intent/effect/finish records；`Memory.md` 更新以 immutable generation + 原子 metadata pointer 发布，旧 JSON Session save 使用可修复 torn tail 的 append-only journal
- Compaction 默认按完整 user→assistant/tool-result turn 切分；token 目标不拆最新 turn，压缩前后 token/window 可审计，旧摘要按 pi-compatible envelope 迭代折叠，瞬时摘要错误可按不可变输入重试
- Web 消息序列化会递归检测 `U+FFFD` 并附加 `content_warnings`，`/api/messages` 同时返回 Session 汇总；前端在对应消息/工具卡标记疑似编码损坏和 JSON 字段路径，检测过程只读且明确不可自动恢复
- Managed Coding Sandbox 使用顶层独立 `coding_sandbox` 包；主应用从 Session WorkspaceStore revision 物化不含存储 metadata 的逻辑树，再由 Agent 通过 provider-neutral 工具修改云端副本，固定验证通过后冻结并签名不可变制品
- Chat 输入区可显式启用 `Code`：先准备精确快照并显示任务范围，用户批准后才创建请求专属副本；本轮工具/权限收窄为 9 个隔离 `coding_*` 工具，结束后服务端重验/冻结并释放计算资源，Changes 中另行审阅和批准发布
- Chat 输入区可显式启用 `Plan`：Planner 提交结构化 DAG 后，通过一张审批卡原子批准精确计划版本及执行范围；Executor/Verifier 使用同一获准副本但分别受执行/只读角色约束，失败原因与重试次数持久化，全部任务通过后经过 Validation→Freeze 屏障
- 待批准或发布冲突的冻结 Artifact 可直接在 Workspace 树中预览/下载；目标 Workspace 冲突不会丢弃制品，用户可按冲突类型选择重新冻结或重试发布
- 本机 Publisher 在项目级跨进程锁内复核完整 baseline 和签名制品，以备份、原子替换、hash-chained journal、失败回滚和启动恢复发布；Sandbox 永不挂载真实工作区
- 每个 Session 最多一个活跃 Managed Sandbox operation；创建、验证、冻结、审批发布、取消和丢弃均由 Backend 单一转换表驱动，REST 返回版本化 `allowed_actions`/`allowed_transitions`，SQLite 完整旧记录 CAS 防止并发状态覆盖，并保存有界事件日志通过统一 WS 实时推送
- Validation 与 Freeze 共用 operation 互斥锁；冻结前重验 validation/config/workspace SHA，冻结后远端归档前后、下载归档和签名前再次校验。屏障前 stale 可重验，屏障后 `artifact_stale` 终态销毁，发布只读取已签名不可变制品
- Sandbox Modal 可恢复当前 Session 的最新状态、日志、Diff 与验证证据；刷新和后端重启都不会重放模型/命令，启动时未完成操作统一标记 `interrupted`，发布必须由用户重新勾选显式确认；删除 Session 前会先停止活跃请求并释放 Harness

## 数据与生命周期

默认开发数据根目录为 `.pi-agent-data/`：

| 数据 | 默认位置/生命周期 |
|---|---|
| 账号与登录 Session | `.pi-agent-data/auth.sqlite`；账号保留，登录 Session 在后端重启时撤销 |
| 账号工作区 | `.pi-agent-data/users/{user_id}/` |
| Session/消息/operation records/Skills/MCP/Provider/Sandbox metadata | 用户目录下 `workspace.sqlite`；Sandbox operation 与有界事件流同库持久化 |
| Session 文件 | 用户目录下 `uploads/{session_id}/` |
| Sandbox 本机状态 | 主应用 baseline 来自 `uploads/{session_id}` 的 WorkspaceStore revision；`coding-sandbox-staging/` 保存临时 materialization、snapshot、制品和隔离发布镜像，Workspace 事务 journal 位于 Session 隐藏目录并由 Store 恢复；`coding-sandbox-publisher/` 与 `coding-sandbox-projects/` 仅服务未配置 WorkspaceStore 的本地目录兼容组合 |
| 旧 Chunk Knowledge（磁盘退役） | 若历史用户目录存在 `knowledge/knowledge.db` 与 `knowledge/libraries/`，产品启动不打开、不迁移也不删除；仅 `wiki/legacy.py` 的显式归档流程可处理 |
| LLM Wiki | 开发启动器使用 `knowledge/wiki.db`、`knowledge/spaces/` 与 `knowledge/legacy/`；schema v8 Raw 原件/不可变 parse revisions、MinerU 三档模式、页面镜像、FTS5、图谱、Change Set 与 Conversation 均由新 Store 管理 |
| API Key | OS Keyring、显式 session-only memory 或显式 env；不写入 SQLite 明文 |
| Agent Telemetry | `.pi-agent-data/telemetry.sqlite`；跨账号集中存储结构化运行元数据，默认 30 天且最多 50,000 spans；正文与凭证不采集 |
| Active request / pending approval / event subscribers | 当前后端进程内存；后端重启不恢复执行 |

## 2026-09-06 Bash 阶段 2D 验证

- 最终相关合并复核：**209 passed / 3 skipped**，含 Wiki API 全部初跑失败项与本轮执行路径。
- Frontend **221 passed**、Chromium 24 个用例最终状态 passed、Ruff、strict Mypy **286 files**、生产构建与 Evals 通过。
- 实机 Docker Web Coding/Plan **2 passed**；测试容器已回收，未启用真实部署或发布到用户 Workspace。
- 全量初跑 **2551 passed / 7 failed / 9 skipped / 9 deselected**：6 项深临时路径问题用短路径复核，
  1 项旧拒绝错误码断言对齐新审批契约。最终合并复核通过，但 Wiki 产品长路径兼容仍列入 TODO。
- 累计 coverage **78.09%** 为全量初跑与增量的合并诊断值；不是收尾后同一快照的单次全量发布门禁。

详细结果与边界见 [阶段 2D 验证记录](docs/validation/workspace-bash-stage2d-2026-09-06.md)。

## 2026-09-06 Bash 阶段 2C 验证（历史阶段）

- 最终相关回归：**398 passed / 2 skipped**，53.94 秒；新增生命周期专项 26 项。
- Ruff、strict Mypy **283 source files**、6 suites / 10 配对案例 Evals candidate gate 通过。
- 原固定镜像真实 Docker **25 项**通过，无新安装/镜像构建；测试容器回收，未发布到真实 Workspace。
- 该阶段未跑全量后端/coverage 或前端/E2E，当时 Web 尚未接线；随后阶段 2D 已接通 Coding/Plan，最终验证以阶段 2D 记录为准。

见 [阶段 2C 验证记录](docs/validation/workspace-bash-stage2c-2026-09-06.md)。

### 阶段 2B 历史基线

- 最终相关回归：**314 passed / 2 skipped**，31.78 秒；其中新增 Bash/公共许可/Plan 原子适配专项 57 项。
- Ruff、strict Mypy **282 source files**、6 suites / 10 配对案例 Evals candidate gate 通过。
- 新镜像真实 Docker **20 项**两次通过；最后补充的投递期间取消检查另行通过上述最终回归。
- 本次未跑全量后端/coverage 或前端/E2E；当前 Web Bash 尚未注册启用。

见 [阶段 2B 验证记录](docs/validation/workspace-bash-stage2b-2026-09-06.md)。

### 阶段 1/2A 历史基线

- 后端全量：**2463 passed, 7 skipped, 9 deselected**；1133.32 秒，coverage **77.18%**，达到 75% 门槛。
- 单文件限额补丁另跑最终相关回归：**154 passed**；授权 41、Docker Backend 44、既有相关 69。
- Ruff、strict Mypy **293 files**、本地 Evals candidate gate、真实 Docker **14 项**全部通过。
- 本轮没有重跑前端/Chromium E2E，不构成 Workspace Bash 五阶段最终门禁；产品接入仍未完成。

见 [阶段 2A 验证记录](docs/validation/workspace-bash-stage2a-2026-09-06.md)。

## 2026-09-05 验证记录（历史）

当前 `0.0.29` 后续代码持续验证离线 Backend、Frontend、Chromium E2E、Evals 与静态门禁。
本轮完成 Session History / Structured Memory 阶段 1–4，并补齐刷新后工具结果的终态恢复；
详细记录见 [`session-history-memory-2026-09-05.md`](docs/validation/session-history-memory-2026-09-05.md)。
此前固定统计和逐次确认 Python 分析证据分别见 [`data-analysis-2026-09-05.md`](docs/validation/data-analysis-2026-09-05.md)
与 [`python-data-analysis-2026-09-05.md`](docs/validation/python-data-analysis-2026-09-05.md)。
先前上传与媒体设计证据保留在 [`workspace-upload-media-2026-09-05.md`](docs/validation/workspace-upload-media-2026-09-05.md)。
Worker、wheel、真实 E2B 与 DDGS/GLM 沿用最近一次保留证据；Parser OCI 仍未通过实机 Gate。
下表标注专项/历史的行是阶段证据，不是本轮重新运行的数字：

| 验证 | 结果 | 备注 |
|---|---|---|
| Ruff 全量 | **PASS** | `ruff check src tests scripts evals`；0 errors |
| strict Mypy 全量 | **PASS** | `mypy src evals`：277 source files / 0 issues；包含只读历史与结构化记忆模块 |
| Wiki Parser Worker 静态门禁 | **PASS** | Ruff 0 errors；strict Mypy 18 source files / 0 issues |
| Backend 全量离线（第二轮前基线） | **3143 passed, 7 skipped, 9 deselected** | 当时为 3159 collected；`pytest tests --tb=short -q`；1053.41s；coverage 81.91% |
| Backend 当前精简套件 | **2243 passed, 7 skipped, 9 deselected** | `pytest tests --tb=short -q`；540.83s；coverage 76.59%；包含 12 项历史/记忆新增回归。之后收紧非原始消息证据角色过滤，并通过最终定向 36 项、Ruff/Mypy；排除真实外网、LLM 与 Docker marker |
| Frontend 当前精简套件 | **202/202 passed** | 30 files；含终态工具结果恢复的 2 项新增回归；同时通过 typecheck、ESLint 与 production build |
| Session History / Structured Memory 阶段 1–4 | **最终定向 36/36 passed** | 只读/隔离、中文搜索/分页、原文投影、原子索引初始化、结构化增量/no-change、来源/更正/手工固定、分支失效、Web Agent 调用与重启恢复；压缩实现未改 |
| 逐次确认 Python 分析 | **Backend 18/18；Chromium 2/2（含在完整 24/24）** | 独立环境真实执行、完整代码/来源确认、None/AllowAll 不绕过、拒绝/取消/超时、来源复查、错误行号、CSV/PNG/报告保存、重启不重放；见 `docs/validation/python-data-analysis-2026-09-05.md` |
| 可选 Data Analysis 专项 | **Backend 27/27；Chromium 1/1（包含在完整 22/22 中）** | 真实工作进程、四类文件/图表、Web Agent 调用、默认关闭/Session 隔离、取消/超时/恢复、迁移 rollback、显式保存、来源 hash、无正文 Telemetry；uv lock 一致性 PASS |
| Workspace upload 拖拽专项 | **Chromium 5/5 passed；Checkpointer 11/11 passed** | 覆盖聊天/右栏真实拖拽、原件归档、同名文件刷新恢复、Excel 转换、工作代码摘要重启恢复；完整门禁见专项验证文档 |
| pi Evals 专项 | **18 passed；5 suites / 10 observations；candidate gate PASS** | 覆盖数据契约、Judge、配对/诊断、真实 Application/Runtime/Session、工具/Workspace、统一资源装配、SQLite reload、Telemetry 隐私、默认/显式正文产物和离线依赖边界；默认 JSONL 泄漏扫描 PASS |
| pi Telemetry 专项 | **21 passed** | 覆盖 noop/memory/nested span、SQLite 并发/持久/筛选、passive failure、正文脱敏、Agent event 投影、Admin API、跨账号查询、Auth v1→v2 和 package 依赖方向 |
| pi Telemetry wheel | **PASS** | 离线构建主 wheel，共 444 entries；Core/Web Telemetry 与前端资源在包内；隔离安装导入 PASS |
| pi SQLite Session backend 专项 | **94 passed；共享服务邻接 152 passed；兼容回归 75 passed** | 覆盖 Repository/Storage/Search、migration、统计、fork/branch cache、writer fence、取消 rollback、Web Runtime handle、Extension/Plan 共享 connection 与未启动 lifespan 的兼容投影 |
| pi SQLite Session backend wheel | **PASS** | 离线构建主 wheel，共 428 entries；migration SQL、SQLite storage 子模块与 Coding Agent Core 均在包内；`python -I` 隔离导入和 migration 初始化 PASS |
| pi `coding-agent` 阶段 1–3 专项 | **Core 17 passed；Web Session 映射 57 passed** | 覆盖工具集解析/恢复、请求级 Provider/Skill/MCP/Workspace/Prompt composition、Session/Harness 隔离、跨 Session 并行、删除后状态隔离、关闭竞态、产品包依赖边界与 Web 执行/持久化回归 |
| pi `coding-agent` wheel | **PASS** | 离线构建主 wheel，共 407 entries；新增 `coding_agent_app/core` 模块均在包内；`python -I` 隔离导入 Agent/Harness/SQLite/Runtime/Resource Loader PASS |
| pi `ai` 对齐专项 | **159 passed** | provider history/image/usage/retry 新增回归及 Anthropic/OpenAI/Factory/Context Budget/Compaction 邻接测试；2.36s，0 failure |
| pi `agent` 对齐专项 | **36 passed；邻接 127 passed** | Agent/P0 lifecycle/control queue/public state 36 项；再含 Session/Compaction/ToolResult/Coding Sandbox 邻接共 127 项；0 failure |
| pi package 结构专项 | **7 passed；wheel PASS** | 旧/新导入对象身份、AI/Agent AST 依赖方向与旧 facade 约束；wheel 393 entries，隔离安装后 Agent/Harness/SQLite canonical import 通过 |
| LLM Wiki 发布候选定向回归 | **Backend 67 passed, 1 skipped；Frontend 16/16；Browser 1/1** | 覆盖 Store/API/Summary/Fake Parser、五个 Wiki 前端视图与 source→approval→page→graph→conversation 浏览器主链路 |
| Workspace 阶段 1 定向回归 | **125 passed** | `VirtualFileStore` 兼容别名、双根初始化、并发幂等、旧路径/purpose 迁移、固定根删除保护、Checkpointer/Auth/重启；`-W error` 下 0 warning |
| Workspace 阶段 2 定向回归 | **116 passed** | 代码 `scripts/**` 映射、安全逻辑路径、revision 持久/冲突、Markdown CRUD、Agent 工具与 Web API；使用 `--no-cov` 定向运行 |
| 自动 Coding 编排定向回归 | **Backend 31 passed；Frontend 21/21；real E2B PASS** | 新增 2 项测试覆盖自动创建→验证→冻结待批准，以及验证失败不冻结；全仓 Ruff、strict Mypy 171 files、Frontend typecheck/lint/build 通过；真实 E2B 由新编排器完成创建、9 工具、重验、冻结、签名、模拟批准回写与销毁（25.328s） |
| LLM Wiki Parser Contract v1 兼容边界 | **保留，非默认路径** | 仅供旧 API/provider 显式兼容；新上传、开发启动器与产品 UI 全部走 MinerU Contract v2，不包含旧 PDF 引擎运行时 |
| LLM Wiki MinerU 源码级专项 | **64 passed；最终邻接 60 passed** | Contract/Fake、三档路由、MinerU adapter 输出规范化/图片去重、单 attempt、queue Provider、Ingestion/API/恢复、不可信 artifact、合规归档；最终变更同时已进入 Backend 全量门禁 |
| LLM Wiki MinerU OCI/真实语料 | **环境阻塞，待验证** | 当前主机未安装/Expose Docker CLI；新镜像尚未执行模型预取、CPU pipeline、CUDA medium/high、断网、取消/恢复与代表性 PDF smoke；`runtime_ready=false` |
| LLM Wiki Raw revision 专项 | **5 passed** | 重解析历史保留、selected version 防 ABA、失败保留 selected 并记录 attempt、启动修复 pointer、跨 Source FK 隔离 |
| LLM Wiki Source retention | **2 API + 4 Worker + 4 View passed** | 删除后 Raw 立即不可读、零保留期安全物理清理、active Page 来源阻断；独立 retention 协程不干扰 Parser queue；定向 Ruff/Mypy/typecheck/ESLint PASS |
| LLM Wiki Space lifecycle | **2 API + 1 Store + 2 Workspace passed** | archive/restore、空 Space 安全删除、active 内容 409 阻断；Ruff/Mypy/typecheck/ESLint PASS |
| LLM Wiki archived read-only | **2 API + 4 Worker passed** | archived 上传代表路径返回 `space_read_only`；解析/Summary Job 与归档竞态由事务门禁统一收敛；Ruff/Mypy PASS |
| LLM WikiStore/Files/Legacy | **52 passed, 4 skipped** | schema/version/capability/integrity、Space CRUD/双连接 CAS、路径/原子写入/镜像恢复、legacy 一致备份/故障顺序和旧 RAG import 隔离；skip 为 Windows symlink capability |
| LLM Wiki 阶段 1–3 定向回归 | **104 passed, 4 skipped** | Provider contract、Store/Files/Legacy、Source/Artifact/Job、HTML、Fake PDF、安全 tar 导入、API/lifespan、并发 claim 与崩溃后新 attempt；skip 为 Windows symlink capability |
| Coding Sandbox 安全矩阵 | **151 passed, 2 skipped** | 离线 Fake/E2B 契约、路径/命令/网络/凭证攻击、故障注入、validation/artifact 篡改、Publisher 冲突/回滚/崩溃恢复，以及退役 Provider 空字段兼容迁移；Windows capability skip |
| Sandbox Web 生命周期/API | **7 passed** | SQLite operation/event 恢复、启动 `interrupted` 收敛、snapshot seed、验证失败不得冻结/发布且真实工作区字节级不变、取消，以及 disabled/missing/latest API 边界 |
| 真实 E2B + WorkspaceStore 发布 smoke | **PASS** | 9 个代码工具、故意验证失败与冻结拒绝、恢复重验、制品冻结/签名、审批门禁、WorkspaceStore 单 revision 回写与 Sandbox destroy；27.171s；未输出凭证 |
| Complete-turn Compaction 定向回归 | **50 passed** | 完整 turn/token target、超预算最新 turn、token/window、previous summary、retry lifecycle、失败不改源消息、Web API 与 durable-operation 邻接回归 |
| Durable recovery 定向回归 | **PASS** | operation intent/effect/finish、同进程无模型重试、启动前滚、source-leaf conflict 保留消息、文件 pointer rollback、JSON torn-tail / legacy migration |
| 自动 Session Memory 阶段 2 | **11 passed** | 1 个既有测试文件新增 2 项：成功更新且不清空消息、Provider 失败保留回答并在下一轮恢复；另复用 2 条 Regenerate 主路径。Ruff、strict Mypy、Frontend typecheck/lint PASS |
| Coding Workspace 内容分类阶段 3 | **124 Backend + 8 Frontend passed** | 只修改 1 个既有 Backend 测试文件中的 2 项；覆盖 `inputs/**`/`artifacts/**` 默认路由、公开 ownership/mutation metadata 与系统路径拒绝，并复用 Workspace/文件工具/Sandbox lifecycle 和右栏现有回归。全量 Ruff、strict Mypy 177 files、Frontend typecheck/lint PASS |
| Coding Workspace 代码连续性阶段 4 | **30 Backend + 85 邻接 + 6 Frontend passed** | 只修改 1 个既有 Backend 测试文件并新增 2 项；覆盖 revision-bound 固定摘要、普通上传不伪造验证，以及 renderer 故障时上传保留且状态 stale/failed；复用 Workspace/Sandbox lifecycle 与右栏现有测试。Ruff、strict Mypy、Frontend typecheck/lint PASS |
| Coding Workspace 无上下文续作阶段 5 | **43 Backend + 85 邻接 + 6 Frontend passed** | 只修改 1 个既有 Backend 测试文件并新增 2 项；覆盖零聊天历史进程重启后从 AGENT/Memory/current code/tree 续作，以及 Pending Memory、待批准 Artifact、stale code summary 同时存在时的可信边界；Ruff、strict Mypy、Frontend typecheck/lint PASS |
| Session tree 定向回归 | **69 passed** | immutable entry/lane、旧库迁移、branch/fork/label/active leaf、重启、Web API、revision sibling 与 trailing suffix |
| ToolResult metadata 定向回归 | **173 passed** | usage / added names、hook 边界、消息/事件、provider context、Snapshot、Session/SQLite、Web serializer 与旧数据默认值 |
| Agent 公开状态定向回归 | **137 passed** | Agent、Harness、stream、Provider runtime 与 Web state；系统 temp ACL 阻断项改用工作区 `basetemp` 后通过 |
| DDGS + GLM 真实 smoke | **3/3 passed** | 固定 secret-safe 脚本；DDGS 1 项 + GLM 2 项；24.63s；未输出凭证 |
| 真实测试门禁回归 | **3 skipped** | 手工选择 `-m integration` 但未设置 `PI_RUN_INTEGRATION=1`，确认不触网 |
| Frontend Vitest 历史全量 | **352/352 passed** | 30 files；旧 Chunk Knowledge UI/Store 测试随产品退役删除，新 Wiki API/Store/Views/Route/Knowledge Chat 回归已覆盖 |
| Frontend typecheck | **PASS** | `vue-tsc --noEmit` |
| Frontend ESLint | **PASS** | `eslint . --max-warnings=0` |
| Frontend production build | **PASS** | `vite build`；0 mixed dynamic/static import warning |
| 版本/许可证一致性回归 | **3 passed** | Python、两个 FastAPI、前端 package/lockfile 与根 LICENSE 元数据一致 |
| `0.0.29` wheel 构建 | **PASS** | 主 wheel 410 entries，包含 9 个 `wiki_parser` 文件且不携带 MinerU/Torch/Transformers；Worker wheel 30 entries，MIT/MinerU notice、路由配置、manifest 与 SBOM 全部通过 verifier |
| `0.0.29` 最小发布回归 | **PASS** | Ruff；strict Mypy 179 source files；Backend 51；Frontend 11、typecheck/lint/build；不触发真实网络、E2B 或完整 Playwright |
| B7 定向回归 | **248/248 passed** | SQLite Store lifecycle/open failure |
| Keyring 定向回归 | **94/94 passed** | Runtime、launcher 与 restart 范围 |
| 真实 Windows Keyring 探针 | **write/read = true；cleanup = true** | 随机非用户值，执行后删除；同账号/同解释器复验与安全取证步骤已形成 Windows smoke 文档 |
| MCP/DDGS UTF-8 定向回归 | **34 passed, 1 deselected** | 含真实 Python 子进程中文 round-trip |
| Browser E2E | **24/24 passed** | Chromium；11 个规格、单 worker、`CI=1 --retries=0`；最终 1.4m；覆盖固定/Python Data Analysis、Sandbox、Compaction、Approval、Wiki、MCP、Session refresh、Telemetry Admin 与 Workspace 拖拽，以及扩展延迟时实时恢复；修复迭代失败后最终 0 retry / 0 flaky / 0 failure；posttest 恢复生产构建，过程见本轮验证记录 |
| Context Compaction Browser E2E | **1/1 passed** | 修复逐轮 Prompt 未等待 202 导致的测试自身竞态；沙箱外真实启动浏览器；production build 由 posttest 恢复 |

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

- 每个持久 Web Session ID 已映射独立 `CodingAgentRuntime` Session、Agent/Harness 状态机与可释放 SQLite `SessionStorage` handle；同 Session 单请求串行，不同 Session 可并行。默认 Session 只把入参 Harness 作为兼容投影，其他 Session 从模板克隆；宿主自定义 ModelClient 行为保留，Provider transport 关闭所有权不转移
- 普通 Prompt/Regenerate active request 与 pending approval 不跨后端重启恢复；浏览器刷新只恢复仍在当前进程运行的请求。Checkpointer 可对已接受 intent 前滚；Managed Sandbox 只恢复持久状态/事件并把重启前未完成操作标为 `interrupted`，不重放模型、命令或发布
- Human Approval 只有 Approve once / Deny；没有永久授权
- Context Budget 是带安全余量的确定性近似，不是 Provider 官方 tokenizer
- 普通 Web Context compaction 支持自动/手动有界 LLM 摘要及持久工作视图，原消息与 Memory 不变；
  SDK 旧规则式接口保持兼容。自动 Session Memory 与 `/checkpointer` 仍独立，前者保留消息、后者成功后清空当前 lane
- Regenerate 仅支持最新 Assistant，不提供历史 revision 切换 UI
- Session tree 的 Core/Web API 已完成；当前聊天 UI 尚无可视化 branch/lane navigator
- MCP stdio command 是可信本机代码执行边界；Streamable HTTP endpoint 允许用户配置，但只作为当前 Web Agent 的扩展 transport，不提供独立远程 Agent 服务
- `added_tool_names` 已保留到 provider 边界，但当前 OpenAI/Anthropic-compatible adapters 不实现原生 deferred tool loading，仍按完整 ToolRegistry 发送工具
- 通用、无正文的 `web.request` span 已覆盖 Prompt、Regenerate 与 Checkpointer；Knowledge Prompt 沿用同一入口。当前没有 Wiki 子步骤专用 span、OTLP exporter、外部告警或完整 RBAC
- 本机 Evals 当前只使用脚本化 Fake Provider，分数代表编排、隔离、持久化与安全契约；不能解释为开放式模型智能或自然语言 Prompt 的真实质量差异

### 文件与 LLM Wiki

- Session 文件统一到 `WorkspaceStore` 事实源；新上传原件放在逻辑 `upload/**`，工作代码在 `scripts/**`，Sandbox 已事务发布回同一事实源；右侧成果面板展示代码、Markdown 和固定文档转换产物
- Session PDF/DOCX/XLSX 原件在 `upload/**`，转换产物在 `documents/<stem>-<source-id>/`；Agent/右栏读取生成的 `content.md`、CSV/schema 与 assets，二进制原件只提供元信息/下载且不可变。历史 `documents/<id>/original.*` 可继续重解析
- Wiki PDF 扫描件由用户选择的 MinerU 档位处理；`gpu-high` 启用图片/图表分析。Session 固定文档转换目前仍使用 pypdf/python-docx/openpyxl，不含 OCR
- 图片可上传和预览，但 Workspace OCR/视觉理解与视频链接解析尚未实现；本机异步任务方案见 [`workspace-upload-media.md`](docs/design/workspace-upload-media.md)
- Wiki 只对已批准页面使用 SQLite FTS5/BM25，不建立 Chunk、向量数据库或 embedding 主链路
- 新 Wiki 已独立接入 Web lifespan/API；真实用户旧 Chunk 库原样保留但产品不再打开。MinerU Contract v2 与 OCI file-queue Provider 已接入；开发启动器默认装配本机 exchange，Worker 未启动时 PDF fail closed
- 旧记录中已经写入的 Unicode replacement character `U+FFFD` 仍无法从现有数据反推出原字符；当前会在读取时把它标记为“疑似编码损坏”并显示受影响字段，但不会猜测或写回所谓修复

### 工程债务

- 当前无 Git remote；tag/push 需要先决定版本并配置 remote
- 旧 `.pytest_cache` 仍受本机 ACL 限制，但 pytest 已固定使用可写的 `.pytest-cache-workspace` 与 `.pytest-tmp`，不再读写旧目录或关闭 cacheprovider
- Coding Sandbox P0 第 0–10 项和 Workspace 阶段 4A–6 已完成；显式状态机/CAS、双 TOCTOU、revision-bound baseline、事务发布、固定文档转换、右栏成果刷新与发布门禁均已固定

## 当前阻塞项

LLM Wiki 阶段 0–10、Session Workspace 阶段 1–7 与 Coding Agent Workspace 连续性阶段 1–5 已完成。MinerU 源码与 Web 产品闭环已完成，但真实 OCI Worker 仍为 `runtime_ready=false`，需在具有 Docker/CUDA 的主机完成 CPU/GPU/断网语料门禁。当前 release tag 为 `0.0.29`；仓库仍未配置 Git remote。

## 建议下一步

1. 完成阶段 2D 剩余的独立 `run_bash`：接入完整脚本/cwd/预算确认及可信工具上下文，覆盖拒绝、Stop、过期与刷新不重放，再显式注册；已有 Coding/Plan 任务审批和调度无需重做，发布保持单独确认。
2. 此后按计划实施 Docker 制品发布、Web/Admin/Telemetry/后台清理和完整门禁。本项目不追随 pi 的 `client/protocol/server/tui` 产品形态。
3. 如需发布到远端，先配置 Git remote 再单独授权 push；Modal 与图片能力继续按既有决定暂缓。

未完成事项的唯一清单见 [`TODO.md`](TODO.md)。使用与架构说明见 [`README.md`](README.md)。
