# Current TODO

> Current status: [STATUS.md](STATUS.md)
> Future roadmap: [ROADMAP.md](ROADMAP.md)
> Released versions: [CHANGELOG.md](CHANGELOG.md)

## Current phase

**P1-E Multi-Provider Switching — M1 Multi-Provider Runtime**（PIVOT @ 2026-07-19）。

- 路线：[ROADMAP.md](ROADMAP.md) § P1-E（M1 / M2 / M3 milestone）
- 状态：[STATUS.md](STATUS.md) — Backend Foundation ✅ FROZEN，当前 M1-0 审计待启动
- Pivot 决策：原 P1-E2/E3/E4/E5 拆分过细，E2-4 独立 Security Freeze cancelled——改为 M1 Runtime / M2 Frontend / M3 Unified Freeze 单一 milestone
- Pivot 附录：[docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md) §19

## P1-E1 ✅ COMPLETE（MERGED + TAGGED）

P1-E1 Secure Credentials 已正式交付：

- **master merge commit**：`de05c66`（no-ff；保留 22 commit 阶段性历史）
- **release tag**：`v0.0.27-secure-credentials`
- **post-merge baseline**：1833 passed / 14 deselected / 0 failed；37/37 E2E；ruff clean；working tree clean
- **Push status**：⛔ 未授权（仅本地）

子阶段全部 FROZEN：

- P1-E1-1 Secret Primitives ✅
- P1-E1-2 Credential Repository ✅（含 2.1 并发加固 / 2.2 enum 校验）
- P1-E1-3A Router / Service ✅
- P1-E1-3B Validation Strategy ✅（B1 Anthropic / B2 wiring）
- P1-E1-4 Web API Design ✅ APPROVED
- P1-E1-4A Composition Root ✅
- P1-E1-4B REST API ✅
- P1-E1-5A Security Audit ✅（78 控制 / 5 finding）
- P1-E1-5B Security Hardening ✅（5 finding/gap 全 RESOLVED/CLOSED）
- P1-E1-5C Final Regression ✅

## P1-E2 Backend Foundation（✅ COMPLETE，不单独 merge / tag）

P1-E2 配置后端已 frozen，作为 M1/M2/M3 的持久化基础。**不再扩展配置后端**——E2-4 独立 Security Freeze cancelled，并入 M3 Unified Freeze。

- E2-1 Schema + Store ✅ FROZEN @ `89fabfd`
- E2-2 Service + Static Model Options ✅ FROZEN @ `a2c7932`
- E2-3A Session Creation Audit ✅ FROZEN @ `9878ef9`
- E2-3 Composition + REST API + Session binding ✅ FROZEN @ `17c843d` / `9bbd0f2` / `cad7ca7`

测试基线：2063 full pytest + 2×37/37 E2E + ruff clean + 0 Core Runtime diff + 0 network + 0 secret reads。

## M1 deliverables — Multi-Provider Runtime

### M1-0：Provider Contract Audit（审计门，无生产代码）

- [ ] 审计 `providers/base.py` Adapter contract（stream / close / events）
- [ ] 审计 `providers/glm.py` 流式事件格式 / tool_call 增量 / usage / finish_reason / client close 生命周期
- [ ] 审计 `providers/anthropic_compat.py`（参考；不重写）
- [ ] 审计 `providers/registry.py` ProviderDefinition 结构
- [ ] 审计 Agent 如何持有 Provider（`harness.agent.<provider field>` 引用结构）
- [ ] 审计 `_run_prompt_core` / `_run_regeneration_core` 调用入口
- [ ] 输出 `docs/design/p1-e-m1-provider-runtime.md`：冻结统一 Adapter 接口
- [ ] 审计完成后 **停止并审核**，再启动 M1-1

### M1-1：OpenAI-compatible Provider

- [ ] `providers/openai_compat.py`：Qwen / Kimi 共用 Adapter
- [ ] request / SSE stream / assistant text delta / tool calls / finish reason / usage
- [ ] safe error mapping（错误信息不含 Authorization / 完整 endpoint）
- [ ] client close 生命周期
- [ ] 围绕现有 Provider contract 实现，不发明第二套事件模型
- [ ] `feat(providers): add openai-compatible provider adapter`

### M1-2：Qwen / Kimi ProviderDefinition presets

- [ ] `registry.py` 加 `qwen` preset（protocol=`openai_compatible` / `default_base_url`）
- [ ] `registry.py` 加 `kimi` preset（同上）
- [ ] `GET /api/provider-definitions` 返回 glm / qwen / kimi + anthropic（safe display fields only）
- [ ] `feat(providers): add qwen and kimi provider presets`

### M1-3：Provider Factory

- [ ] `providers/factory.py`：唯一知道「哪个 Provider 用哪个 Adapter」的位置
- [ ] GLM → 包装现有 GLMProvider
- [ ] Qwen / Kimi → `OpenAICompatibleProvider`
- [ ] Anthropic → 包装现有（保留兼容）
- [ ] 输入 ProviderDefinition + api_key + model_id；输出 RequestProvider
- [ ] `feat(providers): add provider factory`

