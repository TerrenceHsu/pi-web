# Current TODO

> Current status: [STATUS.md](STATUS.md)
> Future roadmap: [ROADMAP.md](ROADMAP.md)
> Released versions: [CHANGELOG.md](CHANGELOG.md)

## Current phase

**P1-E2 Provider Profiles + Session Model Bindings**——设计 DESIGN FROZEN @ 2026-07-19，待 E2-1 编码。

- 设计文档：[docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md)
- 最简后端方案：3 张表 / 4 个主要新增生产模块 / 7 API 操作 / **E2 真实网络调用 = 0**

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

## P1-E2 deliverables

### E2-1：Schema + Store

- [ ] `web_provider_config_schema_meta` schema meta v1
- [ ] `web_provider_profiles` 表 + 索引（partial unique `ux_provider_profiles_default`）
- [ ] `web_session_model_bindings` 表 + 索引（FK profile ON DELETE RESTRICT）
- [ ] Provider Config Store 独立 aiosqlite connection + `PRAGMA foreign_keys=ON` 验证返回 1
- [ ] Profile CRUD（`profile_id` 随机不可预测；`provider_id` 创建后 immutable）
- [ ] default Profile 单事务切换（`BEGIN IMMEDIATE` 内 UPDATE 旧 + UPDATE 新）
- [ ] Binding CRUD + 显式 profile-in-use 查询（与 FK RESTRICT 双重保险）
- [ ] restart persistence 测试
- [ ] `feat(providers): add profile and session binding store`

### E2-2：Service + Static Model Options

- [ ] `ProviderConfigService`（create / update / delete / list profile；get / set / init session binding）
- [ ] Profile status 派生（ready / disabled / needs_credential / needs_key / backend_unavailable / credential_invalid / credential_error）
- [ ] Provider 作用域约束：仅 `last_validated_provider_id == profile.provider_id` 时 `invalid`/`error` 影响当前 Profile
- [ ] enabled/is_default 交叉约束（is_default 要求 enabled；禁用当前默认同事务清除；显式绑定 disabled → 409 `profile_disabled`）
- [ ] `ANTHROPIC_MODEL_OPTIONS` / `GLM_MODEL_OPTIONS` 静态常量
- [ ] `validate_model_id`（控制字符 / 换行 / NUL 拒绝；长度 ≤ 256）
- [ ] `feat(providers): add profile configuration service`

### E2-3A：Session Creation Audit（审计门，无生产代码）

- [ ] 审计现有 Web Session create 函数调用路径
- [ ] 审计 Session delete 能力（messages / files / snapshots / revisions cleanup）
- [ ] 审计 ID 返回时机与并发可见性
- [ ] 审计补偿失败语义
- [ ] 确定 §13 失败策略（A 整体回滚 / B 补偿删除 / C 不阻塞）
- [ ] 列出对 `web/app.py` / `web/state.py` / `session_sqlite.py` / `extension_store.py` 的最小接线修改清单
- [ ] `docs(providers): audit session creation for E2-3 binding wiring`

### E2-3：REST API + Session Creation Binding

- [ ] 7 API 操作（4 Profile CRUD + 1 models + 2 session binding）
- [ ] 安全 DTO（Pydantic `extra="forbid"`；不接受 secret_value / secret_ref / base_url / headers / Authorization）
- [ ] E1 安全 envelope 复用（TrustedHost / Origin / `X-PI-Agent-UI` / 32 KiB body / SafeValidationError）
- [ ] Session 创建时物化默认 Binding（按 E2-3A 冻结的策略）
- [ ] `web/app.py` + 通用安全边界模块 + Session 创建路径的最小接线修改（不复制第二套安全中间件、不大规模重构）
- [ ] `feat(web): expose provider profiles and session bindings`

### E2-4：Restart + Security + Freeze

- [ ] Profile / Binding restart persistence 验证
- [ ] session-only → `needs_key` / env → 动态恢复 / keyring → `ready` 验证
- [ ] Credential 删除后 Profile 保留 + status=`needs_credential` 验证
- [ ] Profile in-use 删除返回 409 `profile_in_use`（FK RESTRICT + 显式查询双重）
- [ ] 显式绑定 disabled Profile 返回 409 `profile_disabled`
- [ ] disabled Profile 仍可保留已有 Binding 验证
- [ ] `model_id` 控制字符 / 换行 / NUL 拒绝验证
- [ ] `provider_id` 必须存在于 ProviderRegistry 验证
- [ ] `is_default` 切换 / 清除单事务验证（含禁用默认自动清除）
- [ ] `PRAGMA foreign_keys=ON` 启动时验证返回 1
- [ ] API / SQLite / log / WS / export 无 Key（marker 测试）
- [ ] **E2 真实外部网络调用 = 0**（marker 测试：所有 E2 测试均不发出 HTTP 请求）
- [ ] `test(providers): freeze profile and session binding foundation`

## Cross-stage frozen constraints（P1-E / P1-F 全程约束）

- ❌ 不修改 D2 Revision schema（v2）——provider/model 从 AssistantMessage JSON 投影
- ❌ 不修改 `loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`
- ❌ 不替换现有 `providers/glm.py` / `anthropic_compat.py` / `base.py`（包装而非重写）
- ❌ 不向多个第三方发 API Key 自动探测供应商
- ❌ 不实现自动 provider fallback / 模型负载均衡
- ❌ 不顺带重构 `web/app.py`（留 P3）
- ❌ 不在 P1-E 阶段引入 Markdown 面板（P1-F 独立阶段）
- ❌ **E2 内不调用任何远程模型目录 API**（`/v1/models` 等远端探测均不进入 E2）
- ❌ **E2 内不读取 Secret / 不构造 HTTP client / 不引入 remote `ModelOption` source**
- ❌ **E2 内不复制第二套安全中间件**（全复用 E1）
- ✅ keyring 为 web optional dependency；不可用时不阻塞 app 启动
- ✅ 请求启动后 provider/model 不可变（运行中切换只影响下次请求）
- ✅ Regenerate 使用当前 session 当前模型（不动 D2 不变量）

## Next after P1-E2

- P1-E3 Request-scoped Provider Selection（`web/provider_runtime.py`）
- P1-E4 Frontend Provider / Model Selector
- P1-E5 E2E + Docs + Freeze

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
