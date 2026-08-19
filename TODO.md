# Current TODO

> 校准日期：**2026-08-19**。本文件只保留尚未完成或明确延期的事项；已完成阶段不再复制数百行历史记录，统一由 [`STATUS.md`](STATUS.md)、[`CHANGELOG.md`](CHANGELOG.md) 和 `docs/validation/` 追溯。

## 当前收敛执行顺序

- [x] **1. 修复全量 Ruff / strict Mypy，使仓库自身 CI 静态检查通过**（完成：Ruff 0 项；Mypy 114 files / 0 issues；后端 CI 3655 passed、6 skipped、12 deselected、coverage 83.73%；前端 lint / typecheck / build 通过；Python CI job timeout 由 10 分钟调整为 30 分钟以容纳完整门禁）
- [x] **2. 整理并提交当前工作区改动，同时更新 `STATUS.md` 到最新验证基线**（完成：运行时/测试/CI 提交 `c214d28`；81 个工作区路径完成分类与敏感信息审计，状态文档记录真实门禁结果）
- [ ] **3. 统一 Python、FastAPI/Auth、前端与 release 版本号，并补齐仓库根 `LICENSE`**
- [ ] **4. 复跑当前提交的完整 Playwright E2E 与最终 GLM 真实 smoke**
- [ ] **5. 继续 pi-agent 对齐：补齐 model、thinking level、streaming message、pending tool calls 等公开 Agent 状态**

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
- [ ] 补齐 model、thinking level、streaming message、pending tool calls 等公开 Agent 状态
- [ ] 对齐 ToolResult 的图片内容、usage 和动态 added-tool metadata

### 4. P2 — Session、Compaction 与持久化

- [ ] 评估并迁移 append-only 会话树或 lane-based Session；支持 branch、fork、label 和 active leaf
- [ ] 引入 durable operation/recovery，避免整份 JSON 覆盖和非原子发布
- [ ] 将 compaction 默认边界改为完整 turn，并补齐 token/window、前缀摘要和重试语义

## P0 — Release 与文档卫生

- [ ] 统一版本元数据：Python `__version__` / FastAPI 当前为 `0.0.21`，前端为 `0.0.20`，最新 tag 为 `v0.0.27-secure-credentials`
- [ ] 在仓库根补齐与 `pyproject.toml` MIT 声明一致的 `LICENSE` 文件
- [ ] 在发布前复跑当前 HEAD 的完整 Playwright E2E，而不是沿用历史阶段数字
- [ ] 决定下一个 release 版本与 tag；tag 仍需用户单独授权
- [ ] 如需 push，先配置 Git remote；当前仓库没有 remote，push 仍需用户单独授权
- [ ] 评估本地初始账号 `admin / 123456` 的改密入口；在此之前继续保持 localhost-only

## P1 — 可靠性与维护

- [ ] 清理全量 Backend 的已知 warning：Starlette/httpx deprecated API、同步测试误用 `@pytest.mark.asyncio`
- [ ] 修复本机 `.pytest_cache` ACL 或在开发流程中固定可写 cache 目录，避免测试通过时仍产生 cache warning
- [ ] 为 ToolResult/UTF-8 修复增加 Browser E2E：跨轮工具卡顺序、中文 DDGS 结果和刷新恢复
- [ ] 设计旧消息中 `U+FFFD` 的可选检测/标记工具；不得声称能恢复已丢失原字符
- [ ] 为 Keyring 启动预检增加 Windows 实机 smoke 文档；保持非敏感探针和执行后清理约束

## P2 — 可选产品迭代

- [ ] **P2-D Session organization**：Session 搜索、收藏、归档
- [ ] 接入 Provider 官方 tokenizer；保留当前 estimator 作为安全 fallback
- [ ] 自动 compaction 策略；明确触发时机、失败回滚和请求并发边界
- [ ] 可选 LLM compaction 摘要器；与 `/checkpointer` 的 Session Memory 语义保持区分
- [ ] 跨后端重启的 request/approval 持久化方案；当前仅浏览器刷新恢复
- [ ] Human Approval 的持久规则/永久授权模型；需要新的安全与审计设计
- [ ] MCP HTTP transport；当前仅 stdio transport 可用

## 明确延期 / Out of scope

- [ ] OCR 与图片理解：扫描 PDF 当前终态为 `needs_ocr`
- [ ] Multi-Agent、Plan Mode、Docker Sandbox/Git Integration
- [ ] RBAC、OAuth、企业级多租户、TLS 公网部署、横向扩展
- [ ] 向量数据库、embedding、hybrid retrieval；当前 Knowledge 固定为 SQLite FTS5/BM25
- [ ] 自动 provider fallback、模型负载均衡、长期后台任务调度

## 已完成基线索引

| 能力 | 最终状态/提交 |
|---|---|
| P2-R Knowledge/RAG + Web Manager | ✅ `7faf635` / `6527be5` |
| Auth + Session Folder + Checkpointer + P0 Runtime | ✅ `b529bbc` |
| P2-A Session reload recovery | ✅ `f30da56` |
| P2-B Approval + P2-C Context Budget/Compaction | ✅ `924b047` |
| B7 SQLite cleanup | ✅ `688cf08` |
| Persistent Keyring preflight | ✅ `ada31fc` |
| ToolResult ordering + MCP UTF-8 | ✅ `e7bf8f3` |
| Agent semantics + controls + stream lifecycle | ✅ `c214d28` |

当前代码基线的验证结果见 [`STATUS.md`](STATUS.md#2026-08-19-当前验证基线)。
