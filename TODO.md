# Current TODO

> 校准日期：**2026-09-03**。本文件只保留尚未完成或明确延期的事项；已完成阶段不再复制数百行历史记录，统一由 [`STATUS.md`](STATUS.md)、[`CHANGELOG.md`](CHANGELOG.md) 和 `docs/validation/` 追溯。

## 当前收敛执行顺序

- [x] **1. 修复全量 Ruff / strict Mypy，使仓库自身 CI 静态检查通过**（完成：Ruff 0 项；Mypy 114 files / 0 issues；后端 CI 3655 passed、6 skipped、12 deselected、coverage 83.73%；前端 lint / typecheck / build 通过；Python CI job timeout 由 10 分钟调整为 30 分钟以容纳完整门禁）
- [x] **2. 整理并提交当前工作区改动，同时更新 `STATUS.md` 到最新验证基线**（完成：运行时/测试/CI 提交 `c214d28`；81 个工作区路径完成分类与敏感信息审计，状态文档记录真实门禁结果）
- [x] **3. 统一 Python、FastAPI/Auth、前端与 release 版本号，并补齐仓库根 `LICENSE`**（完成：统一为 `0.0.28`；两个 FastAPI 工厂直接复用 Python `__version__`；新增跨 Python/前端/lockfile/API 一致性测试；根 MIT `LICENSE` 已进入 wheel；提交 `8a6ff2e`，发布 tag `0.0.28`）
- [x] **4. 复跑当前提交的完整 Playwright E2E 与最终 GLM 真实 smoke**（完成：修复 full-suite 的 Regenerate 完成等待、logout 共享 token 撤销与 Session 删除路由竞态，提交 `51ce3c7`；完整 Playwright 45/45 passed；固定 DDGS 1 项 + GLM 2 项真实 smoke 3/3 passed）
- [x] **5. 继续 pi-agent 对齐：补齐 model、thinking level、streaming message、pending tool calls 等公开 Agent 状态**（完成：新增 secret-free `AgentModelState`、完整 `ThinkingLevel`、`is_streaming` / `streaming_message` / `pending_tool_calls` / `error_message`；按消息与工具事件生命周期更新，request 结束、异常与 reset 统一清理；`/api/state` 与前端类型同步；提交 `848ae1d`）
- [x] **6. 按当前开发阶段精简测试套件**：采用“最小可观察行为”预算，删除退役 Chunk-RAG、历史 Step/Smoke、Provider 内部实现与重复边界矩阵，只保留核心语义、安全/数据风险、公开 API 和关键用户旅程；Backend 从 4093 降到 2105 collected，Frontend Vitest 从 352 降到 180，Playwright 从 54 收敛为 8 个规格/18 项。第二轮仅改测试与策略文档，按开发阶段规则不复跑耗时全量门禁；当前收集、Ruff 和保留的最小定向测试通过。历史冻结证据继续归档在 `docs/validation/`
- [x] **7. 发布 `0.0.29`**（完成）：Python/API、Web 前端与 Wiki Parser Worker 的 package/lock/OCI/compliance metadata 已统一；CHANGELOG 分隔 `0.0.28`/`0.0.29`；Ruff、strict Mypy 179 source files、Backend 51、Frontend 11、typecheck/lint/build 通过；两个 `0.0.29` wheel 的版本与许可证已核验；创建本地 annotated tag `0.0.29`，未配置 remote 因而未 push
- [x] **8. 实现三类意图路由**（完成）：产品入口默认启用 provider-neutral 的 `read_only | coding | knowledge` 决策；Knowledge 只由 durable Conversation binding 选择，Coding 复用现有 Sandbox 自动编排，只读路线同时裁剪写入/执行工具并注入只读约束。支持 `intent_mode` 显式覆盖和旧 `coding_mode=true`；Prompt、async request、Context Budget、`/api/state` 与 Turn 卡公开 secret-free 决策审计。仅修改 1 个既有测试文件并新增 2 项，覆盖三条路线、否定约束和显式覆盖；定向 Backend 2 passed，Ruff、strict Mypy、Frontend typecheck/lint 通过。设计见 [`docs/design/coding-agent-intent-routing.md`](docs/design/coding-agent-intent-routing.md)
- [x] **9. 对齐上游 pi 的 `ai` 模块核心契约**：统一 provider/API/model 身份与不可变历史转换；跨模型不重放签名/redacted thinking；新增 provider-neutral 图片块和视觉能力降级；修复 tool-call/result ID 与缺失结果；Usage 增加 cache/reasoning/cost 扩展位；Provider 建连失败支持首事件前有界重试。专项 159 passed；完整 Backend 2121 passed / 7 skipped / 9 deselected / coverage 77.13%；Ruff、strict Mypy 187 files、Frontend 183/183 + typecheck/lint/build 全部通过。完整差异与剩余边界见 [`docs/validation/pi-ai-parity-2026-08-31.md`](docs/validation/pi-ai-parity-2026-08-31.md)
- [x] **10. 完成第 2 项 `agent` 对齐**：按当前上游修正 `shouldStop` / preparation / terminal lifecycle；补齐 prompt 图片与 AgentMessage batch、CustomMessage 转换/持久化、signal-aware transform、工具 `prepare_arguments`、before terminate、after field patch、迟到 update、assistant-tail queued continue、thinking 与 next-turn runtime replacement、公开 system/tools 状态和完整 transcript + per-run delta。专项 36 passed、邻接 127 passed；完整 Backend 2131 passed / 7 skipped / 9 deselected / coverage 77.24%；Ruff、strict Mypy 187 files、Frontend 183/183 + typecheck/lint/build 全部通过。详见 [`docs/validation/pi-agent-parity-2026-08-31.md`](docs/validation/pi-agent-parity-2026-08-31.md)
- [x] **11. 完成第 3 项 `coding-agent` 对齐（阶段 1–3）**：建立 `coding_agent_app.core` 的 Application/Runtime/Session/Services/Settings/Resources/Toolset/Prompt/SDK 边界；按 Session ID 合并并发创建并收敛关闭竞态，请求模式工具集 fail closed 且异常后原子恢复；Web 不再直接替换 Harness tools/client。Sandbox automation/workspace 迁入产品包，旧路径保留对象身份兼容 facade，并以 AST 固定产品包不得反向依赖 Web。随后把每个持久 Web Session ID 真实映射为独立 Runtime Session/Harness，并统一 Provider、Skill、MCP、Workspace 与 Prompt 的请求快照装配。详见 [`docs/validation/pi-coding-agent-parity-2026-09-01.md`](docs/validation/pi-coding-agent-parity-2026-09-01.md)
- [x] **12. 完成第 4 项 `session-backends/sqlite-node` 对齐**：新增 Harness 级 Repository/Storage/Search 协议与物理 backend 分层；补齐全局 sequence/log、typed entry/record 查询、统计、name/label fact、writer lease/fence/heartbeat、branch cache/repair、fork、有序事务 migration、未来版本拒绝和惰性 FTS5。Session/Extension/Plan 共用 connection 级可重入串行化，异常/取消与关闭残留事务统一 rollback；每个已启动 Web Runtime Session 持有独立 Storage handle 并在删除/关闭时释放。专项 94 passed、共享服务邻接 152 passed、兼容回归 75 passed；Backend 全量 2175 passed/7 skipped/9 deselected/76.27%；Ruff、strict Mypy 265 files、Worker 静态门禁、Frontend 183/183 + typecheck/lint/build、Playwright 19/19、428-entry wheel 隔离导入与 migration 初始化均通过。详见 [`docs/validation/pi-session-backend-parity-2026-09-03.md`](docs/validation/pi-session-backend-parity-2026-09-03.md)
- [x] **13. 完成第 5 项 `telemetry` 对齐并增加 Admin 前端**：新增 Backend-neutral Context/Span/Reader、noop/memory/schema 与 passive SQLite Recorder；Web/Coding Agent 统一记录嵌套请求、Provider/model、token/cost、工具、耗时和 outcome，所有账号共享独立 Telemetry DB。Auth schema v2 持久化 Admin 角色，服务端 API 强制鉴权；Admin 前端提供摘要、趋势、筛选、请求与事件详情。Prompt/消息/Tool 参数和输出/凭证/异常正文不落盘。专项 21 passed；Backend 2187 passed/7 skipped/9 deselected/76.43%；Ruff、strict Mypy 275 files、Worker 静态门禁、Frontend 187/187 + typecheck/lint/build、Playwright 20/20、444-entry wheel 隔离导入均通过。详见 [`docs/validation/pi-telemetry-parity-2026-09-03.md`](docs/validation/pi-telemetry-parity-2026-09-03.md)

## P0 — Coding Agent Workspace 连续性

整体架构、内容分类、自动记忆状态机和无上下文续作标准见
[`docs/design/coding-agent-workspace-continuity.md`](docs/design/coding-agent-workspace-continuity.md)。

