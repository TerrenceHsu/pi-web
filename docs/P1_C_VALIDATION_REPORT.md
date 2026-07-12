# P1-C Validation Report — Extension Persistence

> **阶段**: P1-C（MCP / Skill 配置持久化）—— ✅ COMPLETE
> **基线**: `v0.0.24-async-architecture` @ `79cea14`
> **Tag**: `v0.0.25-extension-persistence`
> **执行日期**: 2026-07-12 ~ 2026-07-13

## 1. P1-C 总结

P1-C 全部完成——Skill + MCP server + MCP disabled tool 配置持久化 + 启动恢复 + 失败隔离 + 安全策略。

### 子阶段状态

| 子阶段 | 状态 | 测试 |
|---|---|---|
| C1 Persistence Foundation | ✅ PASS | 27 用例 |
| C2 Skill Persistence | ✅ PASS | 20 用例 |
| C3 MCP Persistence | ✅ PASS | 16 用例 |
| C4 Startup Restore & Isolation | ✅ PASS | 16 用例 |
| C5 Browser Validation & Release | ✅ PASS | 5 用例 |

## 2. 测试基线

| 命令 | 结果 |
|---|---|
| offline pytest | **968 passed**（889 P1-B baseline + 79 C1-C4），55.89s |
| Coverage | **83.78%** ≥ 75% PASS |
| Playwright E2E | **28/28 PASS**（23 旧 + 5 persistence），含 retry |
| Ruff | **All checks passed** |
| Frontend production build | 124 modules / **0 处 `__storeHooks` / `__e2eHooks`** |
| Secret safety scan | **PASS**——SQLite / API / body.innerText 无 secret marker |

## 3. 最终 SQLite Schema

详见 [`docs/P1_C_PERSISTENCE_ARCHITECTURE.md`](P1_C_PERSISTENCE_ARCHITECTURE.md)。

## 4. API 变化

### 新增
- `DELETE /api/skills/{name}`——删除 uploaded Skill（DB row 为准，非 upload 403）

### 变化
- `GET /api/mcp/servers` 响应加 `desired_enabled` / `attached` / `restore_status` / `missing_env_keys` 字段
- `enabled` 语义 = `desired_enabled`（兼容映射）

## 5. 安全策略

- env value **永不写入 SQLite**（只 env_keys）
- env value 从 `os.environ` 恢复
- Missing env → `restore_status: "needs_env"` + `missing_env_keys`（只 key name）
- `_safe_extension_error()` 替换 secret values（空字符串过滤 + 长度降序）
- Skill prompt body 默认不返回

## 6. 兼容性

- ✅ 旧 `POST /api/prompt` / `/api/prompt/async` 不回归
- ✅ 旧 `POST /api/abort` 兼容别名保留
- ✅ WebSocket reconnect replay / event envelope / request registry 不回归
- ✅ Session / messages / snapshots 数据不受影响
- ✅ `enabled` 字段语义兼容（= desired_enabled）

## 7. 已知限制

1. **Request registry 内存态**——server 重启后 active request 丢失
2. **Single harness 单 active request**——不支持多 session 并行
3. **env value 必须通过 os.environ**——不能放进 args / command
4. **Localhost-only / no auth**——不适合公网部署

## 8. 是否改 core runtime

**否**。未改：loop / agent / harness 核心 / context / providers / events / stream_events / tools / mcp 核心协议 / skills 模型 / skill_loader。

## 9. 建议 tag

`v0.0.25-extension-persistence`

## 10. 下一阶段建议

P1-D 产品功能（Export markdown / Regenerate / PDF 文本提取）。
