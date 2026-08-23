# Current TODO

> 校准日期：**2026-08-23**。本文件只保留尚未完成或明确延期的事项；已完成阶段不再复制数百行历史记录，统一由 [`STATUS.md`](STATUS.md)、[`CHANGELOG.md`](CHANGELOG.md) 和 `docs/validation/` 追溯。

## 当前收敛执行顺序

- [x] **1. 修复全量 Ruff / strict Mypy，使仓库自身 CI 静态检查通过**（完成：Ruff 0 项；Mypy 114 files / 0 issues；后端 CI 3655 passed、6 skipped、12 deselected、coverage 83.73%；前端 lint / typecheck / build 通过；Python CI job timeout 由 10 分钟调整为 30 分钟以容纳完整门禁）
- [x] **2. 整理并提交当前工作区改动，同时更新 `STATUS.md` 到最新验证基线**（完成：运行时/测试/CI 提交 `c214d28`；81 个工作区路径完成分类与敏感信息审计，状态文档记录真实门禁结果）
- [x] **3. 统一 Python、FastAPI/Auth、前端与 release 版本号，并补齐仓库根 `LICENSE`**（完成：统一为未打 tag 的 `0.0.28`；两个 FastAPI 工厂直接复用 Python `__version__`；新增跨 Python/前端/lockfile/API 一致性测试；根 MIT `LICENSE` 已进入 wheel；提交 `8a6ff2e`）
- [x] **4. 复跑当前提交的完整 Playwright E2E 与最终 GLM 真实 smoke**（完成：修复 full-suite 的 Regenerate 完成等待、logout 共享 token 撤销与 Session 删除路由竞态，提交 `51ce3c7`；完整 Playwright 45/45 passed；固定 DDGS 1 项 + GLM 2 项真实 smoke 3/3 passed）
- [x] **5. 继续 pi-agent 对齐：补齐 model、thinking level、streaming message、pending tool calls 等公开 Agent 状态**（完成：新增 secret-free `AgentModelState`、完整 `ThinkingLevel`、`is_streaming` / `streaming_message` / `pending_tool_calls` / `error_message`；按消息与工具事件生命周期更新，request 结束、异常与 reset 统一清理；`/api/state` 与前端类型同步；提交 `848ae1d`）

## P0 — Session Workspace 一体化

完整架构、所有权边界、Sandbox 发布流和富文档转换约定见
[`docs/design/workspace-sandbox-integration.md`](docs/design/workspace-sandbox-integration.md)。

