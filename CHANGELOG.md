# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> 每个版本的详细验证报告见 [docs/validation/](docs/validation/)；
> release notes 见 [docs/releases/](docs/releases/)。

## [Unreleased]

### Automatic per-turn Session Memory（2026-08-29）

- 普通 Session Prompt 与 Regenerate 成功提交后自动生成有界 turn evidence，并通过每 Session 串行、append-only `auto_memory` operation 使用当前 Provider 累计更新 `Memory.md`；模型调用不启用 Tools、Skills 或 MCP，聊天消息不会被清空
- 自动记忆失败不改变主回答成功状态；evidence 保持可恢复，下一轮 preflight 先重试，仍不可用时把待处理 evidence 作为不可信历史上下文注入。canonical 最新完整 turn 可重建消息提交后、intent 落库前的进程退出窗口
- Coding turn 在签名 Artifact 待审批期间延迟更新，避免 Workspace revision 变化使 Validation→Freeze 基线失效；Sandbox 进入发布/拒绝/取消/失败/中断终态后自动续跑。Knowledge Conversation 明确跳过
- 产品启动器默认启用，低层 `create_app` 保留显式 opt-in；Prompt/request 与 `/api/state` 公开 secret-free continuity/recovery 状态并广播 `workspace_changed`
- 遵守快速开发预算：只修改 1 个既有测试文件、新增 2 项，验证成功更新且保留消息，以及 Provider 失败后下一轮恢复；最小相关 Backend 11 passed，Ruff、strict Mypy、Frontend typecheck/lint PASS

### Coding Agent Workspace module extraction（2026-08-29）

- 新增顶层 `agent_workspace` 包，迁移 WorkspaceStore、事务/恢复、固定文档转换和 provider-neutral Checkpointer 领域逻辑；持久格式与公开 Web 行为保持不变
- WorkspaceStore 不再依赖 FastAPI `UploadFile`，改用最小 async upload Protocol；新包独立 import 不加载 `pi_agent_core_py`、FastAPI、具体 Provider 或 Sandbox
- 新增 `coding_agent_app` 产品适配层承载 WorkspaceStore 与 `coding_sandbox` 的 baseline/publisher 桥接；旧 Web 模块暂时提供兼容 re-export，Core 内 Checkpointer 只负责消息序列化和 ModelClient stream 翻译
- 重构阶段未新增或修改测试；现有 Workspace/文件工具/Checkpointer/文档转换/Sandbox 定向 133 passed，Ruff PASS，strict Mypy 177 source files / 0 issues；构建 wheel 已包含 `agent_workspace` 与 `coding_agent_app`

### Automated Coding request orchestration（2026-08-29）

- Chat 新增显式 `Code` 模式；Backend 在 Agent turn 前自动创建/复用并等待 Session Sandbox，临时把工具与权限收窄到 9 个隔离 `coding_*` 工具，不再要求用户先点 Start 或逐工具批准
- Agent 完成后 Backend 独立重跑固定 validation plan；通过后自动执行 Freeze/签名并严格停在 `awaiting_approval`，最终 Workspace 发布仍要求用户审阅完整 Diff、勾选确认
- 验证失败保持 `validation_failed` 且不冻结/写回；Stop 覆盖创建、验证和冻结阶段。Coding 指令计入 Context Budget，右栏自动切换 Changes 并在桌面/窄屏显示审批提醒
- 遵守快速开发测试预算：只修改 1 个既有测试文件、新增 2 项，验证自动创建→验证→待批准主路径和验证失败绝不冻结；全仓 Ruff、strict Mypy 171 files、Frontend typecheck/lint/build 通过，定向 Backend 31 passed、Vitest 21/21 passed；真实 E2B 由新编排器完成创建、重验、冻结待批准、模拟批准回写和销毁（25.328s）

### Session Workspace stage 6 release acceptance（2026-08-29）