- [x] **阶段 1：从 `pi_agent_core_py` 分离 Workspace 领域模块**（完成）：新增顶层 `agent_workspace`，迁移规范 Store、固定文档转换和 provider-neutral continuity；Store 以结构化 async upload port 替代 FastAPI 类型。新增 `coding_agent_app` 承载 Workspace/Sandbox 产品适配，Web/Core 只保留组合代码及旧 import 兼容层；新包不 import Core、FastAPI、Provider 或 Sandbox，独立 import boundary PASS，wheel 已包含两个新顶层包。未新增或修改测试，复用既有 Workspace/文件工具/Checkpointer/文档/Sandbox 回归 133 passed；全量 Ruff PASS、strict Mypy 177 files / 0 issues
- [x] **阶段 2：每轮完成后自动提炼并更新 `Memory.md`**（完成）：普通 Session Prompt 与 Regenerate 在回答成功持久化后生成有界、可校验的 turn evidence，并以每 Session 串行 `auto_memory` durable operation 调用当前绑定 Provider，受控生成累计 `Memory.md`；主回答不会因记忆失败改成失败，未完成 intent 会在下一轮串行 preflight 重试，持续失败时把 evidence 作为不可信历史上下文注入。下一轮还能从 canonical 最新完整 turn 重建“消息已提交但 intent 尚未落库”的崩溃窗口。Coding turn 在签名 Artifact 待审批期间延迟写入，避免 `Memory.md` 提升 Workspace revision 破坏 Validation→Freeze 基线，Sandbox 终态后自动续跑；Knowledge 模式明确跳过。产品启动器默认启用，低层 `create_app` 保持显式 opt-in；公开 Prompt/request 与 `/api/state` 返回 secret-free continuity 状态。只修改 1 个既有测试文件并新增 2 项，覆盖成功更新且不清空消息、Provider 失败保留回答并在下一轮恢复；Backend 最小相关 11 passed，Ruff、strict Mypy、Frontend typecheck/lint PASS
- [x] **阶段 3：细分 Coding Workspace 内容**（完成）：在独立 `agent_workspace` 中固定 `HANDOFF.md`、`tasks/**`、固定/共享 `docs/**`、`inputs/**`、`artifacts/**` 分类和单一 `WorkspacePathPolicy`；新 Session 仍只初始化 `AGENT.md`/`Memory.md`，其它目录按首份内容惰性出现。普通上传进入正文不可改写但可删除的 `inputs/**`，代码上传保留 `scripts/**`；Agent 非代码产物默认进入 `artifacts/**`，只额外允许 `docs/notes/**`，Coding 模式提示把系统连续性路径视为只读。文件 API、Agent 工具与前端共享 category/owner/edit/move/delete/agent/sandbox/immutable metadata，右栏按后端策略关闭编辑与删除入口；Sandbox 保留既有普通 Markdown 兼容发布，同时拒绝 `HANDOFF.md`、`tasks/**`、`inputs/**`、固定 docs、`documents/**` 和 `.pi-agent/**`。只修改 1 个既有测试文件中的 2 项，覆盖上传/Agent 默认路由、公开策略和受保护路径；定向 Backend 124 passed、Frontend 8 passed，全量 Ruff、strict Mypy 177 files、Frontend typecheck/lint PASS
- [x] **阶段 4：已发布代码的流程总结**（完成）：新增独立 `CodeContinuityService`，从 revision-bound、逐文件校验的 `scripts/**` 物化树确定性生成只读 `docs/architecture.md`、`docs/code-flow.md`、`docs/validation.md`；用户代码上传/删除和已批准 `WorkspacePublished` 触发，启动恢复 stale/failed 及功能上线前已有代码。Workspace mutation 原子标记 stale，三份文档全部持久后才发布 current，代码并发变化 CAS fail closed；失败不回滚用户代码。Validation 只记录真实 Sandbox evidence/check/output SHA，普通上传明确未验证，不接受模型自述。产品入口默认启用、低层组合显式 opt-in，API 与右栏显示 revision/hash/current/stale/failed。只修改 1 个既有测试文件并新增 2 项：主路径及 renderer 失败保留上传并标记 stale；Backend 文件 API 30 passed、Workspace/Sandbox 邻接 85 passed，Frontend Workspace/Sandbox 6/6，Ruff、strict Mypy、typecheck/lint PASS
- [x] **阶段 5：无聊天上下文续作验收**（完成，提交 `c9ed330`）：顶层 `agent_workspace.WorkspaceContextAssembler` 在稳定 revision 上以 strict UTF-8、普通文件/stat/SHA-256 复验和 96,000 字符总预算，统一装配 `AGENT.md`、可选 current task/HANDOFF、Pending Memory、`Memory.md`、current 代码摘要、Workspace 树与非终态 Sandbox 投影；stale/failed 代码摘要正文不进入 Prompt，待批准 Artifact 明确标记未发布，必需文件不可验证时在 Provider 调用前 409 fail closed。普通 Prompt、Regenerate 与 Context Budget 复用该路径，Knowledge/Checkpointer 隔离并清理跨模式 metadata；Prompt/request/budget 公开 secret-free revision/hash/included/omitted 审计。只修改 1 个既有 Backend 测试文件并新增 2 项，覆盖零聊天历史进程重启续作，以及 Pending Memory + 待批准 Artifact + stale code 的组合风险；相关 Backend 128 passed、Frontend Context Budget 6/6，Ruff、strict Mypy、Frontend typecheck/lint 通过

## P0 — Plan Mode / Planner–Executor–Verifier

中心编排、持久化通信信封、任务状态机、角色权限与 Sandbox 复用设计见
[`docs/design/plan-mode-multi-agent.md`](docs/design/plan-mode-multi-agent.md)。Plan Mode 是 Coding
路由的执行方式，不新增第四类意图。

- [x] **阶段 1：PlanStore 与通信协议**：共享 Session SQLite 实现 PlanRun/PlanVersion/Task/Event、严格 DTO、事件幂等键、单调 sequence 与启动 `interrupted` 收敛；角色通信固定携带 run/version/task/attempt/causation 投影
- [x] **阶段 2：Planner**：独立 Agent/Harness 仅以 `plan_submit` 提交有向无环 Plan；前端生成可恢复任务列表卡并停在“批准并执行”
- [x] **阶段 3：Executor**：批准后创建/复用单一 Session Sandbox，按依赖顺序执行；每个任务只允许结构化 complete/blocked 终态，不把内部角色 transcript 写入 Session
- [x] **阶段 4：Verifier**：独立只读 Agent 按实际 Sandbox diff 和验收标准审核；通过打勾，失败持久显示原因、建议和固定分类；`retry_executor` 最多两次
- [x] **阶段 5：最终验证与审批**：全部任务通过后复用服务器 Validation→Freeze TOCTOU 屏障并停在 Artifact 待批准；发布/放弃后 Plan 收敛到 completed/cancelled
- [x] **阶段 6：前端与恢复验收**：实现 Plan 可用状态/开关、任务卡、批准、角色阶段、刷新恢复与 Stop；仅在 1 个既有测试文件增加 2 项，成功 P→E→V→Freeze 与 Verifier 拒绝不冻结均通过；邻接 Backend 27 passed，Ruff、strict Mypy、Frontend typecheck/lint 通过

## P0 — Sandbox 审阅、冲突恢复与工作区交互

- [x] **冻结制品审阅**：新增只读 Artifact 文件 API，待批准与发布冲突状态下可从 Workspace 文件树逐项预览/下载冻结内容；Python 预览使用依赖无关的安全词法高亮，不执行文件
- [x] **发布冲突恢复**：`publish_conflict` 保留签名 Artifact 与审计状态；源基线已改变时可重新冻结，冲突解除时可重试同一发布；状态机、公开 actions/transitions、事件和前端操作保持一致
- [x] **自动 Coding 空变更修复**：空 diff 不再冻结为可批准 Artifact；自动 Coding 首次无改动时以受控修复提示重试一次，仍无改动则返回稳定 `coding_no_changes`
- [x] **交互与生命周期收敛**：Chat 直接显示待批准/冲突操作条，Thinking 支持首段正文前实时展示，桌面三栏与 Workspace 上下分区可拖拽并持久化；删除 Session 前先停止活跃请求并等待 Harness 释放
- [x] **完整门禁与回归收敛**：修复 Plan Mode 绕过单一 Provider binding 入口的安全 AST 回归、两组旧前端 mock 的 Plan state stderr warning，以及生成文档专用只读提示；全量 Ruff PASS、strict Mypy 185 files / 0 issues、Backend 2110 passed / 7 skipped / 9 deselected / coverage 77.17%、Frontend 183/183 + typecheck/lint/build、Playwright 19/19 且最终无 retry/failure

## P0 — Session Workspace 一体化（既有基线）

完整架构、所有权边界、Sandbox 发布流和富文档转换约定见
[`docs/design/workspace-sandbox-integration.md`](docs/design/workspace-sandbox-integration.md)。

