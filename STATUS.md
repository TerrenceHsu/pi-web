# Project Status

> 当前事实快照，校准日期：**2026-08-29**。本页只描述当前代码基线；阶段性测试数字和历史决策保留在 `docs/validation/`、`CHANGELOG.md` 与归档计划中。

## 基线身份

| 项 | 当前事实 |
|---|---|
| 代码基线 | `0.0.28` 发布基线之上已完成 Coding Sandbox 阶段 4A–4C、Session Workspace 阶段 5–7；Coding Agent Workspace 连续性阶段 1–3 已提交至 `1535485`，当前工作树完成阶段 4 代码连续性 |
| 分支 | `master` |
| 最新 release tag | `0.0.28`；annotated tag 指向发布基线，不包含其后的阶段 4A–6 提交 |
| Python / API 版本 | `0.0.28`（Python `__version__`、workspace FastAPI 与 Auth gateway 共用同一来源） |
| 前端包版本 | `0.0.28`（`package.json` 与 lockfile 一致） |
| 许可证 | MIT；根 `LICENSE` 为标准正文，`pyproject.toml` 与 wheel 均直接引用/携带该文件 |
| Git remote | 当前仓库**未配置 remote**，因此尚无可执行的 push 目标 |
| Python | 声明支持 `>=3.11`；本机验证使用 Python 3.12.13（conda `pipy`） |
| 产品边界 | localhost-only 本地 Agent 工作台；不是公网 SaaS |

> `0.0.28` 是当前 Python、API、前端 package 与 release tag 的统一发布基线。

## 当前交付状态

LLM Wiki 阶段 0–10 已完成：PDF 采用 PyMuPDF4LLM fast + Docling accurate/auto fallback，HTML
保持零网络；Raw、Agent Summary、入口/主题页面、Change Set 审批、页面 FTS5、知识图谱、每 Space
多 Knowledge 对话、独立前端、来源保留、Space 生命周期和归档只读门禁均已接通。产品组合不再启动
旧 Chunk Knowledge DB/Worker/Tool/API/UI：

