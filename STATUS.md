# Project Status

> 当前事实快照，校准日期：**2026-08-26**。本页只描述当前代码基线；阶段性测试数字和历史决策保留在 `docs/validation/`、`CHANGELOG.md` 与归档计划中。

## 基线身份

| 项 | 当前事实 |
|---|---|
| 代码基线 | LLM Wiki 阶段 2 已提交为 `9030e45`；当前工作树正在实现阶段 3 Raw Ingestion |
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

LLM Wiki 产品合同、Parser Provider 与 Store 基础阶段已完成；阶段 3 Contract v1 Backend、双 Parser Contract v2、Raw parse revisions、离线 Fake v2、Contract v2 主应用编排、AGPL Worker 合规包、真实 Parser adapter 及持久 OCI Worker 的构建与实机 Gate 已完成。PDF 路线为 PyMuPDF4LLM fast + Docling accurate/auto fallback；当前待办仅是阶段 3 前端：

| 能力 | 状态 | 代表性基线 |
|---|---|---|
| Step 1–21 Core Runtime | ✅ 完成 | 归档见 `docs/archive/legacy-plans/PLAN_STEP_1_21.md` |
| Multi-provider Runtime / UI | ✅ 完成 | GLM、Qwen、Kimi；Anthropic-compatible 后端能力 |
| Knowledge PDF → Markdown → FTS5 → Citation | ✅ 完成 | P2-R4 final `7faf635`；P2-R5 final `6527be5` |
| 登录与账号工作区隔离 | ✅ 完成 | `b529bbc` |
| Session Workspace、`AGENT.md`、`Memory.md`、`/checkpointer` | ✅ 完成 | `WorkspaceStore` 为唯一规范事实源；新旧 Session 幂等初始化两个固定根文件，保留旧正文/file id；设计见 `docs/design/workspace-sandbox-integration.md` |
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
| 全仓 Ruff / strict Mypy / CI 收敛 | ✅ 完成 | Ruff 0；Mypy 114 files / 0 issues；Python CI timeout 30 分钟 |
| Release metadata 与 MIT License | ✅ 完成 | `8a6ff2e`；Python/API/前端统一 `0.0.28`，wheel 携带根许可证 |
| 当前发布前浏览器/联网门禁 | ✅ 完成 | `51ce3c7`；Playwright 45/45，DDGS + GLM 真实 smoke 3/3 |
| Managed Coding Sandbox P0 0–10 | ✅ 完成 | 独立包、快照、E2B、代码工具、固定验证、签名制品、本机事务 Publisher、Web 生命周期/状态恢复/审批发布 UI，以及真实 E2B、完整 CI、攻击矩阵和 Browser E2E 验收 |
| Backend warning / pytest 状态目录 / ToolResult UTF-8 E2E | ✅ 完成 | Backend `-W error` 0 warning；cache/temp 固定到工作区；Playwright 48/48 |
| Frontend warning 收敛 | ✅ 完成 | Modal/Teleport attrs、Vite mixed import 与 Playwright color env 三类提示归零；Vitest 404/404；Playwright 48/48 |
| 历史消息 U+FFFD 完整性标记 | ✅ 完成 | 读取时递归检测并返回计数/RFC 6901 路径，不改写 SQLite、不伪造恢复；消息与工具卡可见，刷新保持；Playwright 49/49 |
| LLM Wiki 产品合同与 Parser Provider Gate | ✅ Contract/Fake/OCI Provider v2 完成并实机验证 | 新 Wiki 保持无 Chunk-RAG；Marker 路线停止；Contract v2 固定 PyMuPDF4LLM fast、Docling accurate/auto fallback、逐页制品、同源 attempt 证据、hash-pinned config、原始 PDF 唯一事实源与 AGPL-3.0 路径；主应用通过无 Parser 依赖的 file queue Provider 连接外部 Worker |
| LLM Wiki AGPL Worker 合规包 | ✅ `runtime_ready=true` | 独立 `workers/wiki_parser_worker`、AGPL-3.0-only、35 文件精确/确定性 Source Offer、SPDX 2.3 SBOM、88 包 uv lock、hash-required Linux resolution、三项 Artifex 源码物化器、digest-pinned OCI/断网 Compose、持久 supervisor/child 与 wheel verifier；Docker 29.7.2/Linux-amd64 下镜像 bundle/notices、隔离、真实 fast/accurate/复杂/OCR/fallback 与取消恢复均通过 |
| LLM WikiStore、目录与旧库退役 Gate | ✅ schema v2 | 页面中心型 schema v2、Space CRUD/CAS、不可变 ParseAttempt/ParseRevision、revision-scoped Artifact、selected version CAS/pointer repair、Raw/Page 原子路径和显式只读 legacy backup；旧 Wiki flat schema v1 固定要求显式重建，不自动迁移 |
| LLM Wiki Raw Ingestion Backend | 🚧 阶段 3 进行中 | Contract v1 Source/Artifact/Job、不可变 PDF/HTML、零网络 HTML、Fake PDF、不可信 tar、lifespan/API/Worker、中断恢复、双 Parser Contract v2、Raw parse revisions、离线 Fake v2、Contract v2 不可信 artifact 导入/原子编排、真实 adapter、OCI transport/supervisor 及实机 Gate 已完成；前端待完成 |