- [x] **阶段 1：统一 `WorkspaceStore` 与初始化 `Memory.md`**（完成）：以 `WorkspaceStore` 作为 Session 文件唯一规范事实源，`VirtualFileStore` 仅为同一实现的兼容别名；新旧 Session 均幂等拥有唯一根 `AGENT.md` 与 `Memory.md`，迁移保留已有正文/file id 并规范大小写等价旧路径与 purpose，两个固定根文件均禁止删除；Ruff PASS、strict Mypy 141 files / 0 issues、Workspace/文件/Auth/Checkpointer 定向 125 passed（`-W error`）、Backend CI 3857 passed / 8 skipped / 12 deselected、83.69% coverage
- [x] **阶段 2：代码与 Markdown 规则**（完成）：`WorkspaceStore` 按扩展名把 Agent 写入和用户上传的代码统一映射到惰性逻辑根 `scripts/`，安全相对目录自动约束在其下；新增普通 Markdown 精确创建、正文更新、移动/重命名和删除 API，固定根文件继续受保护；每个 Session 以隐藏 `.workspace.json` 持久化单调 revision，mutation 同时支持逐文件 SHA 与 Workspace revision 乐观锁，失败保持文件树/revision 不变，删除 tombstone 和状态 temp 可在重启时收敛；Agent `write_file`/`list_files` 与前端 API/types/store 返回 revision。全量 Ruff PASS、strict Mypy 141 files / 0 issues、阶段定向 Backend 116 passed、Frontend typecheck/lint 与 Vitest 405/405 passed
- [x] **阶段 3：右侧 Workspace 成果面板**（完成）：`AppShell` 改为 Sessions / Chat / Workspace 三栏，窄于 1050px 时右栏降级为带新成果提示的 drawer；右栏监听成功 `write_file` ToolResult 的 `file_id`/Workspace revision，立即刷新、选中并预览 Agent 生成的 `.md`、`.py` 等成果，整页刷新后从持久 ToolResult 恢复；Files 支持目录树、最新成果提示、用户上传（不自动附加到聊天）、Markdown 创建/安全预览/编辑、代码查看与下载，Sandbox / Changes 复用现有 operation、日志、验证、diff 和完整审批发布 Modal。Frontend typecheck/lint/build PASS、Vitest 409/409、Playwright 全量 52/52（含成果自动展示/刷新恢复、Markdown 编辑、代码上传、窄屏 drawer），0 retry / 0 failure
- [ ] **阶段 4：统一 Sandbox 快照与发布目标**（冻结）：从 `WorkspaceStore` 物化 E2B 快照，固定验证和用户确认后事务发布回同一 Workspace，保护根文件与系统路径并原子提升 revision；在 LLM Wiki 当前阶段完成前不进入实施
- [ ] **阶段 5：固定文档转换工作流**（冻结）：建立不可变原件、转换任务和 manifest，依次支持 PDF→Markdown、DOCX→Markdown、XLSX→摘要/CSV/schema；OCR 明确延期；不得与独立 Knowledge/LLM Wiki 的 Raw Source 流水线混用
- [ ] **阶段 6：完整验收**（冻结）：Backend/Frontend 静态检查和测试、Workspace Browser E2E、刷新/重启/并发冲突回归，以及真实 E2B Python 写入、验证和发布 smoke

## P0 — LLM Wiki（替代 Knowledge/RAG）

完整产品合同、目录与权限、Provider、Schema、Change Set、页面图谱、Knowledge Agent 和退役方案见
[`docs/design/llm-wiki.md`](docs/design/llm-wiki.md)。旧 P2-R Knowledge/RAG 文档只作为历史基线，
不再指导后续产品实现；Chunk 检索主链路废弃，FTS5 仅索引已批准 Wiki 页面的标题、别名和正文。