| 能力 | 状态 | 代表性基线 |
|---|---|---|
| Step 1–21 Core Runtime | ✅ 完成 | 归档见 `docs/archive/legacy-plans/PLAN_STEP_1_21.md` |
| Multi-provider Runtime / UI | ✅ 完成 | GLM、Qwen、Kimi；Anthropic-compatible 后端能力 |
| 旧 Chunk Knowledge PDF → Chunk FTS5 → Citation | 🗄️ 历史兼容 | 实现仅保留显式兼容入口；默认套件只保留不可误启动守卫，不再进入产品组合或前端 |
| 登录与账号工作区隔离 | ✅ 完成 | `b529bbc` |
| Session Workspace、`AGENT.md`、`Memory.md`、`/checkpointer` | ✅ 完成 | `WorkspaceStore` 为唯一规范事实源；新旧 Session 幂等初始化两个固定根文件，保留旧正文/file id；设计见 `docs/design/workspace-sandbox-integration.md` |
| Workspace 模块分离 | ✅ 阶段 1 完成 | 规范 Store、固定文档转换和 provider-neutral continuity 已迁移到顶层 `agent_workspace`；Workspace/Sandbox adapter 位于 `coding_agent_app`，旧 Core 路径只保留兼容层；独立 import boundary 与 wheel 内容验证通过，Ruff、strict Mypy 177 files、既有定向 133 passed |
| 每轮自动 Session Memory | ✅ 阶段 2 完成 | Prompt/Regenerate 以有界 turn evidence 和串行 durable operation 累计更新 `Memory.md`；失败不影响主回答并在下一轮 preflight 恢复，Coding 待审批期间按 Sandbox blocker 延迟以保护 Workspace revision，Knowledge 模式跳过；产品入口默认启用 |
| Coding Workspace 内容分类 | ✅ 阶段 3 完成 | `WorkspacePathPolicy` 统一 HANDOFF/tasks/docs/scripts/inputs/artifacts/documents 所有权与写入/发布边界；普通上传进入只读 `inputs/**`，Agent 非代码产物进入 `artifacts/**`，空目录保持惰性；Backend 定向 124 passed，strict Mypy 177 files、Frontend typecheck PASS |
| 已发布代码流程总结 | ✅ 阶段 4 完成 | revision-bound `scripts/**` 生成固定 architecture/code-flow/validation；mutation 先 stale、全套持久化后 current，并发/失败 fail closed；真实 Sandbox evidence 与普通上传“未验证”严格区分；Backend 30 + 邻接 85 passed，Frontend 6/6，静态检查通过 |
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
| Release metadata 与 MIT License | ✅ 完成 | `8a6ff2e`；Python/API/前端统一 `0.0.28`，wheel 携带根许可证 |
| 当前发布前浏览器/联网门禁 | ✅ 完成 | 精简 Playwright 19/19、0 retry/flaky；真实 E2B Managed Sandbox/审批/WorkspaceStore 回写 PASS；此前 DDGS + GLM 真实 smoke 3/3 |
| Managed Coding Sandbox P0 0–10 | ✅ 完成 | 独立包、快照、E2B、代码工具、固定验证、签名制品、本机事务 Publisher、Web 生命周期/状态恢复/审批发布 UI，以及真实 E2B、完整 CI、攻击矩阵和 Browser E2E 验收 |
| 自动 Coding 请求编排 | ✅ 完成 | Chat `Code` 模式自动创建/复用 Sandbox，本轮仅暴露 9 个 `coding_*` 工具；Agent 结束后 Backend 独立重验并冻结到 `awaiting_approval`，绝不自动发布；验证失败保留可修复状态 |
| Coding Sandbox 阶段 4A 状态机/TOCTOU | ✅ 完成 | Backend 单一转换表与公开 actions/transitions、SQLite 完整记录 CAS、UI 动作投影；Validation→Freeze 屏障前 stale 可重验，屏障后 `artifact_stale` fail-closed 终止 |
| Coding Sandbox 阶段 4B Workspace baseline | ✅ 完成 | WorkspaceStore mutation lock 内按 logical path 物化 revision-bound 树，逐文件稳定 stat/SHA 校验；operation 记录源 revision/tree SHA，主应用不再以独立项目目录作为输入事实源；4C 前发布 fail closed |
| Coding Sandbox 阶段 4C Workspace 发布 | ✅ 完成 | 签名 Artifact 在隔离镜像复验后，通过 WorkspaceStore journaled multi-file transaction 发布；路径白名单、目标端 revision/tree/content TOCTOU、rollback/recovery、单次 revision 和 `workspace_changed` 右栏刷新均已接通 |
| Session Workspace 固定文档转换 | ✅ 完成 | PDF/DOCX/XLSX 原件进入 `documents/<id>/original.*` 且不可变；本地固定转换器从 revision 快照生成只读 Markdown/CSV/schema/assets/manifest，并通过 WorkspaceStore 单 revision 事务发布；OCR 延期，完全独立于 LLM Wiki Parser |
| Backend warning / pytest 状态目录 / ToolResult UTF-8 E2E | ✅ 完成 | Backend `-W error` 0 warning；cache/temp 固定到工作区；Playwright 48/48 |
| Frontend warning 收敛 | ✅ 完成 | Modal/Teleport attrs、Vite mixed import 与 Playwright color env 三类提示归零；Vitest 404/404；Playwright 48/48 |
| 历史消息 U+FFFD 完整性标记 | ✅ 完成 | 读取时递归检测并返回计数/RFC 6901 路径，不改写 SQLite、不伪造恢复；消息与工具卡可见，刷新保持；Playwright 49/49 |
| LLM Wiki 产品合同与 Parser Provider Gate | ✅ Contract/Fake/OCI Provider v2 完成并实机验证 | 新 Wiki 保持无 Chunk-RAG；Marker 路线停止；Contract v2 固定 PyMuPDF4LLM fast、Docling accurate/auto fallback、逐页制品、同源 attempt 证据、hash-pinned config、原始 PDF 唯一事实源与 AGPL-3.0 路径；主应用通过无 Parser 依赖的 file queue Provider 连接外部 Worker |
| LLM Wiki AGPL Worker 合规包 | ✅ `runtime_ready=true` | 独立 `workers/wiki_parser_worker`、AGPL-3.0-only、35 文件精确/确定性 Source Offer、SPDX 2.3 SBOM、88 包 uv lock、hash-required Linux resolution、三项 Artifex 源码物化器、digest-pinned OCI/断网 Compose、持久 supervisor/child 与 wheel verifier；Docker 29.7.2/Linux-amd64 下镜像 bundle/notices、隔离、真实 fast/accurate/复杂/OCR/fallback 与取消恢复均通过 |
| LLM WikiStore、目录与旧库退役 Gate | ✅ schema v7 | 页面中心型 Store 已覆盖 Summary/Proposal/Page/Revision/Change Set/FTS5/Edge/Conversation；旧 Wiki flat schema 明确要求重建，旧 Chunk 库不自动打开、不迁移、不删除 |
| LLM Wiki Raw Ingestion | ✅ 完成 | 不可变 PDF/HTML、零网络 HTML、双 Parser Contract v2、Raw parse revisions、不可信 artifact、恢复、真实 OCI Worker，以及 Space/Source/状态/Raw artifact 前端均已完成 |
| LLM Wiki 页面中心主链路 | ✅ 完成 | Agent Summary → 入口页/主题页 Proposal → 单一 Change Set diff → 用户审批 → Page/Revision/FTS5/Graph 原子发布 |
| 独立 Knowledge 页面与 Agent | ✅ 完成 | `/knowledge` 提供 Pages/Sources/Graph/Changes/Conversations；每 Space 多对话复用 Agent 流事件，采用独立 Prompt/Skill/10 工具白名单 |
| 旧 Chunk Knowledge 产品退役 | ✅ 完成 | 删除旧前端；开发/E2E 不再打开旧 DB、Worker、`search_knowledge` 或 REST；Backend 仅保留 `enable_knowledge_api=True` 显式兼容入口 |