- Workspace Browser E2E 新增 XLSX 上传主路径：固定转换完成后右栏自动选中 `content.md`，展示 workbook 摘要并明确只读；修复文件树模板编译错误、生成文档只读提示不可达和 `Memory.md` 删除动作回归
- 真实 E2B smoke 改走完整 Managed Sandbox 状态机和 `WorkspaceStore` adapter：覆盖 9 个代码工具、故意验证失败、重验、冻结签名、审批门禁、单 revision 回写与 Sandbox 销毁，不再只发布到普通本机目录
- 修复 Windows Publisher 深层路径超过传统 MAX_PATH：Workspace adapter 使用短 run key，并把事务 state 放在 staging 根下的短兄弟目录；真实 E2B 首次暴露的 `winerror=206` 已回归通过
- 发布门禁：Ruff PASS；strict Mypy 170 files / 0 issues；Backend `2095 passed, 7 skipped, 9 deselected`；Frontend `180/180`、typecheck/lint/build PASS；Playwright `19/19`、0 retry/flaky；真实 E2B smoke PASS（27.171s）

### Session Workspace fixed document conversion（2026-08-29）

- `.pdf/.docx/.xlsx` 上传归档为 `documents/<document-id>/original.*` 不可变原件；固定转换器从 revision-bound Workspace 快照生成只读 Markdown、CSV/schema、图片和可审计 manifest，不复用 LLM Wiki 的 Provider、Worker、DB 或 Raw 制品
- PDF 使用 `pypdf` 分页提取并标记 `needs_ocr`，DOCX 使用 `python-docx` 转换标题/段落/列表/表格/链接和图片，XLSX 使用 `openpyxl` 输出 workbook 摘要及逐 Sheet 制品，公式只保存为惰性文本
- 生成物复用 WorkspaceStore durable transaction，在目标锁内重验 revision/tree/content 后一次提升 revision；source/converter/config 和输出 SHA 一致时幂等复用，失败不发布半成品且保留已有成功版本
- 上传响应和 retry API 返回转换状态；前端刷新右侧 Workspace 并优先展示/附加 `content.md`。新增 1 个测试文件/2 项测试，邻接 Backend 118 passed，Ruff/strict Mypy/Frontend typecheck/lint 通过，三格式真实 smoke 3/3

### Coding Sandbox WorkspaceStore transactional publish（2026-08-28）

- 新增 provider-neutral Artifact Publisher 与主应用 Workspace adapter：签名 Artifact 先在隔离镜像中复用既有本机事务 Publisher 完整复验，再把最终允许文件交给 `WorkspaceStore`，真实 Workspace 不进入 Sandbox
- `WorkspaceStore` 新增 journaled multi-file publish：Session 锁内核验 baseline revision/tree/content，固定白名单为 `scripts/**` 与普通 UTF-8 Markdown，保护固定根、系统状态和 `documents/**`；payload staging、immutable generation、metadata pointer、删除 tombstone、单次 revision、rollback 与启动恢复形成完整事务边界
- operation 记录目标 `published_workspace_revision`；发布事件携带 revision/changed/deleted paths 并广播 `workspace_changed`，右侧 Workspace 面板自动刷新和聚焦最新成果
- 未新增测试文件，在 1 个既有文件新增 2 项验证批量成功与 stale baseline 零写入；Backend 86 passed / 2 capability skipped，Ruff、strict Mypy、Frontend typecheck/lint 与相关 Vitest 6/6 均通过

### Coding Sandbox WorkspaceStore baseline（2026-08-28）

- `WorkspaceStore` 新增 revision-bound logical-tree materialization：在 Session mutation lock 内逐文件复核 containment、regular file、metadata size/SHA 和打开前后 stat，只导出逻辑路径与内容，不泄露 file id、metadata、隐藏 revision state 或物理路径
- 新增 Web 组合层 baseline adapter，向物化树注入受保护 validation config 后复用既有确定性 `ProjectSnapshot`；Managed operation 持久化源 Workspace revision/tree SHA，源 Workspace 后续变化不改写 baseline
- 主应用启用 WorkspaceStore 时不再使用 `coding-sandbox-projects` 作为输入事实源；阶段 4C Publisher 未接入前，Workspace 来源 operation 的 `publish_available=false`，Backend/API/UI 均隐藏并拒绝 publish，但运行、验证、冻结和审阅保持可用
- 未新增测试文件，修改 1 个既有 lifecycle 测试覆盖 Workspace baseline、后续 revision 隔离与发布暂停；Backend 73 passed / 1 capability skipped，Ruff、strict Mypy、Frontend typecheck/lint 与 CodingSandbox Vitest 均通过