- [x] **阶段 1：统一 `WorkspaceStore` 与初始化 `Memory.md`**（完成）：以 `WorkspaceStore` 作为 Session 文件唯一规范事实源，`VirtualFileStore` 仅为同一实现的兼容别名；新旧 Session 均幂等拥有唯一根 `AGENT.md` 与 `Memory.md`，迁移保留已有正文/file id 并规范大小写等价旧路径与 purpose，两个固定根文件均禁止删除；Ruff PASS、strict Mypy 141 files / 0 issues、Workspace/文件/Auth/Checkpointer 定向 125 passed（`-W error`）、Backend CI 3857 passed / 8 skipped / 12 deselected、83.69% coverage
- [x] **阶段 2：代码与 Markdown 规则**（完成）：`WorkspaceStore` 按扩展名把 Agent 写入和用户上传的代码统一映射到惰性逻辑根 `scripts/`，安全相对目录自动约束在其下；新增普通 Markdown 精确创建、正文更新、移动/重命名和删除 API，固定根文件继续受保护；每个 Session 以隐藏 `.workspace.json` 持久化单调 revision，mutation 同时支持逐文件 SHA 与 Workspace revision 乐观锁，失败保持文件树/revision 不变，删除 tombstone 和状态 temp 可在重启时收敛；Agent `write_file`/`list_files` 与前端 API/types/store 返回 revision。全量 Ruff PASS、strict Mypy 141 files / 0 issues、阶段定向 Backend 116 passed、Frontend typecheck/lint 与 Vitest 405/405 passed
- [x] **阶段 3：右侧 Workspace 成果面板**（完成）：`AppShell` 改为 Sessions / Chat / Workspace 三栏，窄于 1050px 时右栏降级为带新成果提示的 drawer；右栏监听成功 `write_file` ToolResult 的 `file_id`/Workspace revision，立即刷新、选中并预览 Agent 生成的 `.md`、`.py` 等成果，整页刷新后从持久 ToolResult 恢复；Files 支持目录树、最新成果提示、用户上传（不自动附加到聊天）、Markdown 创建/安全预览/编辑、代码查看与下载，Sandbox / Changes 复用现有 operation、日志、验证、diff 和完整审批发布 Modal。Frontend typecheck/lint/build PASS、Vitest 409/409、Playwright 全量 52/52（含成果自动展示/刷新恢复、Markdown 编辑、代码上传、窄屏 drawer），0 retry / 0 failure
- [x] **阶段 4：统一 Sandbox 快照与发布目标**（完成）：从 `WorkspaceStore` 物化 E2B 快照，固定验证和用户确认后事务发布回同一 Workspace，保护根文件与系统路径并原子提升 revision
  - [x] **阶段 4A：显式状态机与 Validation→Freeze TOCTOU 屏障**（完成）：以单一转换表驱动 Backend/API/UI 可用动作；持久化更新使用完整旧记录 compare-and-swap，拒绝并发请求以旧状态覆盖新状态；冻结前在 operation 锁内重验 validation evidence、配置 SHA 与完整 workspace SHA，冻结后再次核验远端导出、下载归档和最终 workspace SHA。屏障前过期回到 `validation_failed` 并允许重验，屏障后 `artifact_stale` 保留明确错误并终态销毁，禁止在已经 fail-closed 的实例内重试。修改 1 个既有测试验证公开动作/转换与 stale CAS；Backend 定向 9 passed，Ruff PASS、strict Mypy 25 source files / 0 issues，Frontend typecheck/lint 与 CodingSandbox Vitest 3/3 passed
  - [x] **阶段 4B：WorkspaceStore 快照输入**（完成）：在 Session mutation lock 内按逻辑路径导出指定 revision 的不可变树，逐文件复核 containment、regular file、metadata SHA/size 与前后稳定 stat，记录源 revision/tree SHA；只把用户可见内容和系统注入的固定 validation config 打入 Sandbox baseline，不泄露 `file_id`、`metadata.json`、`.workspace.json` 或物理路径。主应用配置 WorkspaceStore 时不再创建/扫描独立项目事实源；源 Workspace 后续升版不会改变已创建 baseline。4C 接入前 Workspace 来源 operation 可运行、验证和冻结审阅，但 `publish_available=false`、Backend/API/UI 均 fail closed。未新增测试文件，仅修改 1 个既有 lifecycle 测试覆盖主成功路径、revision 隔离和发布暂停；Backend 73 passed / 1 capability skipped，Ruff PASS、strict Mypy 28 source files / 0 issues，Frontend typecheck/lint 与 CodingSandbox Vitest 3/3 passed
  - [x] **阶段 4C：WorkspaceStore 事务发布**（完成）：复用既有签名 Artifact 与本地事务 Publisher 在隔离镜像中完成签名、baseline、manifest、成员和最终树复验，再把允许的 `scripts/**` 与普通 UTF-8 Markdown 变更交给 `WorkspaceStore` 批量提交；固定保护 `AGENT.md`、`Memory.md`、`.pi-agent/**` 与 `documents/**`，大小写冲突和 symlink/reparse fail closed。目标 Session 锁内重新核验 baseline revision/tree SHA 和全部当前内容 SHA，使用 durable intent/phase journal、immutable generation、metadata pointer、删除 tombstone 与反向 rollback，一批新增/修改/删除只提升一次 revision，启动时收敛未提交或已提交待清理事务。发布完成记录 `published_workspace_revision`，广播 `workspace_changed`，右侧面板刷新并聚焦最新成果。未新增测试文件，在 1 个既有测试文件新增 2 项：成功批量提交/单 revision、stale baseline 零写入；Backend 86 passed / 2 capability skipped，Ruff PASS、strict Mypy 28 source files / 0 issues，Frontend typecheck/lint 与相关 Vitest 6/6 passed
- [x] **阶段 5：固定文档转换工作流**（完成）：`.pdf/.docx/.xlsx` 上传固定归档到 `documents/<document-id>/original.*`，以 `document_original` purpose 保持不可变；转换器只读取 revision-bound Workspace 快照，`pypdf` 输出分页 Markdown/内嵌图片并把低文本 PDF 标为 `needs_ocr`，`python-docx` 输出标题/段落/列表/表格与图片，`openpyxl` 输出 workbook 摘要、逐 Sheet CSV/schema 并把公式转成惰性文本。`content.md/tables/assets/manifest.json` 作为 `document_conversion` 只读文件经复用 4C journal 的单 revision 事务发布，目标端重验 revision/tree/content；manifest 固定记录 source/converter/config/status/warning/输出 SHA，同 SHA+converter version 且制品完整时幂等复用，失败首试只发布安全 manifest、无半成品，重试失败保留旧成功制品。上传响应与显式 retry API 均返回转换结果，前端刷新右侧树并优先展示/附加 `content.md`。与 Knowledge/LLM Wiki 无 import、DB、队列或 Provider 复用；OCR 延期，PDF 结构化表格首版明确 warning。新增 1 个测试文件/2 项测试：成功事务+幂等复用、失败无半成品+原件不可变；Backend 邻接 118 passed，Ruff PASS、strict Mypy 4 source files / 0 issues，Frontend typecheck/lint PASS，PDF/DOCX/XLSX 真实本地 smoke 3/3
- [x] **阶段 6：完整验收**（完成）：全量 Ruff 0 项、strict Mypy 170 source files / 0 issues、Backend `2095 passed, 7 skipped, 9 deselected`、Frontend typecheck/lint/build 与 Vitest `180/180`、Playwright `19/19` 且 0 retry/flaky。新增 1 项 Browser E2E 验证 XLSX 上传后自动转换、右栏优先展示 `content.md` 和只读提示；修复 Phase 5 文件树模板编译错误、`Memory.md` 删除动作回归与刷新测试未等待 Prompt 202 的竞态。真实 E2B Managed Sandbox smoke 通过 9 个代码工具、失败验证不得冻结/回写、修复后重验、签名 Artifact、审批门禁、`WorkspaceStore` 单 revision 发布和销毁；同时修复 Windows 下 Publisher state 深层路径超过 MAX_PATH 导致的 `winerror=206`，事务 state 改为 staging 根下的短兄弟目录
- [x] **阶段 7：自动 Coding 请求编排**（完成）：Chat 输入区新增显式 `Code` 模式；请求携带 `coding_mode=true` 后由 Backend 自动创建或安全复用当前 Session Sandbox、等待 ready，并把本轮 ToolRegistry/PermissionPolicy 收窄为 9 个隔离的 `coding_*` 工具，避免通用策略产生逐工具审批。Agent 获得固定编码/自验约束；模型结束后 Backend 独立重跑服务器选定验证，只有成功才执行 Validation→Freeze TOCTOU 屏障并停在 `awaiting_approval`，绝不自动发布。Stop 在创建/验证/冻结阶段可取消并销毁；验证失败保留 `validation_failed` 供下一轮修复。Context Budget 包含 Coding 指令；右栏自动切换 Changes、窄屏 Results 显示审批提醒。只修改 1 个既有测试文件并新增 2 项，覆盖主路径和验证失败不冻结；真实 E2B 由新编排器完成创建、最终重验和冻结待批准，9 工具、签名、模拟批准后的单 revision 回写及销毁全部通过（25.328s）

## P0 — LLM Wiki（替代 Knowledge/RAG）

完整产品合同、目录与权限、Provider、Schema、Change Set、页面图谱、Knowledge Agent 和退役方案见
[`docs/design/llm-wiki.md`](docs/design/llm-wiki.md)。旧 P2-R Knowledge/RAG 文档只作为历史基线，
不再指导后续产品实现；Chunk 检索主链路废弃，FTS5 仅索引已批准 Wiki 页面的标题、别名和正文。