## 当前产品能力

- 未登录先进入 Username/Password 页面；空认证库幂等创建本地初始账号 `admin / 123456`
- 每个账号拥有独立的 Session、消息、文件、Skills、MCP、Wiki Space/Conversation、Provider/Credential 配置
- Session 路由为 `/chat/{session_id}`；刷新恢复准确 Session、历史、文件树、`AGENT.md`、`Memory.md` 及当前请求状态
- 每个 Session 由唯一 `WorkspaceStore` 初始化独立文件夹和唯一根 `AGENT.md`、`Memory.md`；启动时幂等补齐旧 Session，保留已有正文/file id，两个根文件不可删除；`VirtualFileStore` 仅为同一实现的兼容别名
- Agent 写入或用户上传的代码按扩展名自动进入逻辑 `scripts/**`；普通上传进入不可原地改写的 `inputs/**`，Agent 非代码交付物默认进入 `artifacts/**`，共享笔记使用 `docs/notes/**`
- 文件 API 和右侧 Workspace 面板直接消费 `WorkspacePathPolicy` 的分类、所有者、可编辑/移动/删除、Agent 写入、Sandbox 发布及不可变标记；系统连续性文件不能由普通用户/Agent 文件入口伪造
- Workspace revision 以隐藏状态持久化；上传、创建、更新、移动和删除可同时校验 revision 与逐文件 SHA，过期客户端收到 409 而不会静默覆盖
- 用户代码上传/删除和批准 Sandbox 发布会从实际 revision 更新三份只读工程摘要；每份记录代码树 SHA 和来源 revision，右栏公开 current/stale/failed，启动可恢复未完成或旧 Session 摘要
- PDF、DOCX、XLSX 上传会归档不可变原件并生成可审计只读制品；右栏优先打开 `content.md`，XLSX 公式不会在转换阶段执行，失败不会暴露半套输出
- 桌面右栏是 Agent 成果交付面：成功生成 `.md`、`.py` 等文件后立即刷新、选中并展示，整页刷新后仍从持久 ToolResult 恢复；窄屏使用带新成果提示的 drawer
- 每个成功普通 Session Prompt/Regenerate 自动使用当前 Provider 累计更新 `Memory.md` 且不清空消息；失败保留有界 evidence 并在下一轮 preflight 恢复，Prompt/request 的 `continuity` 和 `/api/state.auto_memory` 公开当前状态
- `/checkpointer` 保留为显式“总结完整当前对话并清空 lane”操作；接受时持久化 source leaf/hash，文件发布后原子清空原 lane，进程退出可幂等前滚
- 支持 Prompt、Stop、Regenerate 最新 Assistant、Markdown Export、实时事件、请求恢复和精确 ToolCall 审批
- Provider Profile、Session Model Binding、Context Window 与 Max Output Tokens 持久化；UI 管理 GLM/Qwen/Kimi
- 持久化凭证默认进入 OS Keyring；开发启动器在监听端口前执行 write/read/delete 探针
- MCP 支持 stdio tools/prompts；内置 DDGS 固定存在、不可删除，可修改返回数、地区、安全搜索、时间范围等参数
- 独立 `/knowledge` 页面支持 Wiki Space、PDF/HTML Source、Raw artifacts、页面/revision、页面 FTS5、已发布图谱、统一 diff 审批与每 Space 多 Agent 对话
- `create_app(wiki_root=...)` 运行独立 Wiki Store/API/Worker；HTML 真实离线解析，PDF 未配置 Provider 时保持 `uploaded`。旧 Chunk Knowledge 默认完全关闭且 API 为 404，不与 Wiki 共享表或业务数据
- Core Runtime 的一个 Turn 等于“一次 LLM 调用 + 该调用产生的当批工具”；Snapshot 分为 RequestSnapshot 与 TurnSnapshot
- 并行工具批次先按源序串行完成 hook、权限、审批与参数校验，再并行执行已放行工具；hook 不得改写 tool-call ID
- Agent 支持独立 steering / follow-up 队列及 `all` / `one-at-a-time` 消费模式；活跃请求期间普通 prompt/continue 明确拒绝
- 全局 `tool_execution` 可强制批次串行；逐工具 `execution_mode="sequential"` 可在并行全局模式下收紧执行
- Assistant thinking/reasoning 内容可保留 provider signature/redacted payload；text/thinking/tool-call 均有完整 start/delta/end 流事件
- `AgentState` 公开 secret-free model 身份、thinking level、请求流状态、当前 partial message、执行中 tool-call ID 与最近 assistant error；Web `/api/state` 使用同一事实源
- ToolResult 可携带工具自身 usage 与 `added_tool_names`；usage 不并入主 LLM 上下文计费，added names 只标记 `Context.tools` 的 provider 加载点且不能由 after hook 伪造
- SQLite Session 使用 append-only parent-entry tree；命名 lane 持久化 active leaf，支持 branch、fork、append-only label fact 与重启恢复；`messages` 是 active lane 的兼容投影
- Session lane operation 使用 append-only intent/effect/finish records；`Memory.md` 更新以 immutable generation + 原子 metadata pointer 发布，旧 JSON Session save 使用可修复 torn tail 的 append-only journal
- Compaction 默认按完整 user→assistant/tool-result turn 切分；token 目标不拆最新 turn，压缩前后 token/window 可审计，旧摘要按 pi-compatible envelope 迭代折叠，瞬时摘要错误可按不可变输入重试
- Web 消息序列化会递归检测 `U+FFFD` 并附加 `content_warnings`，`/api/messages` 同时返回 Session 汇总；前端在对应消息/工具卡标记疑似编码损坏和 JSON 字段路径，检测过程只读且明确不可自动恢复
- Managed Coding Sandbox 使用顶层独立 `coding_sandbox` 包；主应用从 Session WorkspaceStore revision 物化不含存储 metadata 的逻辑树，再由 Agent 通过 provider-neutral 工具修改云端副本，固定验证通过后冻结并签名不可变制品
- Chat 输入区可显式启用 `Code`：请求开始前自动准备 Session Sandbox，并把本轮工具/权限收窄为 9 个隔离 `coding_*` 工具；模型结束后服务端独立重跑固定验证并自动冻结，右栏切到完整 Changes，等待用户批准，不会自动发布
- 本机 Publisher 在项目级跨进程锁内复核完整 baseline 和签名制品，以备份、原子替换、hash-chained journal、失败回滚和启动恢复发布；Sandbox 永不挂载真实工作区
- 每个 Session 最多一个活跃 Managed Sandbox operation；创建、验证、冻结、审批发布、取消和丢弃均由 Backend 单一转换表驱动，REST 返回版本化 `allowed_actions`/`allowed_transitions`，SQLite 完整旧记录 CAS 防止并发状态覆盖，并保存有界事件日志通过统一 WS 实时推送
- Validation 与 Freeze 共用 operation 互斥锁；冻结前重验 validation/config/workspace SHA，冻结后远端归档前后、下载归档和签名前再次校验。屏障前 stale 可重验，屏障后 `artifact_stale` 终态销毁，发布只读取已签名不可变制品
- Sandbox Modal 可恢复当前 Session 的最新状态、日志、Diff 与验证证据；刷新和后端重启都不会重放模型/命令，启动时未完成操作统一标记 `interrupted`，发布必须由用户重新勾选显式确认