## 当前产品能力

- 未登录先进入 Username/Password 页面；空认证库幂等创建本地初始账号 `admin / 123456`
- 每个账号拥有独立的 Session、消息、文件、Skills、MCP、Knowledge、Provider/Credential 配置
- Session 路由为 `/chat/{session_id}`；刷新恢复准确 Session、历史、文件树、`AGENT.md`、`Memory.md` 及当前请求状态
- 每个 Session 由唯一 `WorkspaceStore` 初始化独立文件夹和唯一根 `AGENT.md`、`Memory.md`；启动时幂等补齐旧 Session，保留已有正文/file id，两个根文件不可删除；`VirtualFileStore` 仅为同一实现的兼容别名
- Agent 写入或用户上传的代码按扩展名自动进入逻辑 `scripts/**`；普通 Markdown 支持安全路径创建、编辑、移动/重命名和删除，`AGENT.md`/`Memory.md` 继续使用专用权限
- Workspace revision 以隐藏状态持久化；上传、创建、更新、移动和删除可同时校验 revision 与逐文件 SHA，过期客户端收到 409 而不会静默覆盖
- 桌面右栏是 Agent 成果交付面：成功生成 `.md`、`.py` 等文件后立即刷新、选中并展示，整页刷新后仍从持久 ToolResult 恢复；窄屏使用带新成果提示的 drawer
- `/checkpointer` 使用当前 Session Provider 把对话累计总结到 `Memory.md`；接受时持久化 source leaf/hash，文件发布后原子清空原 lane，进程退出可幂等前滚
- 支持 Prompt、Stop、Regenerate 最新 Assistant、Markdown Export、实时事件、请求恢复和精确 ToolCall 审批
- Provider Profile、Session Model Binding、Context Window 与 Max Output Tokens 持久化；UI 管理 GLM/Qwen/Kimi
- 持久化凭证默认进入 OS Keyring；开发启动器在监听端口前执行 write/read/delete 探针
- MCP 支持 stdio tools/prompts；内置 DDGS 固定存在、不可删除，可修改返回数、地区、安全搜索、时间范围等参数
- Knowledge Manager 支持 Library、PDF 上传、后台解析/索引、Session binding、FTS5 搜索和 Agent citation
- LLM Wiki 阶段 0–2 与阶段 3 Backend 已接入：`create_app(wiki_root=...)` 可在不启动旧 Knowledge 的情况下运行独立 Store/API/Worker；HTML 可真实离线解析，PDF 在未配置 Provider 时保持 `uploaded`，旧 Knowledge/RAG 仍并存且二者不共享表或业务数据
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
- Managed Coding Sandbox 使用顶层独立 `coding_sandbox` 包；Agent 只通过 provider-neutral 工具修改云端副本，固定验证通过后冻结并签名不可变制品
- 本机 Publisher 在项目级跨进程锁内复核完整 baseline 和签名制品，以备份、原子替换、hash-chained journal、失败回滚和启动恢复发布；Sandbox 永不挂载真实工作区
- 每个 Session 最多一个活跃 Managed Sandbox operation；创建、验证、冻结、审批发布、取消和丢弃均由持久状态机驱动，SQLite 保存有界事件日志并通过统一 WS 实时推送
- Sandbox Modal 可恢复当前 Session 的最新状态、日志、Diff 与验证证据；刷新和后端重启都不会重放模型/命令，启动时未完成操作统一标记 `interrupted`，发布必须由用户重新勾选显式确认