### Coding Sandbox explicit state machine and TOCTOU barrier（2026-08-28）

- 以单一 Backend 转换表定义 Managed Sandbox 状态、动作和直接后继状态；REST 返回版本化 `allowed_actions` / `allowed_transitions`，前端不再独立推断操作权限
- 生命周期持久化从 blind update 改为完整旧记录 compare-and-swap；并发 validate/freeze/cancel/discard 只有一个转换可提交，旧请求固定返回 `operation_conflict`
- 明确 Validation→Freeze 屏障：屏障前 `validation_stale` 回到可重验状态，屏障后远端归档或最终指纹变化保留 `artifact_stale` 并终态销毁，不在 fail-closed operation 内重试
- 修改 1 个既有测试验证公开状态动作和 stale CAS；Backend 定向 9 passed，Ruff、strict Mypy、Frontend typecheck/lint 与 CodingSandbox Vitest 均通过

### Complete-turn compaction semantics（2026-08-20）

- `CompactionConfig` 默认边界从 message 改为完整 turn；固定 turn 数与 `keep_recent_tokens` 均只在 user 边界切分，最新 turn 即使超目标也不会拆散 tool-call/tool-result
- compaction 与 preflight 共用 `mixed-char-v1` estimator；Core/Web 返回压缩前后 message/input/projected tokens、context window、output reserve 与比例，Web 同时返回 canonical `budget_before` / `budget`
- SummaryMessage 以 pi-compatible `<summary>` envelope 注入 Provider；连续 compaction 通过 `previous_summary` 折叠旧摘要，不重复卷入展示前缀
- 新增显式 `CompactionRetryPolicy` 与 retry lifecycle callback；仅重试 timeout/connection/rate-limit/stream 瞬时错误，每次使用同一 prepared input 的新副本，失败或耗尽不改 Agent/Session 源消息
- 验证：完整后端 3698 passed、6 skipped、12 deselected、coverage 83.76%；Ruff 0、strict Mypy 114 files / 0 issues；前端 399/399、lint/typecheck/build 通过；Context Compaction Playwright 1/1 通过

### Durable operation / recovery（2026-08-20）

- SQLite Session 新增 lane-scoped durable operation identity 与 append-only `operation_started` / `effect_committed` / `operation_finished` records；相同 intent 重试复用，lane reset 与 completed 在同一事务提交
- `/checkpointer` 在外部 effect 前固定 immutable source leaf/hash；Memory 已发布但 SQLite 收尾失败时不再反向覆盖，重试或启动 recovery 无需再次调用 LLM，source leaf 变化则保留消息并终止为 conflict
- `VirtualFileStore` 文本更新改为 immutable content generation + 原子 metadata pointer；启动修复旧协议 backup/hash mismatch 并清理孤儿 generation
- `JsonFileSessionStore` 从整文件覆盖改为 append-only snapshot journal；旧单对象 JSON 首次写入一次性迁移，malformed final record 可作为 torn tail 截断恢复
- 完整门禁中修复 Ingestion Worker claim 后、内存 active job 更新前的 idle 误判竞态；新判断以单条 SQLite 快照同时观察 uploaded document 与 durable running job
- 验证：完整后端 3692 passed、6 skipped、12 deselected、coverage 83.70%；Ruff 0、strict Mypy 114 files / 0 issues；前端 399/399，lint/typecheck/build 全部通过

### Append-only Session tree 与 lanes（2026-08-20）