## 数据与生命周期

默认开发数据根目录为 `.pi-agent-data/`：

| 数据 | 默认位置/生命周期 |
|---|---|
| 账号与登录 Session | `.pi-agent-data/auth.sqlite`；账号保留，登录 Session 在后端重启时撤销 |
| 账号工作区 | `.pi-agent-data/users/{user_id}/` |
| Session/消息/operation records/Skills/MCP/Provider/Sandbox metadata | 用户目录下 `workspace.sqlite`；Sandbox operation 与有界事件流同库持久化 |
| Session 文件 | 用户目录下 `uploads/{session_id}/` |
| Sandbox 本机状态 | 主应用 baseline 来自 `uploads/{session_id}` 的 WorkspaceStore revision；`coding-sandbox-staging/` 保存临时 materialization、snapshot、制品和隔离发布镜像，Workspace 事务 journal 位于 Session 隐藏目录并由 Store 恢复；`coding-sandbox-publisher/` 与 `coding-sandbox-projects/` 仅服务未配置 WorkspaceStore 的本地目录兼容组合 |
| 旧 Chunk Knowledge（退役兼容） | 若历史用户目录存在 `knowledge/knowledge.db` 与 `knowledge/libraries/`，产品启动不打开、不迁移也不删除；仅显式兼容测试可启用 |
| LLM Wiki | 开发启动器使用 `knowledge/wiki.db`、`knowledge/spaces/` 与 `knowledge/legacy/`；schema v7 Raw 原件/不可变 parse revisions、页面镜像、FTS5、图谱、Change Set 与 Conversation 均由新 Store 管理 |
| API Key | OS Keyring、显式 session-only memory 或显式 env；不写入 SQLite 明文 |
| Active request / pending approval / event subscribers | 当前后端进程内存；后端重启不恢复执行 |

