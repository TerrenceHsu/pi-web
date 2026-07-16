# Current TODO

> Current status: [STATUS.md](STATUS.md)
> Future roadmap: [ROADMAP.md](ROADMAP.md)
> Released versions: [CHANGELOG.md](CHANGELOG.md)

## Current phase

**P1-E1 Secure Credentials**——设计已冻结，待 P1-E1 实施计划批准后进入编码。

实施计划文档：[docs/design/p1-e1-secure-credentials.md](docs/design/p1-e1-secure-credentials.md)（待提交）。

## P1-E1 deliverables

- [ ] `ProviderDefinition` 内置表（GLM / Anthropic-compatible / OpenAI-compatible / Custom）
- [ ] `SecretStore` Protocol + 3 实现（`OSKeyringSecretStore` / `InMemorySecretStore` / `EnvSecretStore`）
- [ ] `CredentialRecord` repository（SQLite 只存 `secret_ref` / `fingerprint` / `masked_value`）
- [ ] masked/fingerprint serializer（前端只见 `sk-****8A31`）
- [ ] Provider hint local heuristic（格式规则——**不**向第三方发请求探测）
- [ ] Credential CRUD + validate API
- [ ] Secret leak tests（REST response / WS event / snapshot / log / export 必须 0 命中）
- [ ] P1-E1 实施计划文档定稿

## P1-E1 explicitly out of scope

- 模型目录（P1-E2）
- ProviderProfile + Session 绑定（P1-E2）
- 请求执行切换 `provider_runtime.py`（P1-E3）
- 前端模型选择器（P1-E4）
- Regenerate 模型切换（P1-E3，依赖 P1-E2 SessionModelBinding）
- OpenAI Adapter 完整实现（P1-E2 按需）

## Cross-stage frozen constraints（P1-E / P1-F 全程约束）

- ❌ 不修改 D2 Revision schema（v2）——provider/model 从 AssistantMessage JSON 投影
- ❌ 不修改 `loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`
- ❌ 不替换现有 `providers/glm.py` / `anthropic_compat.py` / `base.py`（包装而非重写）
- ❌ 不向多个第三方发 API Key 自动探测供应商
- ❌ 不实现自动 provider fallback / 模型负载均衡
- ❌ 不顺带重构 `web/app.py`（留 P3）
- ❌ 不在 P1-E 阶段引入 Markdown 面板（P1-F 独立阶段）
- ✅ keyring 为 web optional dependency；不可用时不阻塞 app 启动
- ✅ 请求启动后 provider/model 不可变（运行中切换只影响下次请求）
- ✅ Regenerate 使用当前 session 当前模型（不动 D2 不变量）

## Next after P1-E1

- P1-E2 Profiles + Model Catalog + Session Binding
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