- [x] **阶段 0：冻结 LLM Wiki 产品合同**：确认先创建 Wiki Space；PDF/单文件 HTML MVP；`raw/` 对 Agent 强制只读；每个来源一篇入口页并允许主题子页面；页面级 FTS5；固定页面关系和系统 `derived_from`；每个 Space 多对话；所有页面/图谱修改进入同一 Change Set 一次审批；旧库结构不迁移；明确不恢复 Chunk-RAG
- [x] **阶段 1A：Provider Contract v1 与 Marker 历史 Gate**（完成但停止后续实施）：已验证独立 `wiki_parser` 生命周期、immutable/extra-forbid DTO、固定安全错误、来源/制品 SHA、配额/超时/取消/幂等销毁和完全离线 Fake；Marker 专属实现路线于 2026-08-23 被双 Parser 取代，历史审计仅见 [`docs/design/llm-wiki-marker-provider-gate.md`](docs/design/llm-wiki-marker-provider-gate.md)
- [x] **阶段 1B：冻结双 PDF Parser 架构**：PyMuPDF4LLM 负责 fast，Docling 负责 accurate 与 auto 质量回退；两者只读取原始 PDF；API 固定 `auto|fast|accurate`；Docling standard/ocr Converter 启动期复用；路由/质量阈值版本化且不从 API 注入；保存 Parser/version/preset/config/质量/attempt；保持无 Chunk-RAG；PyMuPDF4LLM 选择 AGPL-3.0 合规路径。设计见 [`docs/design/llm-wiki-dual-pdf-parser.md`](docs/design/llm-wiki-dual-pdf-parser.md)
- [x] **阶段 2：实现 WikiStore、`wiki.db` 与新目录结构**（完成）：新增独立 `pi_agent_core_py.web.wiki`，以三重 DB 身份/version gate、SQLite capability/integrity gate、STRICT/FK/CHECK/UNIQUE 建立 Space/Source/Artifact/Page/Revision/PageSource/Edge/ChangeSet/Item/Conversation/Job schema v1，明确不创建 Chunk/Chunk FTS；实现带跨连接 CAS/软删除状态机的 Space CRUD，固定 `spaces/{space_id}/{raw,pages}`、canonical `space.json`、全层 casefold/symlink/reparse/特殊文件防护、Raw no-clobber 与 Page atomic replace、启动镜像重建/orphan 报告/staging 隔离；显式 `retire` 在独占锁内先生成 SQLite 一致快照与逐文件 SHA 的只读 legacy backup，再清理旧固定路径，默认 `preserve` 且未触碰真实用户旧库。设计见 [`docs/design/llm-wiki-store-v1.md`](docs/design/llm-wiki-store-v1.md)；Ruff PASS、strict Mypy 152 files / 0 issues、新 Wiki 52 passed / 4 capability skipped、新旧邻接 149 passed / 5 skipped、Backend 3949 passed / 12 skipped / 12 deselected / 83.35%
- [x] **阶段 3：实现 PDF/HTML Raw Ingestion**（完成；Contract v1 Backend/HTML/Fake PDF、双 Parser Contract v2、Raw parse revisions、Fake v2、Contract v2 主应用编排、AGPL Worker 合规包、真实 Parser adapter/source Gate、离线 OCI/真实 smoke，以及 Space/Source/解析状态/Raw artifact 前端均已完成；设计见 [`docs/design/llm-wiki-raw-ingestion.md`](docs/design/llm-wiki-raw-ingestion.md)）
  - [x] Source/Artifact/Job DTO、不可变 `source.pdf|source.html`、同内容冲突、固定 Raw bundle 路径、状态机与跨连接原子 parse claim
  - [x] 零网络单文件 HTML Parser：严格 UTF-8、移除 script/style/iframe/object/embed/form 等主动内容、不获取 CSS/图片/iframe，转义来源 Markdown 注入，仅提取受 MIME/魔数/单图/总量/数量配额约束的 `data:` PNG/JPEG/WebP
  - [x] 不可信 Parser tar 导入：重算 archive/manifest/逐文件 SHA，核验 job/source/provider/version，拒绝路径穿越、大小写重复、symlink/目录/特殊成员、未声明文件、错误 MIME/魔数、超额文件和非 UTF-8 Markdown；全部验证后才以 no-clobber/idempotent 语义写入 Raw
  - [x] 独立 `/api/wiki` lifespan/API 与后台 Worker：旧 Chunk Knowledge 可不启动；Space/Source/Artifact/Job、上传、解析排队、原件/制品安全浏览；重启把中断 attempt 固定失败后创建新 attempt，不重放旧 Provider handle
  - [x] Fake Provider PDF 与 HTML 集成/安全/恢复验收：Wiki 阶段 1–3 定向 `104 passed, 4 skipped`；Ruff PASS；strict Mypy 157 source files / 0 issues；Backend 全量 `3972 passed, 9 skipped, 15 deselected` / 82.78% coverage
  - [x] 撤销未提交 Marker 专属 Sidecar/镜像/配置/测试，保留通用 Provider、Fake、Raw Ingestion 和不可信 artifact 边界
  - [x] 升级 `wiki_parser` Contract v2：新增独立 `ParserProviderV2` 与 immutable/extra-forbid DTO，固定 `auto|fast|accurate`、hash-pinned routing config、PreflightReport/RouteDecision/QualityReport、逐页 ParsedDocument、同源 SHA 的 Attempt evidence、规范 Markdown、Artifact v2 与安全错误；`fast` 不回退，只有 `auto` 可形成 PyMuPDF4LLM quality-rejected → 原始 PDF Docling 的单次回退链。Contract v1 暂留给现有 Raw Ingestion，禁止静默混用；Ruff 全量 PASS、strict Mypy 159 files / 0 issues、v1/v2 Contract 与隔离 45 passed、阶段 3 邻接 68 passed
  - [x] 升级 Wiki schema/Raw bundle：schema v2 以 Source `selected_parse_revision_id + selection_version + selected_at_ms` 取代 flat parser/path 字段，新增 immutable ParseAttempt/ParseRevision 与 revision-scoped Artifact；所有新 HTML 和 Contract v1 兼容 PDF 制品写入 `parses/{parse_revision_id}/{parsed.md,pages/**,images/**,manifest.json}`，`selected.json` 原子镜像且启动可由 DB 修复；重解析保留历史、失败保留旧 selected、跨连接 CAS 防 ABA、跨 Source FK fail closed。旧 schema v1 固定返回 `schema_rebuild_required`，不迁移、不覆盖；Wiki v1/v2/Raw/Worker/API 邻接 `126 passed, 4 skipped`，新增 revision 专项 5 passed，Backend 全量离线 `3994 passed, 9 skipped, 15 deselected`，Ruff PASS、strict Mypy 159 files / 0 issues
  - [x] 实现完全离线 Fake Router/Fast/Accurate：新增独立 `FakeDualPdfParserProvider` 与内容不泄露的 Scenario/Document/Asset DTO，任务创建时核验并冻结原始 PDF 字节；覆盖 accurate/扫描/复杂 PDF 直接 Docling、auto fast 通过、auto fast 质量拒绝后单次 Docling 回退、显式 fast 拒绝且不回退、Docling 失败终止不循环、取消/销毁、hash-pinned routing config、逐页 Artifact v2 tar 与原始 PDF SHA 不变式。Fake v2 专项 10 passed，Wiki 全邻接 `136 passed, 4 skipped`，Backend 全量离线 `4004 passed, 9 skipped, 15 deselected`，Ruff PASS、strict Mypy 160 files / 0 issues
  - [x] 接入 Contract v2 不可信 artifact 导入与现有 Wiki Ingestion/Worker/API：严格交叉核验 receipt/status/manifest/job/source/provider、archive/manifest/逐文件 SHA、规范逐页 Markdown、图片 MIME/魔数与路径/数量/大小配额；`auto|fast|accurate` 精确贯穿上传、排队和崩溃恢复，provider attempt/fallback/quality/route 证据映射为内部不可变记录，artifact 拒绝时保留成功 attempt 但不发布 revision，最终 selected parse revision 与制品/attempt 在同一事务原子提交；支持历史 job/attempt/revision 与指定 revision artifact 浏览，Contract v1 兼容路径保持显式隔离。新增 13 项回归；Wiki 全邻接 `149 passed, 4 skipped`，Backend 全量离线 `4017 passed, 9 skipped, 15 deselected`，Ruff PASS、strict Mypy 160 files / 0 issues
  - [x] 建立独立 AGPL-3.0 Parser Worker 合规包：新增不被主 wheel 打包/导入的 `workers/wiki_parser_worker`，固定 `AGPL-3.0-only`、未经改写的 GNU 官方许可证正文及 SHA、notices、完整 Corresponding Source manifest、Source Offer、SPDX 2.3 SBOM、自包含 smoke 与 wheel verifier；源码 API 从逐文件稳定快照生成规范路径、归一 metadata、确定性 `tar.gz` 和 file/tree/archive SHA，拒绝未声明文件、路径穿越、link/reparse/special file、配额超限及许可证篡改；主应用 `/api/about` 与侧栏 **About & Source** 显示 MIT/AGPL 分离、无担保声明和 License/Notices/SBOM/manifest/源码下载，缺少源码挂载时 fail closed。该包最初以 `runtime_ready=false` scaffold 建立，现已随真实 OCI Gate 完成同步扩展为可验证运行时源码；Worker wheel 实构建、合规资产、Source Offer 与 Browser 下载均有回归
  - [x] 固定 Parser 依赖/模型并实现真实 adapter source Gate：Linux/amd64 CPython 3.12 固定 PyMuPDF4LLM/PyMuPDF/PyMuPDF Layout `1.28.2`（AGPL 路径）、Docling Slim `2.119.0`、Core `2.92.0`、Parse `7.15.0`、IBM Models `3.13.2`、RapidOCR `3.9.2`，Torch/TorchVision 使用官方 `2.13.0+cpu/0.28.0+cpu` 且无 CUDA 依赖；`runtime-manifest.json` 固定关键 wheel/source identity、Heron/TableFormer/PP-OCRv6 逐文件 SHA/size/license。实现 hash-pinned routing/quality config、稳定原件 SHA、PDF 主动内容 gate、PyMuPDF 预检/三模式路由、fast、内嵌图片提取、Docling standard/ocr 双 Converter、模型启动校验与禁止 legacy fallback；adapter/source Gate 后续已由真实 OCI Gate 验证
  - [x] 构建断外网持久 OCI Worker，并完成 fast/accurate/auto fallback、运行中取消与恢复、SHA/配额/合规门禁和代表性 PDF 实机 smoke（2026-08-26，Docker 29.7.2，Linux/amd64）
    - [x] 实现主应用零 Parser 依赖的 `PersistentOciParserProvider` 与稳定 file queue；原始 PDF/请求先完整落盘再原子发布，状态、receipt、artifact 均按 Contract v2、大小和 SHA 复核
    - [x] 实现持久 supervisor + 可重启 parser child：启动期复用 Fast/Docling Converter，单并发 claim，cancel/timeout 硬终止 child 后重建，崩溃与重启遗留 running Job 安全终态化
    - [x] 实现 fast/accurate/auto/单次原始 PDF fallback、质量拒绝、确定性 Artifact v2、配额、source/artifact 篡改检测；离线真实队列/故障矩阵 8 passed
    - [x] 固定 digest 基础镜像、88 包 `uv.lock`、87 条跨平台 marker 后 Linux resolution、`pip --require-hashes`、PyPI + PyTorch CPU 索引、构建期模型逐文件 hash/size 校验，以及无网/只读/非 root/no-cap/no-new-privileges Compose
    - [x] 将 OCI build、锁、队列/监督器和 hash-pinned AGPL 上游源码物化脚本加入 35 文件确定性 Source Offer；每次镜像构建把三项 Artifex 源码归档及 canonical evidence manifest 放入镜像，并提供不输出正文的多语料实机 smoke CLI；本机真实物化 3/3 成功（PyMuPDF 87,903,557 bytes、PyMuPDF Layout 44,947,301 bytes、PyMuPDF4LLM 2,091,728 bytes），逐项 SHA-256 与 `runtime-manifest.json` 一致
    - [x] 实际镜像复核三项 AGPL 上游源码 bundle、容器 notices 与 runtime manifest；运行容器确认 `network_mode=none`、只读 rootfs、UID 65532、drop-all capabilities、no-new-privileges 和 CPU/内存/PID 上限；真实 RFC digital fast、W-9 accurate、复杂论文 accurate、扫描件 OCR 及 auto fast-quality-rejected → 原始 PDF Docling 均成功，运行中取消在 0.141s 内收敛并恢复 healthy，`runtime_ready=true`；最终 exchange 固定到源码树外 `.test-tmp`，smoke CLI 对源码树内 exchange/output fail fast 并有回归，避免运行态文件破坏精确 Source Offer
  - [x] 前端支持 Space、Source、解析状态和 Raw artifact 安全浏览；未复活 Chunk 检索 UI