## 2026-08-29 当前验证基线

当前代码按快速开发阶段的“最小可观察行为”边界完成阶段 6 发布验收：全量离线 Backend、
Frontend 与 Browser E2E 均实际复跑；真实 E2B 也重新执行 Managed Sandbox 写入、失败验证、
重验、冻结、审批和 WorkspaceStore 回写。历史 Parser、DDGS/GLM 证据继续保留：

| 验证 | 结果 | 备注 |
|---|---|---|
| Ruff 全量 | **PASS** | `ruff check .`；0 errors；包含 OCI Provider、Worker service 与 smoke CLI |
| strict Mypy 全量 | **PASS** | `mypy --strict src`：177 source files / 0 issues；覆盖 Wiki、独立 Workspace 包、Workspace 文档转换、Managed Sandbox 与产品组合边界 |
| Backend 全量离线（第二轮前基线） | **3143 passed, 7 skipped, 9 deselected** | 当时为 3159 collected；`pytest tests --tb=short -q`；1053.41s；coverage 81.91% |
| Backend 当前精简套件 | **2095 passed, 7 skipped, 9 deselected** | `pytest tests --tb=short -q --no-cov`；647.44s；默认排除真实外网、LLM 与 Docker marker |
| Frontend 当前精简套件 | **180/180 passed** | 26 files；删除重复 Provider API/Store/表单/选择器矩阵，保留 Provider 设置弹窗和跨层集成行为 |
| LLM Wiki 发布候选定向回归 | **Backend 67 passed, 1 skipped；Frontend 16/16；Browser 1/1** | 覆盖 Store/API/Summary/Fake Parser、五个 Wiki 前端视图与 source→approval→page→graph→conversation 浏览器主链路 |
| Workspace 阶段 1 定向回归 | **125 passed** | `VirtualFileStore` 兼容别名、双根初始化、并发幂等、旧路径/purpose 迁移、固定根删除保护、Checkpointer/Auth/重启；`-W error` 下 0 warning |
| Workspace 阶段 2 定向回归 | **116 passed** | 代码 `scripts/**` 映射、安全逻辑路径、revision 持久/冲突、Markdown CRUD、Agent 工具与 Web API；使用 `--no-cov` 定向运行 |
| 自动 Coding 编排定向回归 | **Backend 31 passed；Frontend 21/21；real E2B PASS** | 新增 2 项测试覆盖自动创建→验证→冻结待批准，以及验证失败不冻结；全仓 Ruff、strict Mypy 171 files、Frontend typecheck/lint/build 通过；真实 E2B 由新编排器完成创建、9 工具、重验、冻结、签名、模拟批准回写与销毁（25.328s） |
| LLM Wiki Parser Contract/Fake v1 | **29 passed** | 历史 PDF-only Contract v1 的 DTO/Protocol、来源/制品 SHA、路径/配额、确定性 tar、取消/超时/销毁和包依赖隔离；可复用但不代表双 Parser v2 行为 |
| LLM Wiki Parser Contract/Fake v2 与隔离 | **30 passed** | Contract v2 三模式、预检/路由/质量、逐页规范 Markdown、同源单次 fallback、attempt/artifact evidence、离线 Fake v2 生命周期/制品、AGPL capability 和主进程无具体 Parser runtime 依赖 |
| LLM Wiki Fake Router v2 专项 | **10 passed** | accurate/扫描/复杂直接 Docling、auto fast 通过、auto 单次 fallback、显式 fast 拒绝、Docling 终止、源路径事后修改仍固定原始 SHA、取消/销毁与内容不泄露 |
| LLM Wiki 全邻接 | **156 passed, 4 skipped** | `pytest tests -k wiki -q --no-cov`；20.62s；Wiki schema/store/files/legacy、Parser v1/v2/Fake v2、Source/HTML、Contract v1/v2 不可信 artifact、AGPL Worker/Source Offer、attempt/quality/route evidence、revision history/CAS/pointer repair、Worker 与 API；skip 为 Windows symlink capability |
| LLM Wiki OCI/合规专项 | **31 passed + real OCI PASS** | fast/accurate/auto/fallback、cancel/timeout/crash/restart、source/artifact 篡改、配额、官方 AGPL 文本 SHA、锁/基础镜像/Compose/上游源码、确定性 Source Offer/About API；最终镜像 `sha256:d1e517ba…0368b`，镜像内 bundle/notices、断网/只读/非 root/无 capability/资源上限与真实 digital/论文/复杂/OCR/fallback/取消恢复均通过；smoke mutable path 防误用回归已覆盖 |
| LLM Wiki v2 邻接回归 | **92 passed** | Contract v1/v2、Fake/OCI Provider、ingestion、HTML、不可信 artifact import、主应用依赖隔离与 AGPL Source Offer；`--no-cov` |
| LLM Wiki Contract v2 主链路新增回归 | **13 passed（纳入全量）** | 覆盖 auto fallback 原子发布、fast 质量拒绝、恶意/非规范 artifact、accurate 重解析历史、v1/v2 隔离、API parse mode/evidence 浏览、恢复模式保持与连续重解析排队 |
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
| Session tree 定向回归 | **69 passed** | immutable entry/lane、旧库迁移、branch/fork/label/active leaf、重启、Web API、revision sibling 与 trailing suffix |
| ToolResult metadata 定向回归 | **173 passed** | usage / added names、hook 边界、消息/事件、provider context、Snapshot、Session/SQLite、Web serializer 与旧数据默认值 |
| Agent 公开状态定向回归 | **137 passed** | Agent、Harness、stream、Provider runtime 与 Web state；系统 temp ACL 阻断项改用工作区 `basetemp` 后通过 |
| DDGS + GLM 真实 smoke | **3/3 passed** | 固定 secret-safe 脚本；DDGS 1 项 + GLM 2 项；24.63s；未输出凭证 |
| 真实测试门禁回归 | **3 skipped** | 手工选择 `-m integration` 但未设置 `PI_RUN_INTEGRATION=1`，确认不触网 |
| Frontend Vitest 全量 | **352/352 passed** | 30 files；旧 Chunk Knowledge UI/Store 测试随产品退役删除，新 Wiki API/Store/Views/Route/Knowledge Chat 回归已覆盖 |
| Frontend typecheck | **PASS** | `vue-tsc --noEmit` |
| Frontend ESLint | **PASS** | `eslint . --max-warnings=0` |
| Frontend production build | **PASS** | `vite build`；0 mixed dynamic/static import warning |
| 版本/许可证一致性回归 | **3 passed** | Python、两个 FastAPI、前端 package/lockfile 与根 LICENSE 元数据一致 |
| Python wheel 构建 | **PASS** | `pi_agent_core_py-0.0.28-py3-none-any.whl`；METADATA 与归档内 LICENSE 均已核验 |
| B7 定向回归 | **248/248 passed** | SQLite Store lifecycle/open failure |
| Keyring 定向回归 | **94/94 passed** | Runtime、launcher 与 restart 范围 |
| 真实 Windows Keyring 探针 | **write/read = true；cleanup = true** | 随机非用户值，执行后删除；同账号/同解释器复验与安全取证步骤已形成 Windows smoke 文档 |
| MCP/DDGS UTF-8 定向回归 | **34 passed, 1 deselected** | 含真实 Python 子进程中文 round-trip |
| Browser E2E | **19/19 passed** | Chromium；8 个规格、单 worker、`CI=1`；1.3m；覆盖基础聊天、Session 恢复、MCP、Approval、Compaction、Workspace 文档转换、Sandbox 与 LLM Wiki；0 retry / 0 flaky / 0 failure |
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