- SQLite Session 从单一线性历史迁移为 immutable parent-entry tree；每个命名 lane 持久化 active leaf，旧库按 `messages.idx` 幂等回填 `main` lane
- `messages` 保留为 active lane 兼容投影；branch、fork、active-lane 切换、label fact 与旧消息 ID/revision 在单 SQLite 事务中保持一致
- Regenerate 不再改写 tree entry：新回答创建 sibling branch；若最新 Assistant 后仍有 ToolResult，则复制 trailing suffix 到新分支
- 新增 Core 与 Web tree API、前端类型/客户端、设计文档及 16 项后端 + 5 项前端契约测试；完整后端 3681 passed / 83.84% coverage，前端 399/399，Ruff / strict Mypy / typecheck / ESLint / production build 全部通过

### ToolResult usage 与 deferred-tool metadata（2026-08-20）

- `ToolResult`、`ToolResultMessage` 与 `LLMToolResultMessage` 新增可选工具自身 usage 和 `added_tool_names`；usage 明确不并入主 LLM 上下文计费
- 元数据贯穿 tool execution update/end、message start/end、下一轮 provider context、Request/Turn Snapshot、SessionMemory、SQLite 与 Web JSON；历史数据缺字段时安全回退为 `None` / `[]`
- `added_tool_names` 仅标记已在 `Context.tools` 中的工具从该结果起可用，不触发 ToolRegistry 变更；after hook 可覆盖 usage，但不能伪造或删除加载点
- 新增 3 项端到端契约测试并扩展 SQLite 回归；定向 173 passed，完整后端 3665 passed / 83.78% coverage，前端 394/394，Ruff / strict Mypy / typecheck / ESLint / production build 全部通过

### Agent public runtime state（2026-08-19）

- `AgentState` 新增 secret-free `AgentModelState(provider/api/id)`、完整 `ThinkingLevel`、`is_streaming`、`streaming_message`、只读语义的 `pending_tool_calls` 与 `error_message`
- message start/update/end 与 tool execution start/end 直接驱动公开瞬态状态；request 完成、Agent 异常和 reset 统一清理，model projection 随 request-scoped client 切换
- `/api/state` 与前端 `AgentStateSummary` 暴露同一契约，并修正 queue size 读取不存在 `state.queue` 的旧 fallback
- 新增核心生命周期与 Web JSON 回归；完整后端 3662 passed、83.78% coverage，前端 394/394，Ruff / strict Mypy / typecheck / ESLint / production build 全部通过

### Release maintenance（2026-08-19）

- Python package、workspace FastAPI、Auth gateway FastAPI、前端 package 与 lockfile 统一为未打 tag 的 `0.0.28`；FastAPI 元数据直接复用 Python `__version__`
- 新增版本一致性回归测试，防止 Python/API/前端版本再次漂移
- 补齐标准 MIT 根 `LICENSE`，`pyproject.toml` 改为引用该文件，并验证构建 wheel 携带许可证正文与 `License-File: LICENSE` 元数据
- 修复 Playwright full-suite 三处测试竞态：等待第二条 persisted assistant、logout 使用隔离 admin token、Session 删除等待路由切换并按后端 ID 验证
- 当前发布前门禁：完整 Chromium Playwright 45/45 passed；固定 DDGS + GLM 真实 smoke 3/3 passed

### P1-E Multi-Provider Switching（✅ COMPLETE / FROZEN；待 merge / tag 授权）