## 数据与生命周期

默认开发数据根目录为 `.pi-agent-data/`：

| 数据 | 默认位置/生命周期 |
|---|---|
| 账号与登录 Session | `.pi-agent-data/auth.sqlite`；账号保留，登录 Session 在后端重启时撤销 |
| 账号工作区 | `.pi-agent-data/users/{user_id}/` |
| Session/消息/operation records/Skills/MCP/Provider/Sandbox metadata | 用户目录下 `workspace.sqlite`；Sandbox operation 与有界事件流同库持久化 |
| Session 文件 | 用户目录下 `uploads/{session_id}/` |
| Sandbox 本机发布区 | 用户目录下 `coding-sandbox-projects/`、`coding-sandbox-publisher/` 与 `coding-sandbox-staging/`；不暴露给云 Sandbox |
| 旧 Knowledge（当前运行时） | 用户目录下 `knowledge/knowledge.db` 与 `knowledge/libraries/` |
| LLM Wiki（阶段 3 进行中） | 开发启动器以 preserve 模式使用 `knowledge/wiki.db`、`knowledge/spaces/` 与 `knowledge/legacy/`；schema v2 Raw 原件位于来源目录，制品位于不可变 `parses/{parse_revision_id}`，`selected.json` 可由 DB 重建；旧 Wiki schema v1 返回 `schema_rebuild_required` |
| API Key | OS Keyring、显式 session-only memory 或显式 env；不写入 SQLite 明文 |
| Active request / pending approval / event subscribers | 当前后端进程内存；后端重启不恢复执行 |

## 2026-08-25 当前验证基线

Backend 全量数字基于当前含 Coding Sandbox P0 第 0–10 项的工作区实际复跑；
版本/许可证提交 `8a6ff2e` 另行通过 wheel 元数据验证；既有 `51ce3c7` 等价内容
通过完整 Playwright 与 DDGS/GLM 网络 smoke，本次另通过真实 E2B + Publisher 安全 smoke：