- [x] **阶段 4–6：按固定顺序完成页面中心主链路**
  1. [x] **Agent 总结**：schema v3 新增 immutable `wiki_source_summaries` Summary Draft；tool-free core Agent 分页读取当前 selected Raw page Markdown，严格 JSON/页码引用校验，记录 source/parse revision/selection version、原件与 parsed/manifest SHA、prompt revision、实际 provider/model 及正文 SHA；大文档分批后再由同一模型合并，重解析并发 CAS 失败，非法输出只失败 Job，不改 Raw/Page/FTS/Graph；提供生成、列表、读取 REST API。Wiki 全量 `177 passed, 4 skipped`，新增后定向 `36 passed, 1 skipped`，Ruff PASS、strict Mypy 14 Wiki/App source files / 0 issues
  2. [x] **Wiki 入口页**：schema v4 新增 immutable `wiki_page_proposals`；只消费仍绑定当前 selected parse revision 的 Summary Draft，以确定性模板生成一篇 entry proposal，转义 Markdown/HTML 主动结构、生成稳定 Unicode slug 与别名、保存可信来源 locator 和正文 SHA；重复请求幂等，重解析后拒绝旧 Draft，只完成 `synthesize_entry_page` Job，不写正式 Page/Revision/FTS/Graph。生成/列表/读取 REST 已接入；Wiki 全量 `180 passed, 4 skipped`，Ruff PASS、strict Mypy 15 Wiki/App source files / 0 issues
  3. [x] **主题子页面**：schema v5 新增 `synthesize_topic_pages` Job；严格要求入口 proposal 已存在，从同一 Summary Draft 的 topic candidates 确定性生成零到多个 topic proposals，固定 ordinal/父入口/稳定 slug，只合并页码相交的关键点并保存可信 locator；同一 Job 在单事务中全量插入，重复请求幂等，仍不写正式 Page/Revision/FTS/Graph。REST 已接入；Wiki 全量 `183 passed, 4 skipped`，定向 `42 passed, 1 skipped`，Ruff PASS、strict Mypy 15 Wiki/App source files / 0 issues
  4. [x] **Change Set diff**：schema v6 将 Change Set 可信绑定 `source_summary_id`；按 entry→topic ordinal 稳定顺序把全部 proposals 原子冻结为一个 `awaiting_approval` Change Set，page-create item 同时保存 canonical payload JSON、来源 locator、正文 SHA 与确定性 unified diff；重复请求幂等，slug/来源/selected revision/graph base 冲突 fail closed，不提供发布入口。REST 已接入；Wiki 全量 `185 passed, 4 skipped`，定向 `44 passed, 1 skipped`，Ruff PASS、strict Mypy 16 Wiki/App source files / 0 issues
  5. [x] **用户批准**：实现 approve/reject 单入口与幂等决策；approve 在 Space 写锁/`BEGIN IMMEDIATE` 中重验 graph revision、selected parse revision、Summary/Proposal/payload/diff/slug，原子创建全部 Page、immutable Revision、PageSource，回填 Change Item target、推进 graph revision 并发布 Change Set；任一前提变化持久化整体 `stale` 且零部分写入，reject 零发布。commit 后按 DB current revision 原子刷新 `pages/*.md` 与 `space.json`，启动可修复缺失镜像；Page/Revision/decision REST 已接入。Wiki 全量 `188 passed, 4 skipped`，定向 `47 passed, 1 skipped`，Ruff PASS、strict Mypy 16 Wiki/App source files / 0 issues
  6. [x] **页面 FTS5**：schema v7 新增独立 `wiki_pages_fts`（unicode61）；索引插入与 Change Set approve 同事务，只投影 active Page 的 current approved Revision，启动可从规范 Page/Revision 全量重建；Raw、Summary、Proposal、未批准 Change Set、历史 Revision 和 Chunk 均无写入路径。查询由服务端固定 Space，用户文本逐 term 编译为 quoted literal phrase，限制长度/limit，返回 page/revision/snippet/BM25 rank；REST 已接入。Wiki 全量 `190 passed, 4 skipped`，定向 `49 passed, 1 skipped`，Ruff PASS、strict Mypy 17 Wiki/App source files / 0 issues
  7. [x] **页面知识图谱**：主题页→入口页的 `part_of` 作为 edge-add item 与所有 page-create item 进入同一 Change Set，批准事务在任何写入前重验 canonical payload/diff、固定关系类型、同 Space 端点、去重、自环、`related_to` 规范方向和 `part_of` 无环性，再原子写入 Page/Revision/PageSource/FTS/Edge 并统一推进 graph revision；`derived_from` 不进入 `wiki_edges`，只从可信 `wiki_page_sources` 动态投影为 system-managed 边。提供稳定全图与单页邻接 REST 快照，草稿/拒绝/stale 内容均不可见。Wiki 全量 `191 passed, 4 skipped`，图谱/API 定向 `22 passed`，全量 Ruff PASS、strict Mypy 17 Wiki/App source files / 0 issues
  8. [x] **Knowledge Agent 对话**：实现每个 Space 多个独立 Knowledge Conversation，并以持久 Session 作为唯一可信运行时绑定；创建对话采用补偿式 saga 初始化 Session/provider/workspace/Wiki binding，Session 删除自动归档对应对话。Prompt 执行、regenerate 和 context budget 均自动识别 Knowledge 模式，复用既有 Agent loop、text/thinking/tool 流事件、lane、消息树与 compaction，同时拒绝请求伪造 Conversation、Workspace 附件和用户可选 Skill。运行时临时替换为固定内置 Knowledge Prompt/Skill policy 与 10 个 `wiki_*` 白名单工具，`finally` 恢复普通 Agent registry；读工具只访问可信 Space 的已批准 Page/FTS/Graph 和受限 Raw artifact。Agent 对页面创建、修改、删除及固定页面关系增删只能生成 conversation-bound `awaiting_approval` Change Set 和完整 diff；批准事务重新校验 graph/page revision、payload/diff、Space、关系方向/去重/无环后才原子更新 Page/Revision/FTS/Graph/镜像，竞态整体 stale；`derived_from` 无 proposal/写入路径。Conversation REST 已接入；真实 tool-call loop、跨 Space/Session 隔离、审批前不可见、批准后发布、stale、删除镜像和禁止伪造来源边均有回归。Wiki 全量 `195 passed, 4 skipped`，Knowledge/Store/API 定向 `54 passed, 1 skipped`，全量 Ruff PASS、strict Mypy 19 Wiki/App source files / 0 issues