- 每账号单 harness、单 active request；不支持同账号多个 Session 并行生成
- 普通 Prompt/Regenerate active request 与 pending approval 不跨后端重启恢复；浏览器刷新只恢复仍在当前进程运行的请求。Checkpointer 可对已接受 intent 前滚；Managed Sandbox 只恢复持久状态/事件并把重启前未完成操作标为 `interrupted`，不重放模型、命令或发布
- Human Approval 只有 Approve once / Deny；没有永久授权
- Context Budget 是带安全余量的确定性近似，不是 Provider 官方 tokenizer
- Context compaction 由用户手动触发，默认摘要器为本地规则式；自动 Session Memory 与 `/checkpointer` 调用当前 Session LLM，但前者保留消息、后者成功后清空当前 lane
- Regenerate 仅支持最新 Assistant，不提供历史 revision 切换 UI
- Session tree 的 Core/Web API 已完成；当前聊天 UI 尚无可视化 branch/lane navigator
- MCP HTTP transport 仍是 placeholder；当前可用 transport 为 stdio
- `added_tool_names` 已保留到 provider 边界，但当前 OpenAI/Anthropic-compatible adapters 不实现原生 deferred tool loading，仍按完整 ToolRegistry 发送工具

### 文件与 LLM Wiki

- Session 文件统一到 `WorkspaceStore` 事实源，代码归一到逻辑 `scripts/**`，Sandbox 已事务发布回同一事实源；右侧成果面板展示代码、Markdown 和固定文档转换产物
- Session PDF/DOCX/XLSX 上传后进入 `documents/<id>/`；Agent/右栏读取生成的 `content.md`、CSV/schema 与 assets，二进制原件只提供元信息/下载且不可变
- PDF 扫描件由 Docling OCR preset 处理；MVP 不做通用图片语义理解或视觉模型问答
- Wiki 只对已批准页面使用 SQLite FTS5/BM25，不建立 Chunk、向量数据库或 embedding 主链路
- 新 Wiki 已独立接入 Web lifespan/API；真实用户旧 Chunk 库原样保留但产品不再打开。双 PDF Parser v2 与 OCI file-queue Provider 已接入；默认开发启动器未配置 Provider 时 PDF 保持 `uploaded`
- 旧记录中已经写入的 Unicode replacement character `U+FFFD` 仍无法从现有数据反推出原字符；当前会在读取时把它标记为“疑似编码损坏”并显示受影响字段，但不会猜测或写回所谓修复