| 验证 | 结果 | 备注 |
|---|---|---|
| Ruff 全量 | **PASS** | `ruff check .`；0 errors；包含 OCI Provider、Worker service 与 smoke CLI |
| strict Mypy 全量 | **PASS** | 主项目 `src` + OCI smoke CLI：163 files / 0 issues；Worker source/scripts/tests：21 files / 0 issues；覆盖 `wiki_parser` Contract/Fake/OCI v1/v2、新 Wiki Raw Ingestion、Source Offer、Parser adapters、构建脚本与 supervisor |
| Backend 全量离线 | **4024 passed, 9 skipped, 15 deselected** | `pytest tests --tb=short -q --no-cov`；550.08s；包含双 Parser Contract/Fake v2、Wiki schema v2、Contract v2 artifact 导入/编排、AGPL Worker/Source Offer、parse revision/CAS/pointer repair 与阶段 3 Backend/API/恢复回归 |
| Workspace 阶段 1 定向回归 | **125 passed** | `VirtualFileStore` 兼容别名、双根初始化、并发幂等、旧路径/purpose 迁移、固定根删除保护、Checkpointer/Auth/重启；`-W error` 下 0 warning |
| Workspace 阶段 2 定向回归 | **116 passed** | 代码 `scripts/**` 映射、安全逻辑路径、revision 持久/冲突、Markdown CRUD、Agent 工具与 Web API；使用 `--no-cov` 定向运行 |
| LLM Wiki Parser Contract/Fake v1 | **29 passed** | 历史 PDF-only Contract v1 的 DTO/Protocol、来源/制品 SHA、路径/配额、确定性 tar、取消/超时/销毁和包依赖隔离；可复用但不代表双 Parser v2 行为 |
| LLM Wiki Parser Contract/Fake v2 与隔离 | **30 passed** | Contract v2 三模式、预检/路由/质量、逐页规范 Markdown、同源单次 fallback、attempt/artifact evidence、离线 Fake v2 生命周期/制品、AGPL capability 和主进程无具体 Parser runtime 依赖 |
| LLM Wiki Fake Router v2 专项 | **10 passed** | accurate/扫描/复杂直接 Docling、auto fast 通过、auto 单次 fallback、显式 fast 拒绝、Docling 终止、源路径事后修改仍固定原始 SHA、取消/销毁与内容不泄露 |
| LLM Wiki 全邻接 | **156 passed, 4 skipped** | `pytest tests -k wiki -q --no-cov`；20.62s；Wiki schema/store/files/legacy、Parser v1/v2/Fake v2、Source/HTML、Contract v1/v2 不可信 artifact、AGPL Worker/Source Offer、attempt/quality/route evidence、revision history/CAS/pointer repair、Worker 与 API；skip 为 Windows symlink capability |
| LLM Wiki OCI/合规专项 | **31 passed + real OCI PASS** | fast/accurate/auto/fallback、cancel/timeout/crash/restart、source/artifact 篡改、配额、官方 AGPL 文本 SHA、锁/基础镜像/Compose/上游源码、确定性 Source Offer/About API；最终镜像 `sha256:d1e517ba…0368b`，镜像内 bundle/notices、断网/只读/非 root/无 capability/资源上限与真实 digital/论文/复杂/OCR/fallback/取消恢复均通过；smoke mutable path 防误用回归已覆盖 |
| LLM Wiki v2 邻接回归 | **92 passed** | Contract v1/v2、Fake/OCI Provider、ingestion、HTML、不可信 artifact import、主应用依赖隔离与 AGPL Source Offer；`--no-cov` |
| LLM Wiki Source Offer Browser E2E | **1 passed** | 登录后从侧栏打开 About，核验 MIT/AGPL 分离、无担保和 runtime-ready 提示，读取 hash manifest 并实际下载命名正确、具有 gzip 魔数的 Corresponding Source；运行态 exchange 固定在 Source Offer 树外；0 retry |
| LLM Wiki Contract v2 主链路新增回归 | **13 passed（纳入全量）** | 覆盖 auto fallback 原子发布、fast 质量拒绝、恶意/非规范 artifact、accurate 重解析历史、v1/v2 隔离、API parse mode/evidence 浏览、恢复模式保持与连续重解析排队 |
| LLM Wiki Raw revision 专项 | **5 passed** | 重解析历史保留、selected version 防 ABA、失败保留 selected 并记录 attempt、启动修复 pointer、跨 Source FK 隔离 |
| LLM WikiStore/Files/Legacy | **52 passed, 4 skipped** | schema/version/capability/integrity、Space CRUD/双连接 CAS、路径/原子写入/镜像恢复、legacy 一致备份/故障顺序和旧 RAG import 隔离；skip 为 Windows symlink capability |
| LLM Wiki 阶段 1–3 定向回归 | **104 passed, 4 skipped** | Provider contract、Store/Files/Legacy、Source/Artifact/Job、HTML、Fake PDF、安全 tar 导入、API/lifespan、并发 claim 与崩溃后新 attempt；skip 为 Windows symlink capability |
| 新旧 Knowledge 存储邻接 | **149 passed, 5 skipped** | 新 Wiki 52 项及旧 Knowledge Store/File、SQLite open-failure 清理回归；确认新实现不破坏旧运行时 |
| Coding Sandbox 安全矩阵 | **151 passed, 2 skipped** | 离线 Fake/E2B 契约、路径/命令/网络/凭证攻击、故障注入、validation/artifact 篡改、Publisher 冲突/回滚/崩溃恢复，以及退役 Provider 空字段兼容迁移；Windows capability skip |
| Sandbox Web 生命周期/API | **7 passed** | SQLite operation/event 恢复、启动 `interrupted` 收敛、snapshot seed、验证失败不得冻结/发布且真实工作区字节级不变、取消，以及 disabled/missing/latest API 边界 |
| 真实 E2B + Publisher 安全 smoke | **PASS** | 9 个代码工具、故意验证失败与冻结拒绝、恢复重验、制品冻结/签名、本机冲突且工作区字节级不变、commit、幂等 retry、Sandbox destroy；20.577s；未输出凭证 |
| Complete-turn Compaction 定向回归 | **50 passed** | 完整 turn/token target、超预算最新 turn、token/window、previous summary、retry lifecycle、失败不改源消息、Web API 与 durable-operation 邻接回归 |
| Durable recovery 定向回归 | **PASS** | operation intent/effect/finish、同进程无模型重试、启动前滚、source-leaf conflict 保留消息、文件 pointer rollback、JSON torn-tail / legacy migration |
| Session tree 定向回归 | **69 passed** | immutable entry/lane、旧库迁移、branch/fork/label/active leaf、重启、Web API、revision sibling 与 trailing suffix |
| ToolResult metadata 定向回归 | **173 passed** | usage / added names、hook 边界、消息/事件、provider context、Snapshot、Session/SQLite、Web serializer 与旧数据默认值 |
| Agent 公开状态定向回归 | **137 passed** | Agent、Harness、stream、Provider runtime 与 Web state；系统 temp ACL 阻断项改用工作区 `basetemp` 后通过 |
| DDGS + GLM 真实 smoke | **3/3 passed** | 固定 secret-safe 脚本；DDGS 1 项 + GLM 2 项；24.63s；未输出凭证 |
| 真实测试门禁回归 | **3 skipped** | 手工选择 `-m integration` 但未设置 `PI_RUN_INTEGRATION=1`，确认不触网 |
| Frontend Vitest 全量 | **414/414 passed** | 32 files；包含 Workspace 三栏/drawer、Agent 成果聚焦、Markdown 预览编辑及 About/Source Offer 回归；0 Vue attribute warning |
| Frontend typecheck | **PASS** | `vue-tsc --noEmit` |
| Frontend ESLint | **PASS** | `eslint . --max-warnings=0` |
| Frontend production build | **PASS** | `vite build`；0 mixed dynamic/static import warning |
| 版本/许可证一致性回归 | **3 passed** | Python、两个 FastAPI、前端 package/lockfile 与根 LICENSE 元数据一致 |
| Python wheel 构建 | **PASS** | `pi_agent_core_py-0.0.28-py3-none-any.whl`；METADATA 与归档内 LICENSE 均已核验 |
| B7 定向回归 | **248/248 passed** | SQLite Store lifecycle/open failure |
| Keyring 定向回归 | **94/94 passed** | Runtime、launcher 与 restart 范围 |
| 真实 Windows Keyring 探针 | **write/read = true；cleanup = true** | 随机非用户值，执行后删除；同账号/同解释器复验与安全取证步骤已形成 Windows smoke 文档 |
| MCP/DDGS UTF-8 定向回归 | **34 passed, 1 deselected** | 含真实 Python 子进程中文 round-trip |
| Browser E2E | **52/52 passed** | Chromium；单 worker；`CI=1`；完整套件 2.5m；覆盖 Agent Python 成果自动展示/刷新恢复、Markdown 创建编辑、代码上传与窄屏 drawer；0 retry / 0 failure；production build 已恢复 |
| Context Compaction Browser E2E | **1/1 passed** | Chromium；独立端口 8013；沙箱外真实启动浏览器；production build 由 posttest 恢复 |

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
- Context compaction 由用户手动触发，默认摘要器为本地规则式；`/checkpointer` 才调用当前 LLM
- Regenerate 仅支持最新 Assistant，不提供历史 revision 切换 UI
- Session tree 的 Core/Web API 已完成；当前聊天 UI 尚无可视化 branch/lane navigator
- MCP HTTP transport 仍是 placeholder；当前可用 transport 为 stdio
- `added_tool_names` 已保留到 provider 边界，但当前 OpenAI/Anthropic-compatible adapters 不实现原生 deferred tool loading，仍按完整 ToolRegistry 发送工具