- [x] **阶段 7：实现独立 Knowledge 页面并完成退役验收**：新增 `/knowledge` 独立路由与 Pages/Sources/Graph/Changes/Conversations 五视图，支持来源/解析产物、页面/revision、FTS5、图谱、统一 diff 审批及多 Knowledge 对话；删除旧 Library/Document/Chunk 前端，开发与 E2E 产品组合不再启动旧 DB/ingestion/indexing/`search_knowledge`/REST，旧 Backend 仅保留显式兼容入口且默认测试只验证其不可被产品误启动。当前门禁结果记录于 [`STATUS.md`](STATUS.md)
- [x] **阶段 8：实现 Source retention 与安全 Raw 清理**：新增 Source 删除 API 与前端二次确认入口；请求后立即进入不可逆 `deleting` 并禁止原件/Artifact 内容读取，默认保留 7 天后由独立 retention 协程清理该 Source 的完整 Raw bundle。清理采用同目录原子改名和拒绝 link/reparse/special file 的不跟随遍历，崩溃遗留 staging 可重试；Source、解析、Summary 与审批审计行永久保留。active Page 的 `derived_from`、运行中 Job 或待审批 Change Set 会以 409 阻断删除；不修改 schema。新增/修改 2 个 API 行为测试，定向 2/2、既有 Worker 4/4、Wiki 视图 4/4 通过；Ruff、定向 strict Mypy、Frontend typecheck/ESLint 通过
- [x] **阶段 9：补齐 Wiki Space 生命周期**：新增 `active ↔ archived` API 和前端 Archive/Restore，归档后不再作为默认活动 Space；新增 Space 删除 API 与明确二次确认。删除仍是审计保留的 `deleting` 终态，只有全部 Source 已 deleting、Page 已 deleted、Conversation 已 archived，且不存在 queued/running Job 或 draft/awaiting-approval Change Set 时才允许，任一未收敛项统一返回 `space_in_use` 409；数据库及 Space 目录不物理删除。新增/修改 2 个 API 行为测试，定向 2/2、既有 Store 状态机 1/1 通过；Ruff、定向 strict Mypy、Frontend typecheck/ESLint 与既有 Wiki Workspace 测试通过
- [x] **阶段 10：收紧 archived Space 只读语义**：Archive 事务拒绝 queued/running Job、active Conversation 和 draft/awaiting-approval Change Set；上传、解析 claim、Summary/Proposal Job、新对话及 Source/Knowledge Change Set 均在写事务内重验 Space 仍为 active，避免“先检查、后归档”的竞态。读取、页面搜索、图谱和审计浏览保持可用；Restore、对话归档、Change Set reject、Source/Space 删除收敛路径保持可用。统一返回 `space_read_only` 409。未新增测试，仅在现有 Space 生命周期测试中增加代表性上传阻断断言；相关 API/Summary/Worker 6/6、Ruff 与定向 strict Mypy 通过

## P0 — 托管 Coding Sandbox 与安全发布

目标架构：Sandbox 自有实现固定为顶层独立包 `src/coding_sandbox`，不得反向导入
`pi_agent_core_py`；pi-agent 只保留 Web、凭证与生命周期薄适配。控制端和真实工作区
先保留在本机，Agent 通过 provider-neutral `SandboxBackend` 在 E2B 云 Sandbox 内编辑和
验证代码；验证通过后只下载不可变制品，由本机 Publisher 做 SHA 冲突检查和事务发布。
Modal 与 Local Docker 作为后续兼容后端，最终用户不需要安装 Python、Node、Conda 或 Docker。

- [x] **0. 将 Coding Sandbox 迁为 `src/coding_sandbox` 独立包**（完成：核心、快照、E2B 与通用管理层全部归顶层包；`pi_agent_core_py` 仅保留 FastAPI/凭证/生命周期组合薄适配；源码 AST 与隔离进程双重门禁确保独立导入不会加载 pi-agent；wheel 包含 13 个 Sandbox 文件且无旧嵌套包；全量 Ruff PASS、strict Mypy 130 files / 0 issues、Sandbox 64 passed / 1 Windows symlink capability skipped、Web/凭证生命周期 70 passed）