- [x] **阶段 0：冻结 LLM Wiki 产品合同**：确认先创建 Wiki Space；PDF/单文件 HTML MVP；本机独立 Marker Sidecar；PDF 只提取内嵌图片；`raw/` 对 Agent 强制只读；每个来源一篇入口页并允许主题子页面；页面级 FTS5；固定页面关系和系统 `derived_from`；每个 Space 多对话；所有页面/图谱修改进入同一 Change Set 一次审批；旧库结构不迁移
- [x] **阶段 1：重新执行 Marker Gate 并冻结 Parser Provider Contract**（完成）：审核基线固定 Marker `2.0.0`，分离 Apache-2.0 代码与修改版 OpenRAIL-M 模型许可证 Gate；发布实现固定为每任务本机 OCI 隔离容器、`fast_no_ocr`、任务期间断网，独立 venv 仅为降级开发模式；新增独立顶层 `wiki_parser` 的 immutable/extra-forbid DTO、异步 Protocol、固定安全错误、来源/制品 SHA 与配额/超时/取消/幂等销毁契约，以及完全离线确定性 Fake；主应用未引入 Marker/Torch/Surya/Transformers。Ruff PASS、strict Mypy 全仓 146 files / 0 issues、Provider/Fake 29 passed、Backend 3897 passed / 8 skipped / 12 deselected / 83.44%；Gate 见 [`docs/design/llm-wiki-marker-provider-gate.md`](docs/design/llm-wiki-marker-provider-gate.md)
- [x] **阶段 2：实现 WikiStore、`wiki.db` 与新目录结构**（完成）：新增独立 `pi_agent_core_py.web.wiki`，以三重 DB 身份/version gate、SQLite capability/integrity gate、STRICT/FK/CHECK/UNIQUE 建立 Space/Source/Artifact/Page/Revision/PageSource/Edge/ChangeSet/Item/Conversation/Job schema v1，明确不创建 Chunk/Chunk FTS；实现带跨连接 CAS/软删除状态机的 Space CRUD，固定 `spaces/{space_id}/{raw,pages}`、canonical `space.json`、全层 casefold/symlink/reparse/特殊文件防护、Raw no-clobber 与 Page atomic replace、启动镜像重建/orphan 报告/staging 隔离；显式 `retire` 在独占锁内先生成 SQLite 一致快照与逐文件 SHA 的只读 legacy backup，再清理旧固定路径，默认 `preserve` 且未触碰真实用户旧库。设计见 [`docs/design/llm-wiki-store-v1.md`](docs/design/llm-wiki-store-v1.md)；Ruff PASS、strict Mypy 152 files / 0 issues、新 Wiki 52 passed / 4 capability skipped、新旧邻接 149 passed / 5 skipped、Backend 3949 passed / 12 skipped / 12 deselected / 83.35%
- [ ] **阶段 3：实现 PDF/HTML Raw Ingestion**：保存不可变 `source.pdf|source.html`；PDF 经本机 Marker Sidecar 输出 `parsed.md`、内嵌图片和 manifest；HTML 使用零网络独立 Parser，删除脚本/危险资源并仅允许受限 `data:` 图片；主应用把 Provider 输出作为不可信制品做路径、类型、配额和 SHA 校验
- [ ] **阶段 4：实现 Wiki 页面、Revision 与原子 Change Set**：每个来源生成入口页草稿，允许 Agent 提出主题子页面；创建/更新/删除页面及 edge add/delete 统一生成稳定 diff；用户一次批准后全有或全无发布，任一 page version/SHA 或 graph revision 变化时整个 Change Set 标记 stale
- [ ] **阶段 5：实现页面级 FTS5 与页面知识图谱**：只索引 active 页面当前已批准 revision；支持 `related_to`、`references`、`extends`、`contradicts`、`part_of`，强制同 Space、去重、自环/`part_of` 循环校验；`derived_from` 只由服务端依据可信来源证据维护
- [ ] **阶段 6：实现 Knowledge Agent 模式与 Space 多对话**：复用 Agent loop、消息/流、lane、compaction 和持久化，新增独立 Knowledge Prompt、内置 Wiki Skill 和工具白名单；`space_id`/`conversation_id` 由可信上下文注入，Agent 只读 Raw、只能写 Change Set staging，禁止跨 Space
- [ ] **阶段 7：实现独立 Knowledge 页面并完成退役验收**：将现有弹窗替换为 Pages/Sources/Graph/Changes/Conversations 页面；支持来源/解析产物查看、页面阅读与 revision、图谱、统一 diff 审批和多对话；完成静态检查、Backend/Frontend/E2E、安全、并发、恢复、真实 Marker 与大文件配额验收后退役旧 API/Worker/UI

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
- [ ] 决定并创建 `0.0.28` 对应的 release tag；package baseline 已确定，但 tag 名称与创建动作仍需用户单独授权
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

- [ ] OCR 与图片理解：LLM Wiki MVP 不承诺扫描 PDF OCR；Marker 的 OCR 行为必须在阶段 1 独立冻结
- [ ] Multi-Agent、Plan Mode；托管 Coding Sandbox 已提升到 P0，Local Docker/Git provider integration 仍按其独立阶段实施
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
| B7 SQLite cleanup | ✅ `688cf08` |
| Persistent Keyring preflight | ✅ `ada31fc` |
| ToolResult ordering + MCP UTF-8 | ✅ `e7bf8f3` |
| Agent semantics + controls + stream lifecycle | ✅ `c214d28` |
| ToolResult usage + deferred-tool metadata | ✅ `b6baea8` |
| Complete-turn Compaction semantics | ✅ `0c72679` |

当前代码基线的验证结果见 [`STATUS.md`](STATUS.md#2026-08-23-当前验证基线)。