### 文件与 Knowledge

- Session 文件已经统一到 `WorkspaceStore` 事实源，代码也已归一到逻辑 `scripts/**`，右侧成果面板已接入；物理布局仍沿用 `uploads/{session_id}`，Sandbox 发布回该事实源属于后续阶段
- Session Folder 的 `view_file` 只对文本/Markdown/HTML/CSV/Parquet提供正文或结构化预览；Session PDF 只返回元信息
- Knowledge 子系统可解析文本型 PDF；扫描件进入 `needs_ocr`，当前无 OCR、图片理解或视觉模型
- Knowledge 检索使用 SQLite FTS5/BM25，不使用向量数据库或 embedding
- 新 Wiki 已以 `preserve` 模式接入独立 Web lifespan/API，真实用户旧库仍原样保留；双 PDF Parser v2 编排和 OCI file-queue Provider 已接入，但默认开发启动器尚未配置 Worker，未配置时 PDF 保持 `uploaded`，最终切换仍须显式 `retire`
- 旧记录中已经写入的 Unicode replacement character `U+FFFD` 仍无法从现有数据反推出原字符；当前会在读取时把它标记为“疑似编码损坏”并显示受影响字段，但不会猜测或写回所谓修复

### 工程债务

- 当前无 Git remote；tag/push 需要先决定版本并配置 remote
- 旧 `.pytest_cache` 仍受本机 ACL 限制，但 pytest 已固定使用可写的 `.pytest-cache-workspace` 与 `.pytest-tmp`，不再读写旧目录或关闭 cacheprovider
- Coding Sandbox P0 第 0–10 项已完成；真实 E2B、完整 CI、安全攻击矩阵、故障注入、Browser E2E 与失败/冲突工作区字节级不变均已验收