- **Multi-provider profile / settings / session switching**——GLM / Qwen / Kimi 三家 Provider 完整支持：用户在 Settings Modal 配置 API Key + Model ID → 顶部 Provider Selector 切换 → 每个 Session 独立保存 Binding → 浏览器 reload 后恢复
- **Secure credentials**——SecretStore Protocol（OSKeyring / InMemory / Env 三种 backend）；API Key 不进入 SQLite 明文 / 日志 / WS event / Markdown export / Pinia state / 浏览器 storage
- **Prompt / Regenerate runtime binding**——请求开始时冻结 `RequestProviderSelection`（profile_id / provider_id / model_id / selection_source 不可变快照）；运行中切换只影响下一次请求；Regenerate 使用当前 Session 当前 Binding
- **Frontend integration validation**——261 vitest + 2585 backend pytest + 144 定向测试 + 连续两次 37/37 Playwright E2E；0 marker leak / 0 external Provider host / 0 Anthropic product UI
- **Trusted UI Header hotfix（M2-F1）**——`api/client.ts` 三个 fetch helper 强制注入 `X-PI-Agent-UI: 1`；结构化错误（`{error: {code, message}}` / FastAPI HTTPException / validation detail）安全转换为用户可读文案，不再渲染为 `[object Object]`
- 最终归档：[docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md](docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md)
- M2 集成验证：[docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md](docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md)

### 路线调整（2026-07-16）