### M1-4：Request Provider Runtime

- [ ] `web/provider_runtime.py`：`RequestProviderRuntime` + `bind_to_harness` async context manager
- [ ] Session Binding → Profile → Credential → Secret → ProviderFactory → 临时绑定 `harness.agent.<provider>`
- [ ] finally 恢复旧 Provider + close request client
- [ ] 复用现有单 active request lock
- [ ] `RequestProviderSelection(profile_id, provider_id, model_id, selection_source)` 不可变快照
- [ ] `feat(web): add request-scoped provider runtime`

### M1-5：Prompt Integration

- [ ] `_run_prompt_core` 接入 `provider_runtime.bind_to_harness`
- [ ] request metadata 记录 `provider_profile_id` / `provider_id` / `model_id` / `selection_source`
- [ ] 不记 credential_id / api_key / Authorization
- [ ] 错误隔离（provider 创建失败 → Agent 不执行；不污染下一请求）
- [ ] `feat(web): bind prompt execution to session provider`

### M1-6：Regenerate Integration

- [ ] `_run_regeneration_core` 接入
- [ ] Regenerate 用当前 Session 当前模型（不动 D2 schema；revision `content_json` 自带 model 信息）
- [ ] `feat(web): bind regenerate execution to session provider`

### M1-7：Runtime Tests

- [ ] GLM / Qwen / Kimi 真实流式回答（mock contract）
- [ ] 工具调用保持正常
- [ ] 切换只影响下次请求（请求级不可变快照）
- [ ] 失败不污染下一请求（错误隔离）
- [ ] Core Runtime diff = 0（GLM 包装，不重写）
- [ ] `test(providers): validate multi-provider runtime`

## M2 deliverables — Frontend Switching

- [ ] `stores/providerStore.ts`
- [ ] `components/provider/ProviderSelector.vue`（顶部切换器；运行时禁用并提示 `Generating...`）
- [ ] `components/provider/ProviderSettingsModal.vue`（每个 Provider 一张卡：API Key + Model ID + status）
- [ ] Session binding restore（刷新后保留选择）
- [ ] `feat(web): add provider switching frontend`

## M3 deliverables — Unified Freeze

- [ ] Secret leak audit（API / SQLite / log / WS / export / marker）
- [ ] GLM / Qwen / Kimi contract tests
- [ ] Prompt / Regenerate / tool / streaming / Session A/B / restart
- [ ] Playwright E2E
- [ ] merge / tag：`feat(providers): deliver multi-provider switching`

## Cross-stage frozen constraints（M1 / M2 / M3 全程约束）

- ❌ 不修改 D2 Revision schema（v2）——provider/model 从 AssistantMessage JSON 投影
- ❌ 不修改 `loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`
- ❌ 不替换现有 `providers/glm.py` / `anthropic_compat.py` / `base.py`（GLM 包装现有；OpenAI-compatible 为新增模块）
- ❌ 不向多个第三方发 API Key 自动探测供应商（只对 Session 选中的 Provider 调用）
- ❌ 不实现自动 provider fallback / 模型负载均衡
- ❌ 不顺带重构 `web/app.py`（留 P3）
- ❌ 不在 P1-E 阶段引入 Markdown 面板（P1-F 独立阶段）
- ❌ **M1/M2/M3 内不调用任何远程模型目录 API**（`/v1/models` 等远端探测均不进入）——Messages API 调用除外
- ❌ **M1/M2/M3 内不引入 remote `ModelOption` source**（静态建议 + 用户手动填写）
- ❌ **M1/M2/M3 内不复制第二套安全中间件**（全复用 E1）
- ✅ M1 起：执行 Prompt/Regenerate 时**会**读 Secret + 构造 HTTP client（仅对 Session 选中的 Profile）
- ✅ keyring 为 web optional dependency；不可用时不阻塞 app 启动
- ✅ 请求启动后 provider/model 不可变（运行中切换只影响下次请求）
- ✅ Regenerate 使用当前 session 当前模型（不动 D2 不变量）

## Next after M1

- M2 Frontend Switching（`providerStore` + `ProviderSelector` + `ProviderSettingsModal`）
- M3 Unified Freeze（security + E2E + merge + tag）

## Deferred

- P1-D3 PDF Text Extraction（转出主路线，重启条件见 ROADMAP）
- PDF / Vector RAG
- P2-A URL Routing + Full Reload Recovery
- P2-B Human Approval UI
- P2-C Context Budget + Compaction UI

## Explicitly out of scope (long-term)

- OCR / Image understanding
- RAG / Vector Memory / Long-term Memory
- Multi-Agent / 多用户 / RBAC / OAuth
- 公网部署 / 横向扩展
- CLI（仅 Web UI 入口）
- 本地文件系统操作工具
- 自动 provider fallback / 模型负载均衡
- 长期后台任务调度