## 当前阻塞项

LLM Wiki Marker 接入已取消。用户已确认保持无 Chunk-RAG，并为 PyMuPDF4LLM 选择 AGPL-3.0 路径；Contract v2、Raw parse revisions、Fake Router v2、主应用不可信 artifact 编排、AGPL Source Offer/SBOM、真实 Parser adapters、完整传递 lock、digest-pinned OCI 与持久 file-queue supervisor/child 均已完成。2026-08-26 已从实际镜像复核 bundle/notices、运行隔离并完成代表性 PDF 与取消恢复 smoke，Worker 现为 `runtime_ready=true`。当前 LLM Wiki 阻塞仅剩阶段 3 前端；Coding Sandbox Modal/Local Docker 仍按既有决定暂缓。正式 release 仍需决定 tag 名，并在需要 push 时配置 Git remote。

## 建议下一步

1. 补阶段 3 最小前端：Space、Source、解析状态与 Raw artifact 浏览；不得引入 Chunk 检索 UI。
2. 完成阶段 3 前端/Backend/E2E 验收后，再进入 Wiki 页面与 Change Set；完整 Pages/Graph/Changes/Conversations 仍按阶段 4–7 推进。

未完成事项的唯一清单见 [`TODO.md`](TODO.md)。使用与架构说明见 [`README.md`](README.md)。