- **P1-D3 PDF Text Extraction**：⏸ DEFERRED——转出主路线（**2026-07-27 重启**：通过 P2-R 系列吸收并扩展，见 [docs/design/p2-r0-rag-contract.md](docs/design/p2-r0-rag-contract.md)）
- **PDF / Vector RAG**：⏸ DEFERRED——转出主路线（**2026-07-27 重启**：P2-R 系列正式引入本地知识库 RAG，见 [ROADMAP §P2-R](ROADMAP.md)）
- **P1-E Multi-Provider / Model Profiles**：✅ DESIGN FROZEN——7 个跨阶段边界 + Custom URL 安全已冻结，详见 [ROADMAP.md](ROADMAP.md#p1-e--multi-provider--model-profiles-design-frozen-2026-07-16)
- **P1-F Markdown Workspace Panel**：⚪ PLANNED
- **P2 Web Agent Enhancements**：⚪ PLANNED（P2-A URL Routing / P2-B Human Approval / P2-C Context Budget）

### 路线 Pivot（2026-07-27）

- **P2-R Knowledge / RAG Subsystem**：🟡 IN PROGRESS——P2-R0 RAG Contract Audit 启动。第一版范围：marker（PDF→MD）+ heading-aware chunk + SQLite FTS5 + Session-scoped Library ACL + `search_knowledge` AgentTool。详见 [ROADMAP §P2-R](ROADMAP.md) + [docs/design/p2-r0-rag-contract.md](docs/design/p2-r0-rag-contract.md)。
- 旧 "PDF / Vector RAG ⏸ DEFERRED" 与"不做列表中的 RAG / Vector Memory / Long-term Memory" 标记**已正式解除**——审计记录见 [docs/design/p2-r0-legacy-limit-audit.md](docs/design/p2-r0-legacy-limit-audit.md)。
- **Long-term user memory / 跨 Session 用户偏好**仍不做（区别于 RAG）。

### Added（已 merge 到 master）

- **Frontend dev/lint maintenance**（merge `1c2289d`，2026-07-16）——
  - `fix(dev): proxy websocket events through vite`（`d8bd2ad`）——dev `/ws/events` proxy（MEMORY `feedback_dev_run_gotchas` 已记录的修复）
  - `refactor(frontend): remove obsolete fallback and lint residue`（`b9f4b17`）——移除 `assistantSeenFromWs` flag / `pickFinalAssistantText` helper / 未使用 import ×4 / `let envelope` → `const` / `interface extends {}` → `type =` / `let args: string[]` 严格初始化
  - `chore: ignore frontend upload runtime artifacts`（`7fbd082`）——`.gitignore` 加 `/src/pi_agent_core_py/web/frontend/uploads/`（保留 `/uploads/` 根路径规则）
- **Documentation governance**（merge `d7df358` + finalize `834bd1d`，2026-07-16）——
  - 根目录 6 文档（README / STATUS / ROADMAP / CHANGELOG / TODO / PLAN）+ `docs/` 六分类（architecture / api / guides / validation / releases / archive）
  - markdown 链接 0 broken
- **P1-D2 Regenerate**（已冻结，HEAD `d53f331`）——
  - Diff-based `replace_messages`（历史 message ID + `created_at` 稳定）
  - `web_message_revisions` schema + migration v1→v2（6 status / partial unique active / 4 索引）
  - Revision repository（8 async 方法 + 9 错误类型 + 状态机）
  - Execution/persistence split（`_execute_prompt` / `_persist_normal_prompt_result` / `_persist_regeneration_result` / `_reset_harness_to_session`）
  - `POST /api/sessions/{sid}/messages/{aid}/regenerate` HTTP API + 错误映射
  - `GET /api/sessions/{sid}/messages/{aid}/revisions` 安全 serializer
  - Web PersistedMessage DTO + startup sweep（mark running revisions interrupted）
  - Frontend regenerate flow（in-place update by message_id / draft item / reconnect recovery）
  - 9 个 Regenerate E2E 用例 + backend integration test（next-prompt-after-regenerate sees B not A）
- `test_next_prompt_after_regenerate_sees_b_not_a`——直接观察 `FakeProviderAdapter.all_messages_calls`，验证下一轮 LLM 输入含 B 不含 A

### Security
- revision `error_summary` 截断 500 字符，避免泄露内部信息
- `GET /api/sessions/{sid}/messages/{aid}/revisions` 不返回 `content_json` / `base_content_sha256` / `request_id`

### Validation
- HEAD `d53f331`：1131 offline pytest passed / coverage 84.20% / 37 e2e PASS / ruff clean
- HEAD `1c2289d`（含 frontend maintenance）：bundle 142.91 KB JS / 40.15 KB CSS（与 D2 baseline 一致）+ E2E 37/37 双模式 + production hooks 0/0
- Production hooks: `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js`
- Core runtime diff（`9264267..HEAD`）：0 modifications to loop/agent/context/events/stream_events/messages（providers/ 在 P1-E 阶段允许扩展）
- 详细证据见 [docs/validation/p1-d/P1_D2_VALIDATION_REPORT.md](docs/validation/p1-d/P1_D2_VALIDATION_REPORT.md)

## [v0.0.26-export-markdown] — 2026-07-14

Tag commit: `ebbc896` · P1-D1 Export Markdown

### Added
- `src/pi_agent_core_py/web/markdown_export.py`——纯函数 renderer（`ExportMessage` + `render_session_markdown`）
- `GET /api/sessions/{sid}/export/markdown`——SQLite messages → Markdown 下载
- filename sanitize：path traversal / 控制字符 / 80 字符 / RFC 5987 UTF-8
- 413 大小限制（`MAX_EXPORT_CHARS=2M` / `MAX_EXPORT_BYTES=5MB`）
- 前端 `requestBlob` + `downloadBlob`（try/finally + `setTimeout(0)` 释放 Blob URL）+ SessionSidebar Export 按钮

### Security
- 三道边界：Blob URL try/finally + Content-Disposition CRLF 阻断 + MCP env value 不导出
- 按 role 过滤——不读 `mcp_servers` 表；不扫描用户正文中的 secret 字符串
- system prompt / request_id / toolResult / MCP env 一律不导出

### Validation
- 22 用例专项测试（含审核 4 补测：空 session / 404 双路径 / CRLF 阻断 / active draft 不导出）
- offline pytest: **990 passed** / coverage **90.38%** / ruff clean
- frontend build (e2e): 137.53 KB JS / 39.80 KB CSS
- 详见 [release notes](docs/releases/v0.0.26-export-markdown.md)

## [v0.0.25-extension-persistence] — 2026-07-13

Tag commit: `b4640aa` · P1-C Extension Persistence

### Added
- 上传 Skill 持久化（`web_uploaded_skills` 表 + canonical skill_json + raw Markdown + enabled 状态）
- MCP server 配置持久化（`web_mcp_servers` 表 + desired_enabled）
- MCP disabled tool 持久化（`web_mcp_disabled_tools` 表 + 结构化 key）
- Startup restore 顺序：session store init → extension store init/migrate → sweep running revisions → restore Skills → restore MCP
- 失败隔离：损坏 row / sha256 不匹配 / MCP attach timeout 单点失败不阻塞其他

### Security
- MCP env values **永不持久化**——只存 `env_keys`，从 `os.environ` 恢复
- API response 只返回 `env_keys`——绝不返回 value
- 缺失 env → `restore_status=needs_env` + `missing_env_keys`

### Validation
- 5 个 persistence E2E + 安全扫描（`P1C_SECRET_MARKER_7F3A91`）
- offline pytest: **968 passed** / coverage **83.78%** / e2e **28/28 PASS**
- 详见 [validation report](docs/validation/p1-c/P1_C_VALIDATION_REPORT.md) 与 [archived architecture](docs/archive/superseded-designs/P1_C_PERSISTENCE_ARCHITECTURE.md)

## [v0.0.24-async-architecture] — 2026-07-12

Tag commit: `79cea14` · P1-B Async Architecture（B1 + B2 + B2.1 + B3）

### Added
- **B1 Async Prompt + Request Registry**：`POST /api/prompt/async` → 202 + request_id → 后台 Task；`GET /api/requests/{id}` + `POST /api/requests/{id}/abort`；WebRunRequest lifecycle (queued → running → completed/error/aborted)
- **B2 WebEventEnvelope + Event Dedup**：`_web_event_hook` 一次性生成 envelope（7 字段）；全局 sequence 单调；`GET /api/events` 支持 `?session_id` / `?request_id` / `?after_sequence` / `?limit` + gap 检测
- **B2.1 Hardening**：gap 检测改全局 `lastGlobalSequence`；`seenEventIds` FIFO 淘汰（1000 上限）；chatStore `setActiveSession` 4 切换路径同步
- **B3 Async UI + Reconnect Replay**：前端切 async；`pendingEventsByRequest` 缓冲；`belongsToCurrentRequest` 隔离 turn-control 事件；`replayFromCursor` 合并 live/replay + dedupe；`pollRequestUntilTerminal` + `reconcileMessagesFromServer`；WS hello 加 `first/last_available_sequence` + `server_time`

### Fixed
- P1-B3 调试栈 5 轮定位的 JavaScript microtask/macrotask 时序陷阱——同步 POST finally 不清跨事件状态
- WebSocket 首次连接 bug——`createEventSocket` 末尾补 `connect()` 调用
- assistant 文本重复显示（POST 兜底 + WS event 竞态）

### Security
- `__e2eHooks.closeEventSocket` 仅 E2E build 暴露（`VITE_E2E_HOOKS=true`）
- 生产 build 经 tree-shake 完全剥离 hooks 字面量

### Validation
- 23/23 e2e（含 7 async-stream-reconnect）+ 889 pytest + coverage 84.36%
- 详见 [validation report](docs/validation/p1-b/P1_B3_VALIDATION_REPORT.md)

## [v0.0.23.1-web-claude-validation] — 2026-07-12

Tag commit: `80a2f6f` · P1-A 真实环境验证

### Added
- A1: API smoke 3 项（并发 409 / 重名 SKILL 409 / 不存在命令 502）
- A2: Playwright MCP tool lifecycle 真链路 E2E（add→test→enable→disable tool→enable→disable server→delete）
- A3: 真实 GLM e2e_probe 多轮 tool_use（`PROBE_OK_ALPHA_7F3A` 防幻觉 token）
- `tests/integration/test_real_glm_tool_use.py` + `tests/e2e/mcp-tool-lifecycle.spec.ts`

### Validation
- 12/12 Playwright e2e（含 MCP tool lifecycle 真链路）
- 843 offline pytest / coverage 84.39% / ruff clean
- 真实 GLM 多轮 tool_use smoke 通过
- 详见 [validation report](docs/validation/p1-a/P1_A_VALIDATION_REPORT.md)

## [v0.0.23-web-claude-p0-mvp] — 2026-07-07

Tag commit: `8817c84` · Web Claude P0 MVP（含 4 个真实环境 hotfix）

### Added
- **P0-1 SQLite sessions**：`SQLiteSessionStore`（WAL + 外键级联 + `UNIQUE(session_id, idx)`）；sessions CRUD + `GET /api/messages?session_id=`
- **P0-2 VirtualFileStore**：session 级文件存储；sha256 / mime / 大小限制（单文件 25MB / session 100MB）
- **P0-3 file tools**：`tools/list_files.py` + `tools/view_file.py`（md / html / csv / parquet / 文本；图片 unsupported；PDF 不解析正文）
- **P0-4 Skills API**：upload / enable / disable / GET；unknown skill web 层预校验 400（不再 500）；`include_prompt=true` 默认 403
- **P0-4 MCP API**：servers CRUD + test connection（不污染 harness）+ enable/disable + delete；tools enable/disable 真实 unregister/register
- **P0-5 default system prompt**：`build_default_system_prompt(*, skills, mcp_tools, file_tools_enabled)`
- **前端骨架**（Pinia + 5 store + 7 inline turn cards + Skills/MCP Manager Modal）
- 两栏 UI（SessionSidebar + ChatPanel）——**不做右栏调试 Drawer**

### Fixed (post-freeze hotfixes)
- uvicorn 进程需要 `websockets>=12` 库（TestClient 不暴露此依赖）
- `createEventSocket` 不调 `connect()` 导致 WS 永不连接
- assistant 文本重复显示（POST 兜底 + WS 时序竞态）
- Playwright Smoke 6 时序（默认 enabled 路径 + ESC 关 modal + getByText 精确匹配）

### Security
- **MCP env values 严格不回显**——response 类型只有 `env_keys`；前端 type=password + autocomplete=new-password；列表只渲染 key 名
- Prompt preview 默认禁用（`allow_prompt_preview=False`）
- Localhost only / no auth

### Validation
- 678 offline pytest / 6 e2e / frontend build 128.48 KB JS
- coverage 73.75%（低于 75% 门槛，但 exit code 0——历史 baseline，非 P0 回归）
- 详见 [release notes](docs/releases/v0.0.23-web-claude-p0-mvp.md)

## [v0.0.22-web-backend-stable] — pre-P0 baseline

> **注意**：此版本号来自 [`docs/releases/v0.0.22.md`](docs/releases/v0.0.22.md)，**本副本 git 仓库未保留对应的 tag**。
> 这是 v0.0.21（Step 21 Provider Adapter Refactor）之上对 Web app 层加固后形成的 baseline，作为 P0 MVP 的起点。

### Added
- `GET /api/sessions`（spec 复数路径，兼容 `GET /api/session`）
- `GET /api/mcp/tools`（spec endpoint，兼容 `GET /api/mcp`）
- `WS /ws/events`（spec 推荐的 WebSocket 实时通道，兼容 `GET /api/stream` SSE）
- `GET /api/stream?limit=N`（SSE 测试模式：发完 N 个 event 后正常关闭）
- `event_buffer_max_size` / `allow_prompt_preview` 配置项

### Fixed
- Hook 累积修复（`remove_on_event_hook` + `lifespan` 精确移除）
- `TraceEventBuffer` 上限 1000（之前无界）
- `POST /api/prompt` 已运行时返回 409（不再 500）
- Prompt preview 默认禁用
- WebSocket 慢客户端不阻塞全局（独立 `asyncio.Queue(maxsize=100)`）

### Validation
- 25 web integration tests + 563 offline pytest
- 详见 [release notes](docs/releases/v0.0.22.md)

[v0.0.26-export-markdown]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.26-export-markdown
[v0.0.25-extension-persistence]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.25-extension-persistence
[v0.0.24-async-architecture]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.24-async-architecture
[v0.0.23.1-web-claude-validation]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.23.1-web-claude-validation
[v0.0.23-web-claude-p0-mvp]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.23-web-claude-p0-mvp