- [x] **1. 建立 provider-neutral Sandbox 基础层**（完成：新增默认关闭/断网的配置模型、secret-reference-only provider 配置、不可变 handle/status/command/transfer DTO、固定安全错误分类、异步 `SandboxBackend` Protocol 和完全离线 Fake Backend；命令仅接受 argv，Fake 强制资源/摘要边界与幂等销毁；全量 Ruff PASS、strict Mypy 120 files / 0 issues、离线契约 21 passed）
- [x] **2. 建立项目快照与输入边界**（完成：生成确定性 gzip/tar 与自校验 `base-manifest.json`；默认排除 VCS、`.env`、密钥、依赖、缓存及构建目录；源文件在扫描/写入间做文件身份与 SHA-256 复核；拒绝归档落入项目、绝对/穿越/重复/大小写冲突路径、symlink/junction/reparse/special file、文件数/单文件/总量超限和 archive bomb；归档不解压即逐项限额并重算内容摘要，篡改必失败；Ruff PASS、strict Mypy 8 files / 0 issues，Sandbox 离线回归 31 passed / 1 Windows symlink capability skipped）
- [x] **3. 实现 E2B Backend**（完成：新增 lazy/optional 官方 `AsyncSandbox` SDK Driver 和可注入离线 Driver seam；完整实现 create/attach/non-resuming status/upload/remote rehash/download/argv exec/destroy，外部 sandbox ID 与防串接 metadata 进入 secret-free handle；默认禁公网入站及出站、allowlist 显式映射，固定 `/workspace`、on-timeout kill、硬命令/传输/输出上限、取消/超时 kill、创建失败清理与可重试 destroy；非零 exit 是正常结果，paused 状态如实公开，所有异常转固定安全错误；新增 `sandbox-e2b` 可选依赖并锁定 E2B 2.43.0；全量 Ruff PASS、strict Mypy 122 files / 0 issues、Sandbox 离线回归 48 passed / 1 Windows symlink capability skipped）
- [x] **4. 增加 E2B 管理配置与连接测试**（完成）：新增 secret-free revision/CAS SQLite 配置、CredentialService `credential_id` 窄引用、enabled/provider/template/资源/网络/配额模型，以及 localhost + UI header + Origin + 32 KiB body limit 保护的 GET/PUT/Test Connection API；连接测试强制断网、短生命周期、固定 `python3 --version` 并始终 destroy；API Key 不进入响应、配置表或 Sandbox metadata。本机安装锁定的 E2B 2.43.0，并新增隐藏双输入、官方 `e2b_` 十六进制格式门禁、Windows Keyring 往返一致性、CAS 配置、失败回滚/密钥清理和官方 SDK 独立认证探针的 `scripts/configure_e2b_sandbox.py`；无效凭证两次均安全返回 `authentication_failed` 且回滚无残留，修正为有效完整 API Key 后首次真实 `base` 探测 3844 ms 成功，持久化 Keyring Credential 的独立 `--test-only` 复验 4265 ms 成功，均确认 `python3` 可用并完成 Sandbox 销毁。当前门禁：全量 Ruff PASS、strict Mypy 130 files / 0 issues、E2B SDK 安装后定向回归 30 passed、配置/诊断 CLI 13 passed、Sandbox 回归 64 passed / 1 Windows symlink capability skipped、Web/凭证生命周期回归 70 passed、wheel 独立包边界验证 PASS、`uv lock --check` PASS
- [x] **5. 实现 Sandbox 文件与命令工具**（完成）：在独立 `coding_sandbox` 包内新增 request-scoped `SandboxOperation`、`ContextVar` 绑定和强制 finally destroy，pi-agent 仅提供 8 个 sequential `AgentTool` 薄适配：`coding_list_files`、`coding_read_file`、`coding_search`、`coding_write_file`、`coding_apply_patch`、`coding_delete_file`、`coding_run`、`coding_diff`。文件 API 只接受已规范化相对 POSIX 路径，拒绝绝对/父级/反斜杠/control/symlink 逃逸；命令只接受有界 argv/cwd/timeout 并转发协作取消与 stdout/stderr 增量；写入走本机 0600 staging、SHA-256 upload 与远端 atomic replace，read/write 响应重算 size/hash；统一 diff 禁止 rename/duplicate/traversal 并在全部 hunk 预检后修改；operation 构造、业务异常和取消路径均清理 Sandbox，错误仅公开固定安全码。新增 Keyring-only `scripts/smoke_e2b_coding_tools.py`，真实 `base` E2B 逐项执行 8/8 工具成功并确认销毁（10719 ms）；新增离线契约 25 passed，Sandbox 定向回归 101 passed / 1 Windows symlink capability skipped；全量 Ruff PASS、strict Mypy 135 files / 0 issues、仓库 CI 3802 passed / 4 skipped / 15 deselected、coverage 83.78%（门禁 75%）
- [x] **6. 实现固定验证门禁**（完成）：从重新核验 archive size/SHA/manifest 的不可变项目 snapshot 读取并严格解析 64 KiB 上限 `.pi-agent/sandbox.toml` v1，仅允许 1–32 个 ID 唯一的 argv/cwd/timeout required checks，固定 source SHA 与 canonical plan SHA；新增无模型命令参数的 `coding_validate`，服务端串行执行固定检查并记录 argv、exit code、termination reason、duration、定长截断 stdout/stderr 及各自 SHA、验证前后完整工作区摘要。所有 run/write/patch/delete 均提升 revision 并使旧证据失效，消费证据前再次扫描远端工作区与配置 SHA，可识别后台及带外修改；验证失败、取消、Sandbox 丢失和未执行检查均生成不可伪造的内部 evidence 并关闭门禁。真实 `base` E2B 已执行 9 个工具、验证后修改、旧证据拒绝、重新验证及销毁（19233 ms）；固定门禁离线用例 26 项，Sandbox 定向回归 126 passed / 1 Windows symlink capability skipped；全量 Ruff PASS、strict Mypy 136 files / 0 issues、仓库 CI 3828 passed / 4 skipped / 15 deselected、coverage 83.77%（门禁 75%）
- [x] **7. 实现不可变输出制品**（完成）：`freeze_output_artifact()` 仅消费 operation 内部保存且再次核验的成功 validation evidence，导出开始即进入 fail-closed frozen 状态，永久拒绝 run/validate/write/patch/delete；固定远端 helper 通过逐层 `O_NOFOLLOW` 描述符扫描，在工作区外生成确定性 tar，完整导出自校验 manifest、binary diff、changed-file 原始字节、deleted files 与 canonical validation evidence。Provider 按预期 SHA 下载后，服务端再次计算 archive size/SHA、逐 member 校验路径/类型/配额/内容 hash/二进制分类和证据关联，再复核远端配置 SHA 与完整 workspace SHA；通过后写入 `artifacts/<sha256>.tar` 内容寻址只读文件，并以注入式、secret-free receipt 的 HMAC-SHA256 server signer 绑定 archive SHA、manifest SHA、key ID 与签名时间。新增篡改 payload、额外 member、symlink、伪造签名、缺 signer/baseline、验证过期、带外修改和冻结状态回归 7 项；真实 `base` E2B 已确认 9 个工具、验证重跑、制品冻结、签名及销毁（22047 ms）；Sandbox 定向回归 106 passed / 1 Windows symlink capability skipped，全量 Ruff PASS、strict Mypy 138 files / 0 issues、仓库 CI 3835 passed / 4 skipped / 15 deselected、coverage 83.78%（门禁 75%）
- [x] **8. 实现本机事务 Publisher**（完成）：新增独立 `LocalTransactionalPublisher`，只消费可信 `ProjectSnapshot` 与已签名 `SandboxOutputArtifact`；跨进程项目锁内重新核验 baseline archive、artifact/signature 和完整本机 manifest，按 before/after size/SHA 生成排序 intent。替换/删除预制完整备份，新增使用 no-clobber hard-link，替换使用同目录临时文件 + `os.replace`，每次项目效果前后写 canonical、SHA hash-chained、append/flush/fsync JSONL；无 durable commit 的异常与启动事务逆序回滚，commit 后只幂等清理，支持 torn-tail 保全/截断、幂等 artifact 重试、取消等待收敛及 post-crash 用户修改冲突保护。state root 强制位于项目外，逐层拒绝 symlink/junction/reparse，Windows 状态路径使用 128-bit 目录 key；Sandbox 从不获得真实项目/state 路径。新增提交/冲突/篡改/回滚/崩溃恢复/断尾/并发锁/recovery conflict/hash-chain/reparse 回归；Sandbox 定向 146 passed / 2 capability skipped；全量 Ruff PASS、strict Mypy 139 files / 0 issues、后端 CI 3846 passed / 8 skipped / 12 deselected、coverage 83.77%；真实 `base` E2B 已完成 9 工具、验证、冻结/签名、本机 Publisher commit、幂等 retry 与 destroy（21204 ms）
- [x] **9. 接入 Web 生命周期与 UI**（完成）：新增持久化 operation/event 状态机与会话唯一活跃操作约束，Web API 覆盖创建、恢复、事件回放、Diff、固定验证、冻结、显式审批发布、取消与丢弃；统一 WS 实时事件和有界 SQLite 事件重放。后端启动把未完成操作收敛为 `interrupted`，浏览器刷新只恢复状态/日志，绝不重放模型、命令或发布动作；Agent 的 9 个代码/验证工具按当前 Session 绑定同一 Sandbox workspace。前端新增 Sandbox Modal，展示状态、验证 stdout/stderr、patch 和日志，发布前必须再次勾选确认。门禁：Ruff PASS、strict Mypy 140 files / 0 issues、生命周期/API 6 passed、仓库 CI 3849 passed / 8 skipped / 12 deselected、83.75% coverage，前端 ESLint/typecheck/build PASS、Vitest 402/402 passed
- [x] **10. 完成真实 E2B、完整 CI 与安全验收**（完成）：离线 Fake、E2B adapter、路径/命令/网络/凭证边界、故障注入、制品篡改、Publisher 冲突/回滚/崩溃恢复和退役 Provider 空字段兼容迁移等安全矩阵 151 passed / 2 Windows capability skipped；新增验证失败不得冻结/发布且真实工作区字节级不变的生命周期回归。真实 `base` E2B smoke 覆盖 9 个工具、首次验证成功、故意删改后的固定验证失败与冻结拒绝、恢复重验、制品冻结/签名、Publisher 本机冲突且工作区字节级不变、成功 commit、幂等 retry 和 Sandbox destroy（20577 ms，未输出凭证）；完整 Playwright 47/47 passed（新增刷新恢复不重放、验证失败 UI）；全量 Ruff PASS、strict Mypy 140 files / 0 issues、Backend CI 3851 passed / 8 skipped / 12 deselected、coverage 83.73%，前端 ESLint PASS、Vitest 402/402 passed，`uv lock --check --offline` PASS
- [ ] **11. 增加第二后端**（暂缓）：保留 provider-neutral `SandboxBackend`、`SandboxBackendName="modal"` 与 Modal secret-reference 配置契约作为未来扩展接口；当前产品只接入并验收 E2B，Modal SDK Backend、管理配置、脚本、依赖和真实连接均不进入当前实现。Local Docker 同样留作需要代码不离开设备的后续显式选项，不作为零配置默认路径

## pi-agent Core 对齐修复顺序

### 1. P0 — 语义正确性

- [x] 并行工具批次改为两阶段执行：按源序串行完成 before hook、权限策略、人工审批和参数校验，再并行执行已放行工具
- [x] `max_turns` 终止信息写回最终 `AgentEndEvent`、`Agent.state.messages` 和持久化消息
- [x] `Agent.continue_()` 拒绝从 assistant 尾消息直接继续
- [x] 禁止 before/after tool hook 改写 tool-call ID，保证 ToolCall/ToolResult 协议配对
- [x] Harness 请求异常完成后恢复 `idle`，保留 `last_error` 和 error snapshot 供诊断
- [x] 为以上语义增加回归测试，并复跑 Agent Core 测试集（179 passed）

### 2. P1 — Agent 控制面兼容

- [x] 实现独立的 steering / follow-up 队列及 `all` / `one-at-a-time` 消费模式（默认均为 `one-at-a-time`；steering 优先，follow-up 仅在 Agent 原本将结束时消费；新增 9 项队列契约测试）
- [x] 活跃请求期间拒绝普通 `prompt()` / `continue_()`，错误信息明确引导调用已提供的 steer / follow-up 入口（覆盖 queued-before-worker、running Agent 与 running Harness 三个竞态窗口）
- [x] 增加全局 `tool_execution` 配置，并保留逐工具 `execution_mode` 覆盖（全局 `sequential` 强制整批串行；全局 `parallel` 下任一逐工具 `sequential` 可收紧整批；新增 8 项配置、透传与运行时校验测试，Agent/loop/Harness 相关回归 163 passed）

### 3. P1 — 消息、流与模型状态

- [x] 扩展 thinking/reasoning 内容块：保留正文、provider signature 与 redacted payload；打通 OpenAI/Anthropic 增量、Agent 消息、上下文回放、预算估算和持久化（相关回归 289 passed）
- [ ] 扩展图片内容块，不再把图片统一降级为 `image_unsupported`（按当前决定暂缓）
- [x] 增加细粒度 text/thinking/tool-call start/delta/end 流事件：Provider 统一输出带 `content_index` 的完整块生命周期，Agent 维护 partial message 并兼容旧 delta-only / whole-tool-call 流；工具仅在 `toolcall_end` 后进入执行（定向回归 62 passed；全量非网络回归 3655 passed）
- [x] 补齐 model、thinking level、streaming message、pending tool calls 等公开 Agent 状态（`AgentState`、动态 client model 投影、Web JSON 契约与 reset/error 生命周期均已覆盖；新增 4 项核心契约测试，完整后端 3662 passed）
- [x] 对齐 ToolResult 的 usage 和动态 added-tool metadata（`ToolResult` → message / event / LLM boundary / Snapshot / Session / SQLite / Web JSON 全链路保留；`added_tool_names` 仅标记 `Context.tools` 的 provider 加载点，不注册工具且 after hook 不可伪造；定向回归 173 passed，完整后端 3665 passed）
- [ ] 扩展 ToolResult 图片内容（随图片内容块继续延期，不进入下一项）