### 工程债务

- 当前无 Git remote；tag/push 需要先决定版本并配置 remote
- 旧 `.pytest_cache` 仍受本机 ACL 限制，但 pytest 已固定使用可写的 `.pytest-cache-workspace` 与 `.pytest-tmp`，不再读写旧目录或关闭 cacheprovider
- Coding Sandbox P0 第 0–10 项和 Workspace 阶段 4A–6 已完成；显式状态机/CAS、双 TOCTOU、revision-bound baseline、事务发布、固定文档转换、右栏成果刷新与发布门禁均已固定

## 当前阻塞项

LLM Wiki 阶段 0–10、Session Workspace 阶段 1–7 与 Coding Agent Workspace 连续性阶段 1–4 已完成，没有功能阻塞。真实 OCI Worker 为 `runtime_ready=true`；release tag 仍为 `0.0.28`；若需发布后续提交或 push，仍需先决定新版本并配置 Git remote。

## 建议下一步

1. 实现 Coding Agent Workspace 连续性阶段 5：统一 ContextAssembler 与无聊天上下文续作验收。
2. 阶段 5 完成后再整理提交；若准备新发布，统一提升版本并创建下一 annotated tag。

未完成事项的唯一清单见 [`TODO.md`](TODO.md)。使用与架构说明见 [`README.md`](README.md)。