### 4. P2 — Session、Compaction 与持久化

- [x] 评估并迁移 append-only 会话树或 lane-based Session；支持 branch、fork、label 和 active leaf（完成：独立 immutable parent-entry tree + 命名 lane leaf；旧 `messages` 保留为 active lane 兼容投影；旧线性库幂等回填 `main`；Regenerate 创建 sibling branch 并兼容 trailing ToolResult suffix；Core/Web/前端 API、设计文档与重启回归齐备；提交 `43c1d0a`；完整后端 3681 passed / 83.84%，前端 399/399）
- [x] 引入 durable operation/recovery，避免整份 JSON 覆盖和非原子发布（完成：SQLite lane operation 使用 append-only intent/effect/finish records；Checkpointer 固定 immutable source leaf/hash，Memory 以 immutable generation + 原子 metadata pointer 发布，lane reset 与 completed 同事务；启动/同进程重试可无 LLM 前滚，leaf 变化时保留消息并标记 conflict；旧 `JsonFileSessionStore` 迁移为 append-only journal + torn-tail recovery；完整后端 3692 passed / 83.70%，前端 399/399；提交 `8a5c569`）
- [x] 将 compaction 默认边界改为完整 turn，并补齐 token/window、前缀摘要和重试语义（完成：默认及 token 目标均只保留完整 user→assistant/tool-result turn，最新超预算 turn 不拆；Core/Web 记录 canonical preflight 的压缩前后 token、context window 与 output reserve；摘要以 pi-compatible `<summary>` envelope 注入并显式折叠 previous summary；瞬时网络/限流错误按不可变输入重试，鉴权/协议/类型/取消不重试，失败不改源消息；提交 `0c72679`；完整后端 3698 passed / 83.76%，前端 399/399，定向 Playwright 1/1）

## P0 — Release 与文档卫生

- [x] 统一版本元数据为 `0.0.28`：Python `__version__`、workspace FastAPI、Auth gateway FastAPI、前端 package 与 lockfile；回归测试防止再次漂移
- [x] 在仓库根补齐标准 MIT `LICENSE`，`pyproject.toml` 直接引用该文件，并验证 wheel 同时携带 `License-File: LICENSE` 与许可证正文
- [x] 在发布前复跑当前 HEAD 的完整 Playwright E2E：45/45 passed，单 worker、`CI=1`、独立端口 8012，production build 已由 posttest 恢复
- [x] 通过固定 secret-safe 脚本复跑真实网络 smoke：DDGS 1 项 + GLM 2 项，3/3 passed，未输出 API Key
- [x] 整理并提交 LLM Wiki 阶段 3–10 发布候选（完成：清除并忽略 `.t/` 测试产物；设计/状态同步到阶段 10；Ruff、strict Mypy 168 files、Frontend ESLint/typecheck/build 通过；Backend 2091 passed / 7 skipped / 9 deselected，Frontend 180/180，Wiki 定向 Backend 67 passed / 1 skipped、Frontend 16/16、Playwright 1/1；修复测试精简后保留安全用例引用已删除 fixture 的问题）
- [x] 创建 `0.0.28` annotated release tag（完成：指向通过发布候选回归并同步发布状态的提交）
- [ ] 如需 push，先配置 Git remote；当前仓库没有 remote，push 仍需用户单独授权
- [ ] 评估本地初始账号 `admin / 123456` 的改密入口；在此之前继续保持 localhost-only

## P1 — 可靠性与维护

- [x] 清理全量 Backend 的已知 warning（完成：Starlette 1.3+ TestClient 显式使用 HTTPX2；替换废弃 HTTP 422 常量与 raw-body API；移除同步测试误用的 asyncio module marker；修复测试文件句柄、SQLite 启动失败与 uvicorn pipe 资源泄漏；Backend 全量在 `-W error` 下 3852 passed / 5 skipped / 15 deselected，0 warning）
- [x] 固定 pytest 可写状态目录（完成：`cache_dir=.pytest-cache-workspace`、`--basetemp=.pytest-tmp`，两者均已加入 `.gitignore`；不再访问 ACL 异常的旧 `.pytest_cache`，全量 Backend 已验证长时可写）
- [x] 为 ToolResult/UTF-8 修复增加 Browser E2E（完成：持久化 `toolResult` 恢复为 MCP/通用工具卡，保留 call ID、tool name、error/details 与 UTF-8 内容；新增确定性两轮 DDGS 中文场景，验证 user/tool/assistant 跨轮顺序及整页刷新恢复；Playwright 全量 48/48，0 retry / 0 failure）
- [x] 清理 Frontend 测试/构建 warning（完成：Modal 显式接管 Teleport fallthrough attrs，外部 `data-testid` 转发到 overlay 且保留 dialog 稳定 ID；主入口、Session API 与 Sidebar 统一静态导入，消除 Vite mixed-import 提示；Playwright global setup 解除 `NO_COLOR`/`FORCE_COLOR` 冲突；同时将 event-dedup 人工注入收敛为单一浏览器任务。Vitest 404/404、Playwright 48/48，三类 warning 与 retry 均为 0）
- [x] 实现旧消息中 `U+FFFD` 的检测与标记（完成：Web 序列化时递归只读扫描 text/thinking/tool result/details/args 等 JSON 字段，返回 replacement/value 计数、受限 RFC 6901 路径和 Session 汇总；明确标记 `suspected=true`、`auto_repairable=false`，不写回 SQLite、不猜测原字符、不在元数据泄露正文片段；前端在消息与工具卡展示“疑似编码损坏”及受影响字段，刷新后保持；新增 serializer/API/Store/组件与真实 Browser E2E，Backend 全量 3856 passed / 5 skipped / 15 deselected、Vitest 405/405、Playwright 49/49）
- [x] 为 Keyring 启动预检增加 Windows 实机 smoke 文档（完成：新增同账号/同解释器/交互式 Windows 会话前置条件，独立随机非用户值 write/read/delete/post-delete 验证、开发启动器 listen-before-probe 验收、cleanup failure-only 补救、自动化伴随回归、失败分类与最小非敏感证据模板；明确禁止真实 API Key、`keyring get` 输出、环境转储和把 `memory` 降级误记为 Keyring 通过；README 已链接操作入口）

## P2 — 可选产品迭代

- [ ] **P2-D Session organization**：Session 搜索、收藏、归档
- [ ] 接入 Provider 官方 tokenizer；保留当前 estimator 作为安全 fallback
- [ ] 自动 compaction 策略；明确触发时机、失败回滚和请求并发边界
- [ ] 可选 LLM compaction 摘要器；与 `/checkpointer` 的 Session Memory 语义保持区分
- [ ] 跨后端重启的 request/approval 持久化方案；当前仅浏览器刷新恢复
- [ ] Human Approval 的持久规则/永久授权模型；需要新的安全与审计设计
- [ ] MCP HTTP transport；当前仅 stdio transport 可用

## 明确延期 / Out of scope

- [ ] 扩展图片理解与富版面 enrichment：Docling OCR preset 基线已通过双 Parser Gate；更多 OCR 语言/语料质量、公式、代码与 VLM enrichment 仍默认关闭，启用前需独立版本/hash/许可证/资源 Gate
- [ ] Local Docker/Git Sandbox provider integration；当前 Plan Mode 已复用 Managed Sandbox 完成，新的本地 Provider 仍需独立安全与隔离设计
- [ ] RBAC、OAuth、企业级多租户、TLS 公网部署、横向扩展
- [ ] 向量数据库、embedding、hybrid retrieval 与 Chunk RAG；LLM Wiki 仅保留已批准页面的 SQLite FTS5
- [ ] 自动 provider fallback、模型负载均衡、长期后台任务调度

## 已完成基线索引

| 能力 | 最终状态/提交 |
|---|---|
| P2-R Knowledge/RAG + Web Manager | ✅ `7faf635` / `6527be5`；历史基线，已由 LLM Wiki 产品方向取代 |
| Auth + Session Folder + Checkpointer + P0 Runtime | ✅ `b529bbc` |
| P2-A Session reload recovery | ✅ `f30da56` |
| P2-B Approval + P2-C Context Budget/Compaction | ✅ `924b047` |
| 三类意图路由 | ✅ `46c8b3e` |
| Planner–Executor–Verifier Plan Mode | ✅ `698b7d4` |
| B7 SQLite cleanup | ✅ `688cf08` |
| Persistent Keyring preflight | ✅ `ada31fc` |
| ToolResult ordering + MCP UTF-8 | ✅ `e7bf8f3` |
| Agent semantics + controls + stream lifecycle | ✅ `c214d28` |
| ToolResult usage + deferred-tool metadata | ✅ `b6baea8` |
| Complete-turn Compaction semantics | ✅ `0c72679` |

当前代码基线的验证结果见 [`STATUS.md`](STATUS.md#2026-08-23-当前验证基线)。
