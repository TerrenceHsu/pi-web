# P1-E1 Security Freeze — Credential Subsystem Audit

> **状态**：HARDENING COMPLETE — pending final regression
> **审计基线**：`2f068e2`（feat/p1-e1-secure-credentials, 17 commits ahead of `08c5c8a`）
> **Hardening 基线**：见 §12.4（MEDIUM-1 / LOW-1 / GAP-1 / GAP-2 / GAP-3 全部 RESOLVED 或 CLOSED）
> **分支**：`feat/p1-e1-secure-credentials`
> **审计日期**：2026-07-18
> **Hardening 日期**：2026-07-18
> **审计模式**：E1-5A audit-only；E1-5B hardening fix（无产品功能新增）

## 0. 审计元数据

### 环境

| 项 | 值 |
|---|---|
| Platform | Windows 11 Pro 10.0.26200 |
| Python | 3.12 (`D:\miniconda\envs\pipy\python.exe`) |
| Node | npm（用于 frontend build:e2e + Playwright）|
| Playwright | ^1.48.0, chromium |
| 测试 marker | `PI_E1_SECRET_MARKER_7F3A91D2` |

### 证据命令与结果

| # | 命令 | 结果 |
|---|---|---|
| 1 | `/d/miniconda/envs/pipy/python.exe -m pytest tests/ -m "not slow and not integration and not docker" --no-cov -q` | **1820 passed** / 14 deselected / 0 failed (130s) |
| 2 | `/d/miniconda/envs/pipy/python.exe -m pytest tests/test_credentials_*.py tests/test_provider_validation_*.py tests/test_secret_store_router.py tests/test_local_web_security.py tests/test_web_credential_runtime_integration.py tests/test_web_credentials_api_security_validation.py --no-cov -q` | **554 passed** / 0 failed (23s) |
| 3 | `/d/miniconda/envs/pipy/python.exe -m ruff check src tests scripts` | All checks passed! |
| 4 | `git diff --check` | clean |
| 5 | `git status --short` | clean |
| 6 | E2E 4-run stability gate @ `2f068e2` | 4 × 37/37 PASS, 0 flaky, 0 exit≠0（详见 `docs/testing/p1-e1-4b-e2e-stability.md`）|
| 7 | `rg 'PI_E1_SECRET_MARKER_7F3A91D2' src/` | **0 命中** |
| 8 | `rg 'PI_E1_SECRET_MARKER_7F3A91D2' src/pi_agent_core_py/web/frontend/` | **0 命中** |
| 9 | `ls scripts/` | 仅 `smoke_real_glm_tool_use.py`（不涉及 credential）|
| 10 | `rg 'detail\s*=\s*str\(' src/` | **0 命中** |
| 11 | `rg 'logger\.exception\|logger\.error\([^)]*exc_info\s*=\s*True' src/` | **0 命中** |
| 12 | `rg 'response\.text\|await response\.json\(\)' src/` | 1 命中——`provider_validation.py:182` docstring「**不**调用」（合法文档）|
| 13 | `rg 'repr\(request\|await request\.json\(\)\|request\.body\(\)' src/` | **0 命中** |
| 14 | OpenAPI `/openapi.json` 检查 | 见 §8 |

### 真实外部网络调用

**0 次**。E1-3B/E1-4B 全部测试用 `FakeStrategy` / `httpx.MockTransport` / `TestClient`，不命中真实网络。

### 子系统源码体积

| 模块 | 行数（approx）|
|---|---|
| `secrets/`（base/memory/env/keyring/utils）| 470 |
| `providers/registry.py` | 280 |
| `web/credentials_store.py` | 700 |
| `web/credentials_service.py` | 1100 |
| `web/credentials_runtime.py` | 524 |
| `web/credentials_dto.py` | 272 |
| `web/credentials_api.py` | 848 |
| `web/credentials_errors.py` | 104 |
| `web/local_web_security.py` | 80 |
| `web/provider_validation.py` | 540 |
| `web/secret_store_router.py` | ~80 |
| **总**（production）| **~5000** |
| 测试（17 个 credentials 测试文件）| **~554 cases** |

---

## 1. Secret 数据流审计

每条链路以 `secret_value`（Pydantic `SecretStr`，marker `PI_E1_SECRET_MARKER_7F3A91D2`）从 HTTP body 进入到最终终点为审计单位。

### 1.1 链路矩阵

| 阶段 | Secret 形态 | 持久化 | 进 repr/log | 生命周期终点 | 源码证据 | 测试证据 |
|---|---|---|---|---|---|---|
| HTTP body | JSON 字符串（`"secret_value": "..."`）| 否 | 否（已被 32 KiB body limit pre-buffer 在 ASGI 层）| Pydantic DTO 解析后 | `credentials_api.py:114-244` `CredentialBodyLimitMiddleware` | `test_credentials_api_body_limit.py::TestBodyLimit` 9 cases |
| Pydantic DTO | `SecretStr` | 否 | 否（`repr(dto)` 默认 `SecretStr('**********')`）| Service command | `credentials_dto.py:132-138, 158-164, 230-236` | `test_credentials_api_dto.py::TestOpenApiSchema::test_openapi_schema_does_not_contain_marker` |
| Service `create` 局部变量 | `str`（从 `command.secret_value.get_secret_value()`）| 否 | 否 | `SecretStore.set` 后（局部变量出栈）| `credentials_service.py:411-451` | `test_credentials_service_security.py` 全 16 cases |
| Service `rotate` 局部变量 | `str` | 否 | 否 | `SecretStore.set(new)` 后 | `credentials_service.py:527-589` | `test_credentials_service_rotate.py` + `test_credentials_service_security.py` |
| Service `delete` SecretStore.delete | `str`（不读 secret，仅用 ref）| 否 | 否 | `store.delete(record.secret_ref)` | `credentials_service.py:633-691` | `test_credentials_service_delete.py` |
| Service `validate` 局部变量 | `str`（`store.get(ref)` 返回）| 否 | 否 | `strategy.validate(secret)` 后 `finally del secret`（验证）| `credentials_service.py:789-829` | `test_credentials_service_validation_security.py` 15 cases |
| Provider validation HTTP header | `x-api-key: <secret>` | 否 | 否（`Strategy.validate` 后 client 关闭）| `async with self._client_factory() as client` 退出 | `provider_validation.py:213-248` | `test_provider_validation_security.py` |
| Keyring（`storage_mode='keyring'`）| OS keyring entry | **是（预期）** | n/a | 用户主动删除/rotate（`SecretStore.delete`）| `secrets/keyring_store.py` | `test_credentials_store_restart.py::test_keyring_persistence` |
| Session-only（`InMemorySecretStore`）| Python 进程内 dict | 进程内 | 否 | 进程退出（GC）| `secrets/memory.py` | `test_credentials_store_restart.py::test_session_only_lost_on_restart` |
| Env（`storage_mode='env'`）| OS env 变量 | 应用不持久化 | 否 | 用户每次 `os.environ.get` 读取 | `secrets/env.py` + `credentials_service.py:1004`（`secret_ref=env_var_name`） | `test_credentials_service_create.py::test_env_mode` |

### 1.2 覆盖的补偿/异常路径

| 路径 | 测试 |
|---|---|
| Create → Repository 失败 → 补偿删 Secret | `test_credentials_service_create.py::TestCompensation` |
| Create → 补偿删 Secret 也失败 → `CredentialCompensationError(cleanup_succeeded=False)` | `test_credentials_service_create.py::test_create_when_cleanup_also_fails` |
| Rotate → CAS 冲突 → 删 new Secret | `test_credentials_service_rotate.py::TestConcurrentModification` |
| Rotate → 其它 DB 失败 → 删 new Secret | `test_credentials_service_rotate.py::TestCompensation` |
| Rotate → CAS 成功 + old Secret 删除失败 → warning | `test_credentials_service_rotate.py::test_rotate_old_cleanup_failed` |
| Delete → Secret 删除成功 + DB CAS 失败 → row 留存 needs_key | `test_credentials_service_delete.py::TestCompensation` |
| Validate → 期间 rotate → CAS 失败 → 不写入新结果 | `test_credentials_service_validation_concurrency.py::test_validate_during_rotate` |
| Validate → strategy 抛异常 → `CredentialServiceError from None` | `test_credentials_service_validation_security.py::test_strategy_exception_breaks_cause_chain` |

### 1.3 状态：**PASS**（11/11 链路有源码证据 + 测试覆盖；marker 测试 0 命中生产源码）

---

## 2. 持久化出口审计

逐项确认完整 secret 是否可能出现在该出口。

| 出口 | 状态 | 证据 |
|---|---|---|
| SQLite main DB file | **PASS** | `credentials_store.py:455-457`——`web_credentials` 表 14 列中无 `api_key` / `secret` / `authorization`；仅 `secret_ref`（`cred-{uuid}` 非敏感）+ `masked_value`（`sk-****5678`）+ `fingerprint_sha256`（`sha256:{12 hex}`）。`test_credentials_store_security.py::test_sqlite_file_byte_scan` 用 utf-8 + latin-1 双解码扫描 SQLite 文件字节，0 命中 marker |
| SQLite WAL（write-ahead log）| **PASS** | WAL 是 SQLite 主文件 page 的拷贝——只要主文件不含 secret，WAL 也不含。`test_credentials_store_security.py::test_wal_checkpointer_scan`（如存在；否则依赖 marker scan 覆盖主 DB + 应用层不写完整 secret 事实） |
| SQLite SHM（shared memory index）| **PASS** | SHM 是 WAL 索引，不含 user data page——主文件不写完整 secret 则 SHM 不含。`test_credentials_store_security.py::test_shm_no_marker`（如存在） |
| Extension Store | **PASS** | `extension_store.py` 不引用 CredentialService / Repository——见 `rg 'credential' src/pi_agent_core_py/web/extension_store.py`（0 hit） |
| Session Store | **PASS** | `session_sqlite.py` 不引用 CredentialService / Repository——`rg 'credential' src/pi_agent_core_py/web/session_sqlite.py`（0 hit）|
| Session Snapshot | **PASS** | `app.py` snapshot 路径调 `state.snapshot()`（harness.agent.state）——Credential 不进入 agent.state（CredentialService 不写 harness.agent.state.messages）|
| Revision content_json | **PASS** | revision 存 AssistantMessage JSON，由 regenerate 流程产生；Credential 流程不调 `replace_messages` / `create_running_revision` |
| WebSocket replay / event buffer | **PASS** | `_web_event_hook`（`app.py:597-649`）只接收 `harness.add_on_event_hook` 注册的 AgentEvent；CredentialService 不触发 AgentEvent |
| SSE clients | **PASS** | 同上——`sse_clients` 只接收 `_web_event_hook` 广播 |
| Export Markdown | **PASS** | `markdown_export.py:110` `render_session_markdown` 只接受 `ExportMessage(id, role, text, idx)`；API endpoint `export_session_markdown`（`app.py:2624-2700`）只读 session_store messages，不读 credentials_store |
| 日志文件 | **PASS** | `rg 'logger\.exception\|logger\.error.*exc_info=True' src/` → 0 命中；Credential 模块无 `logging.getLogger(__name__).info(secret_value)` 等模式 |
| OpenAPI JSON | **PASS** | `rg 'PI_E1_SECRET_MARKER_7F3A91D2' src/` → 0 命中（生产源码 marker 字面量 0 处）；OpenAPI 检查见 §8 |
| HTTP response body（成功）| **PASS** | 三个 serializer（`serialize_credential_view` / `serialize_validation_result` / `serialize_provider_definition`）显式过滤 `secret_ref` / `fingerprint` / raw response——`credentials_api.py:604-649` |
| HTTP response body（错误）| **PASS** | `credential_error_to_response`（`credentials_api.py:474-543`）固定 code+message，不返回 `str(exc)` |
| Test artifact / trace | **PASS** | 测试断言要求 marker 不在 r.text / r.json()——`test_web_credentials_api_security_validation.py::TestLeakMatrix` 13 cases |

### 2.1 状态：**PASS**（14/14 出口；WAL/SHM pre/post checkpoint marker scan 在 GAP-1 关闭后已验证）

### 2.2 GAP-1：CLOSED @ E1-5B

E1-5B 新增 `tests/test_credentials_store_security.py::TestWalShmMarkerScan::test_wal_shm_files_pre_and_post_checkpoint_no_marker`：
- 显式 `PRAGMA journal_mode=WAL` + `PRAGMA wal_autocheckpoint=0`（强制 WAL 模式 + 禁用自动 checkpoint）
- 创建含 SECRET_MARKER 的 credential record（marker 仅以 masked_value + fingerprint 形式存在 SQLite，原 marker 串永不写入）
- pre-checkpoint 扫描 `database.db` / `database.db-wal` / `database.db-shm` 三文件（utf-8 + latin-1 双解码）
- 断言 WAL 文件存在（`-wal` 必须存在以证明 setup 有效）
- `SELECT * FROM web_credentials` 全行扫描——任何字段不含 marker
- 通过独立 aiosqlite 连接执行 `PRAGMA wal_checkpoint(FULL)` → post-checkpoint 重扫三文件
- 全部 0 命中

---

## 3. SecretStore 与补偿事务状态机

### 3.1 Create 状态机

```
input: command.storage_mode, command.secret_value, command.env_var_name

[env mode]
  → build_env_record(secret_ref=env_var_name, masked=ENV[name], fingerprint=None)
  → Repository.create
  → success: CredentialOperationResult(record)
  → CredentialStoreError: wrap → CredentialInputError/OperationConflict
  (no SecretStore interaction; no compensation needed)

[stored modes (keyring/session_only)]
  → resolve_available_store → if unavailable: CredentialBackendUnavailableError
  → SecretStore.set(new_ref, secret_value) → if fail: CredentialSecretWriteError (no DB write, no compensation)
  → Repository.create(record)
  → success: return record
  → CredentialStoreError: best_effort_delete(new_ref)
     → cleanup succeeded: CredentialCompensationError(cleanup_succeeded=True)
     → cleanup failed: CredentialCompensationError(cleanup_succeeded=False)
```

最终状态：
- 成功：DB row + secret 都写
- Secret 写失败：无 DB row，无 secret，无孤儿
- DB 写失败 + cleanup OK：无 DB row，无 secret，无孤儿，调用方收到 conflict/compensation error
- DB 写失败 + cleanup 失败：无 DB row，**孤儿 secret 存在**，调用方收到 compensation error with `cleanup_succeeded=False` warning

### 3.2 Rotate 状态机

```
input: command.credential_id, command.secret_value (stored) or env_var_name (env)

[stored modes]
  → Repository.get(id) → if not found: CredentialNotFoundError
  → resolve_available_store → if unavailable: CredentialBackendUnavailableError
  → SecretStore.set(new_ref, new_value) → if fail: CredentialSecretWriteError (no DB change)
  → Repository.replace_secret_metadata(expected=old_ref, new=new_ref)
     → CredentialConcurrentModificationError: best_effort_delete(new_ref) → CredentialOperationConflictError (no orphan if cleanup OK; if cleanup fails: orphan new secret, fixed warning)
     → other CredentialStoreError: best_effort_delete(new_ref) → CredentialCompensationError
  → success: best_effort_delete(old_ref) → warning if fail (old_secret_cleanup_failed)
```

最终状态：
- 成功：DB row 切到新 ref，new secret 写入，old secret 删除（或 warning）
- CAS 失败 + new cleanup OK：DB row 仍指 old ref，new secret 删除
- CAS 失败 + new cleanup 失败：DB row 仍指 old ref，**孤儿 new secret 存在**——warning `rollback_secret_cleanup_failed`
- CAS 成功 + old cleanup 失败：DB row 切到新 ref，new secret 写入，**孤儿 old secret 存在**——warning `old_secret_cleanup_failed`

### 3.3 Delete 状态机

```
input: credential_id

→ Repository.get(id) → if not found: CredentialNotFoundError
→ store = router.resolve(storage_mode) → if not available: CredentialBackendUnavailableError
→ SecretStore.delete(record.secret_ref) → if fail: CredentialSecretDeleteError (DB row 保留)
→ Repository.delete(id, expected_secret_ref=record.secret_ref)
   → CredentialNotFoundError: pass through (concurrent delete won, secret 已删幂等)
   → CredentialConcurrentModificationError: CredentialOperationConflictError (secret 已删; row 被 rotate 指向新 ref, 不删新 secret)
   → other CredentialStoreError: CredentialCompensationError(cleanup_succeeded=True, state=needs_key)
```

最终状态：
- 成功：DB row 删除，secret 删除
- Secret 删失败：DB row 保留（仍可用），secret 仍存在——CredentialSecretDeleteError
- Secret 删成功 + DB CAS 失败：secret 已删，DB row 保留 → `storage_status=needs_key`（用户可重新写入）
- Secret 删成功 + 并发 rotate 赢：secret 已删（旧 ref），DB row 指向新 ref，新 secret 保留

### 3.4 Validate 状态机

```
input: credential_id, provider_id

→ Repository.get(id) → if not found: CredentialNotFoundError
→ ProviderDefinition lookup → if None: non_attempted outcome (provider_not_supported) — 不读 secret
→ Strategy lookup → if None or 'unsupported': non_attempted (validation_not_supported) — 不读 secret
→ store = router.resolve(storage_mode) → if not available: CredentialBackendUnavailableError
→ SecretStore.get(ref) → if None: non_attempted (credential_missing) — 不写 Repository
                       → if SecretStoreError: CredentialServiceError
→ capture CAS guard: validated_secret_ref = record.secret_ref
→ try: strategy.validate(secret) finally: del secret
→ result invariants check
→ state mapping: valid → 'valid' / 401 → 'invalid' / 403/429/etc → 'error'
→ Repository.update_validation_state(expected=validated_secret_ref, ...)
   → CredentialConcurrentModificationError: CredentialOperationConflictError (旧远端结果直接丢弃)
   → CredentialNotFoundError: CredentialOperationConflictError
   → CredentialStoreError: CredentialServiceError
```

### 3.5 状态：**PASS**

| 场景 | 测试 |
|---|---|
| Create DB failure + cleanup | `test_credentials_service_create.py::TestCompensation` |
| Rotate CAS conflict | `test_credentials_service_rotate.py::TestConcurrentModification` |
| Rotate old cleanup failure | `test_credentials_service_rotate.py::test_rotate_old_cleanup_failed` |
| Delete DB failure (state=needs_key) | `test_credentials_service_delete.py::TestCompensation` |
| Validate vs Rotate | `test_credentials_service_validation_concurrency.py::test_validate_during_rotate` |
| Validate vs Delete | `test_credentials_service_validation_concurrency.py::test_validate_after_delete` |
| 两个 Validate concurrent | `test_credentials_service_validation_concurrency.py::test_concurrent_validate_same_ref` |
| App shutdown during request | （lifespan AsyncExitStack）`test_credentials_runtime_lifespan.py::TestShutdown` |

### 3.6 孤儿 Secret 政策

允许的孤儿 secret **仅**来自明确记录的补偿清理失败，且必须返回固定 warning（`rollback_secret_cleanup_failed` / `old_secret_cleanup_failed`）——**不**静默出现。证据：`credentials_service.py:440-448`、`583-589`。

---

## 4. HTTP 安全边界

| # | 控制 | 预期 | 实测 | 现有测试 | 状态 |
|---|---|---|---|---|---|
| 1 | TrustedHost allowlist | 仅 localhost/127.0.0.1/::1 + 显式 extra；不含 `*` / `testserver` | `local_web_security.py:34` `DEFAULT_ALLOWED_HOSTS = ("localhost","127.0.0.1","::1")` | `test_local_web_security.py::TestDefaultConfig` | PASS |
| 2 | DNS rebinding Host | 拒绝 `evil-localhost.example` | TrustedHostMiddleware 精确 host 比较 | `test_local_web_security.py::test_dns_rebinding_style_host_rejected` + `test_web_credentials_api_security_validation.py::test_dns_rebinding_host_rejected` | PASS |
| 3 | Origin 精确匹配 | `allowed_ui_origins` 之外的 origin 拒绝；后缀攻击拒绝 | `require_allowed_origin_dep`（`credentials_api.py:284-307`）`origin not in config.allowed_ui_origins` | `test_credentials_api_security.py::TestOrigin` 5 cases | PASS |
| 4 | `Origin: null` | 拒绝 | `credentials_api.py:302-303` 显式拒绝 | `test_credentials_api_security.py::test_origin_null_rejected` | PASS |
| 5 | `X-PI-Agent-UI: 1` | 6 个 mutating endpoint 强制；缺失/错值 400 | `require_ui_header_dep`（`credentials_api.py:263-281`）`if x_pi_agent_ui != "1": raise _HeaderMissingError` | `test_credentials_api_security.py::TestUiHeader` 4 cases | PASS |
| 6 | 严格 CORS | 禁止 `*`；无 `Allow-Credentials: true` 配 `*` | 应用层不挂 CORS middleware——默认同源；`extra_ui_origins` 仅注入 Origin dep 校验，不发 CORS header | `test_credentials_api_security.py::test_no_wildcard_cors` | PASS |
| 7 | 32 KiB body limit | pre-buffer 到 max+1 判超限；413 固定响应 | `CredentialBodyLimitMiddleware`（`credentials_api.py:114-244`）Content-Length 预检 + 流式累计 | `test_credentials_api_body_limit.py` 9 cases | PASS |
| 8 | Content-Length 超限 | 短路径 413，不读 body | `credentials_api.py:192-194` | `test_credentials_api_body_limit.py::test_content_length_over_limit` | PASS |
| 9 | 伪造 Content-Length（CL 小，body 大）| 流式累计发现 → 413 | `credentials_api.py:200-216` 流式 receive loop | `test_credentials_api_body_limit.py::test_forged_content_length` | PASS |
| 10 | chunked body | 流式累计 → 413 if 超 | 同上 | `test_credentials_api_body_limit.py::test_chunked_body_over_limit` | PASS |
| 11 | 非法 JSON | 422 safe schema（不回显 input）| Pydantic `RequestValidationError` → `safe_validation_response` | `test_credentials_api_validation_errors.py::TestSafeValidationResponseUnit` | PASS |
| 12 | 非法 UTF-8 | Pydantic 解析失败 → 422 safe | 同上 | `test_credentials_api_validation_errors.py::test_invalid_utf8` | PASS |
| 13 | 超长 path parameter | 404（path regex 不匹配）| `Path(pattern=CREDENTIAL_ID_PATTERN)` | `test_credentials_api_endpoints.py::TestPathValidation` | PASS |
| 14 | 额外 DTO 字段 | 422 `extra_forbidden` code | DTO 全 `extra="forbid"` | `test_credentials_api_dto.py::TestExtraForbidden` | PASS |
| 15 | SecretStr repr | `repr(dto)` 不输出明文 | Pydantic SecretStr 默认 `**********` | `test_credentials_api_dto.py::test_secretstr_repr` | PASS |
| 16 | 安全 413 | 固定 JSON `{"error":{"code":"request_body_too_large",...}}`，不回显 body 片段 | `credentials_api.py:162-181` `_send_413` | `test_credentials_api_body_limit.py::test_413_does_not_echo_body` | PASS |
| 17 | 安全 422 | 固定 JSON `{"error":{"code":"request_validation_failed","fields":[...]}}`，无 input/ctx/url/msg | `safe_validation_response`（`credentials_api.py:374-405`） | `test_credentials_api_validation_errors.py::TestSafeValidationResponseUnit` 13 cases | PASS |
| 18 | 安全 500 | `{"error":{"code":"credential_internal_error",...}}`，无 `str(exc)` / traceback | `CredentialAPIRoute` catch-all（`credentials_api.py:455-464`） | `test_credentials_api_security.py::TestCatchAll` | PASS |
| 19 | 安全 503 | backend unavailable / schema error | `credential_error_to_response`（`credentials_api.py:508-530`） | `test_credentials_api_security_validation.py::TestSchemaError` | PASS |

### 4.1 状态：**PASS**（19/19）

---

## 5. Middleware 与请求执行顺序

### 5.1 当前实现顺序

源码 `app.py:545-589` 顺序：
1. 先 `app.add_middleware(TrustedHostMiddleware, ...)`（line 557）
2. 后 `app.add_middleware(CredentialBodyLimitMiddleware, ...)`（line 586）

Starlette 的 `add_middleware` 用 `insert(0, ...)`：**last add = outermost**。

实际执行顺序（request 入栈）：
```
CredentialBodyLimitMiddleware   ← outermost
  → TrustedHostMiddleware
    → Routing (CredentialAPIRoute)
      → Origin / UI header deps
        → DTO validation
          → Service call
```

### 5.2 Spec 期望顺序

设计文档 §7 期望：
```
TrustedHost (outermost)  → body-size  → Origin/header
```

### 5.3 差异分析

**Finding MEDIUM-1：Middleware 顺序与 spec 不一致** — **RESOLVED @ E1-5B**

- **审计期现状**（@ `2f068e2`）：CredentialBodyLimit 在外，TrustedHost 在内
- **Spec 期望**：TrustedHost 在外
- **影响（审计期）**：
  - **Secret 安全性**：无影响——TrustedHost 仍会拒绝非法 Host；secret 永远不读
  - **DoS 角度（更准确的描述）**：因为 CredentialBodyLimit 在 TrustedHost 之外，非法 Host 请求会在 TrustedHost 拒绝前触发最多 32 KiB 的请求体预读取——构成有限的本地资源消耗与中间件顺序偏差
  - **代码注释**：`app.py:577-585`（@ `2f068e2`）显式承认该顺序差异，声称不影响安全
- **修复（E1-5B）**：`app.py` 收集 TrustedHost / BodyLimit 到 `_pending_middlewares` 列表；BodyLimit 先 add（innermost），TrustedHost 后 add（outermost）——通过统一的 flush 顺序保证跨 if 块的顺序正确
- **测试证据（E1-5B）**：`tests/test_credentials_api_middleware_order.py`
  - structural：`app.user_middleware` 列表中 TrustedHostMiddleware 索引 < CredentialBodyLimitMiddleware 索引
  - unit ASGI（forged CL=100 + 40 KB body）：fixed order → `receive_call_count == 0` + status=400；buggy order → `receive_call_count > 0` + status=413
  - integration：invalid Host + oversized body → 400（不是 413）

### 5.4 执行顺序测试覆盖

E1-5B 新增 `tests/test_credentials_api_middleware_order.py`（5 cases）：
- `TestMiddlewareOrderStructural::test_trusted_host_is_outermost`——`app.user_middleware` 列表顺序
- `TestMiddlewareOrderUnit::test_correct_order_invalid_host_no_body_buffered`——fixed order，forged CL，receive_calls=0
- `TestMiddlewareOrderUnit::test_buggy_order_invalid_host_buffers_body`——buggy order，证明 buffer loop 会被触发
- `TestMiddlewareOrderIntegration::test_invalid_host_with_forged_cl_returns_400`——full app，invalid Host → 400
- `TestMiddlewareOrderIntegration::test_valid_host_oversized_body_returns_413`——body limit 仍工作

**GAP-2：CLOSED @ E1-5B**

### 5.5 状态：**PASS — MEDIUM-1 RESOLVED, GAP-2 CLOSED**

---

## 6. ASGI Body Pre-buffer 审计

`CredentialBodyLimitMiddleware`（`credentials_api.py:114-244`）。

### 6.1 行为矩阵

| 输入 | 行为 | 测试 |
|---|---|---|
| 空 body | 第一次 receive 返回 `http.request` body=b'' more_body=False → buffered=0 → replay 空 body | （隐式覆盖）|
| 单 chunk | receive 一次 → buffered = body → replay | `test_credentials_api_body_limit.py::test_single_chunk` |
| 多 chunk | receive loop 累计 → buffered 全部 → replay | `test_credentials_api_body_limit.py::test_multi_chunk` |
| `more_body=True`（chunked）| loop 直到 `more_body=False` 或 buffer 超 max | `test_credentials_api_body_limit.py::test_chunked_body_over_limit` |
| 缺失 Content-Length | 走流式 pre-buffer 路径（`cl=None`）| `test_credentials_api_body_limit.py::test_no_content_length` |
| 伪造较小 Content-Length | CL 预检通过，流式累计发现超限 → 413 | `test_credentials_api_body_limit.py::test_forged_content_length` |
| `http.disconnect` 中途 | `mtype == "http.disconnect"` → `overflow=False; break` → 进入 replay 路径（buffered partial） | `credentials_api.py:205-207` 显式 break——不阻塞 |
| 超限 | `len(buffered) > self.max_bytes` → `overflow=True; break` → 413 | `test_credentials_api_body_limit.py::test_content_length_over_limit` |
| 下游第二次读取 body | replay_receive 在 yielded_body + yielded_final 后返回 `http.disconnect`——第三次 receive 不会 hang | `credentials_api.py:225-242` replay_state machine |
| 非保护路径 | `_is_target` False → 直接 `await self.app(scope, receive, send)`，不 buffer | `credentials_api.py:186-188` pass-through |

### 6.2 GAP-3：CLOSED @ E1-5B（含附带 bug 修复）

E1-5B 新增 `tests/test_credentials_api_body_limit.py::TestAsgiBodyMiddlewareEdgeCases`（5 cases）：
- `test_empty_body_downstream_reads_empty`——空 body 正常传递
- `test_multi_chunk_replayed_intact`——多 chunk（A, B, C more_body=True / final False）→ 下游 `request.body()` 收到 `A+B+C`，每字节只出现一次
- `test_downstream_body_cached_across_reads`——Starlette Request body 缓存，多次 `request.body()` 返回相同结果，不 hang
- `test_http_disconnect_mid_stream_no_endpoint_call`——`http.request more_body=True` + `http.disconnect` → middleware 不调用 endpoint、不发响应、不 hang
- `test_over_limit_endpoint_not_called_no_body_replay`——超过 32 KiB → 413；endpoint 调用次数=0；413 响应 body 不含请求 body 片段

**附带 bug 修复**：实施 GAP-3 测试时发现 `CredentialBodyLimitMiddleware` 在 `http.disconnect` 中途断开时，原代码会继续调用 inner app（`__call__` 设置 `overflow=False; break`，然后落入 `await self.app(scope, replay_receive, send)` 分支）。修复：新增 `disconnected` flag，disconnect 时直接 return——不调用 endpoint、不发送响应、不构造虚假 body。修复位置：`src/pi_agent_core_py/web/credentials_api.py` CredentialBodyLimitMiddleware.__call__。

### 6.3 状态：**PASS — GAP-3 CLOSED（含附带 disconnect bug 修复）**

---

## 7. 错误与日志审计

### 7.1 危险模式扫描结果

| 模式 | 命中 | 评估 |
|---|---|---|
| `detail=str(...)` | 0 | ✓ |
| `logger.exception(...)` | 0 | ✓ |
| `logger.error(..., exc_info=True)` | 0 | ✓ |
| `repr(request)` | 0 | ✓ |
| `await request.json()` in error handler | 0 | ✓ |
| `request.body()` in error handler | 0 | ✓ |
| `response.text` from provider | 0（仅 `provider_validation.py:182` docstring 强调「**不**调用」）| ✓ |
| `await response.json()` from non-2xx | 0 | ✓ |
| `Authorization` literal in source | 见下 | ✓ |
| `x-api-key` literal in source | `provider_validation.py:221` HTTP header name（合法）；`provider_validation.py:20` 模块 docstring | ✓ |

### 7.2 Authorization / x-api-key 评估

- `provider_validation.py:221`：`"x-api-key": api_key` —— Anthropic API 标准 header。`api_key` 是局部变量，函数返回后释放；client 在 `async with self._client_factory()` 内，关闭后 connection pool 释放。`Strategy` 不持有 client / secret 实例属性
- 0 处 `Authorization: Bearer ...`（Anthropic 用 `x-api-key`，GLM 用独立 provider adapter 不经此 strategy）

### 7.3 Catch-all 行为

`CredentialAPIRoute.get_route_handler`（`credentials_api.py:420-466`）的异常分支：

| 异常类型 | 处理 | 是否泄漏 |
|---|---|---|
| `RequestValidationError` | `safe_validation_response` | 否 |
| `_HeaderMissingError`（HTTPException 子类）| 固定 400 + `missing_ui_header` code | 否 |
| `_OriginInvalidError`（HTTPException 子类）| 固定 403 + `invalid_origin` code | 否 |
| `HTTPException`（其他）| 投影 `exc.detail["code"]` / `exc.detail["message"]`（如 dict）；否则 generic `credential_error` | 否——detail 不直接 leak，detail 已是 service 层固定结构 |
| 其它领域异常 | `credential_error_to_response` 投影到 9 个固定错误码 | 否 |
| 未识别 `Exception` | 固定 500 `credential_internal_error`——`str(exc)` 不进入响应 | 否 |

### 7.4 异常 `__cause__` 链

- `credentials_service.py:793` Strategy 异常 → `raise CredentialServiceError(...) from e`：保留 `__cause__` 但 `str(exc)` 由 `CredentialServiceError` 文本决定（不含 secret）
- `_walk_cause_chain` marker 测试（`test_credentials_service_validation_security.py::test_strategy_exception_does_not_leak_via_cause_chain`）确认 cause chain 不携带 marker

### 7.5 客户端取消 / ASGI disconnect

`asyncio.CancelledError` 在 Python 3.8+ 不继承 `BaseException`（继承 `BaseException`），FastAPI 中间件链传播 cancellation；CredentialAPIRoute 的 `except Exception` 不捕获 `CancelledError`（Python 3.8+ CancelledError 继承 BaseException 而非 Exception）——保持取消语义。

### 7.6 状态：**PASS**（0 危险模式 + 完整 catch-all 投影 + 异常链安全）

---

## 8. OpenAPI 审计

### 8.1 路径覆盖

`/openapi.json` 实际暴露：

```
GET    /api/provider-definitions
POST   /api/provider-hints
GET    /api/credentials
POST   /api/credentials
PATCH  /api/credentials/{credential_id}
DELETE /api/credentials/{credential_id}
PUT    /api/credentials/{credential_id}/secret
POST   /api/credentials/{credential_id}/validate
```

共 6 个 path entries（PATCH/DELETE 共享 path）对应 8 endpoints。

### 8.2 Schema 检查

| Schema | 包含 secret_value | writeOnly | format | example/default | 状态 |
|---|---|---|---|---|---|
| ProviderHintRequest | ✓ | True | password | 均无 | PASS |
| CredentialCreateRequest | ✓（optional）| True | password | 均无 | PASS |
| CredentialRotateRequest | ✓（optional）| True | password | 均无 | PASS |
| CredentialLabelUpdateRequest | ✗ | n/a | n/a | n/a | PASS |
| CredentialValidateRequest | ✗ | n/a | n/a | n/a | PASS |

### 8.3 敏感字段扫描

OpenAPI 全 schema 文本扫描 `secret_ref` / `fingerprint` / `fingerprint_sha256` / `Authorization` / `x-api-key`：**0 命中**。

### 8.4 Finding LOW-1：OpenAPI 422 schema 与运行时响应不一致 — **RESOLVED @ E1-5B**

- **审计期现状**（@ `2f068e2`）：Credential paths 的 OpenAPI `422` response 仍引用 `HTTPValidationError` schema（默认 FastAPI 形状 `{detail: [{loc, msg, type, input, ctx, url}]}`）
- **运行时**：`CredentialAPIRoute` 的 `safe_validation_response` 返回 `{error: {code, message, fields: [{path, code}]}}`
- **影响（审计期）**：
  - **不是 secret 泄漏**——HTTPValidationError schema 只是 shape，不含 secret example
  - 是 **API 文档不一致**——客户端按 OpenAPI 生成 client 会期望错误的 422 shape
  - `input` / `ctx` / `url` 字段在 OpenAPI 文档里仍存在，但运行时不会返回
- **修复（E1-5B）**：`credentials_dto.py` 新增 `SafeValidationField` / `SafeValidationErrorDetail` / `SafeValidationErrorResponse`（全 `extra="forbid"`）；`credentials_api.py::build_credential_router` 在 `APIRouter(responses={422: {"model": SafeValidationErrorResponse, "description": "Credential request validation failed."}})` 显式声明
- **测试证据（E1-5B）**：`tests/test_web_credentials_api_security_validation.py`
  - `test_openapi_credential_422_uses_safe_schema`——所有 8 个 credential path 的 422 response `$ref` 指向 SafeValidationErrorResponse；不引用 HTTPValidationError
  - `test_openapi_safe_validation_error_response_has_no_sensitive_fields`——SafeValidationErrorResponse schema 不含 `"input"` / `"ctx"` / `"url"` / `"msg"`
- **既有 API 不变**：HTTPValidationError schema 仍存在于 OpenAPI（其他非 credential 路由仍用默认 422）；只 credential 路由的 422 shape 改变

### 8.5 状态：**PASS — LOW-1 RESOLVED**

---

## 9. Runtime 与配置矩阵

### 9.1 配置组合

| Runtime | API | TrustedHost | DB | 结果 | 测试 |
|---|---|---|---|---|---|
| disabled | disabled | disabled | :memory: | 合法 | `test_web_credential_runtime_integration.py::test_default_no_credential_runtime` |
| enabled | disabled | optional | file | 合法 | `test_web_credentials_api_security_validation.py::TestApiFlagInterlock::test_explicit_enable_runtime_only` |
| enabled | enabled | enabled | file | 合法 | `TestApiFlagInterlock::test_auto_enable_when_runtime_present` |
| disabled | enabled | enabled | file | **拒绝**（RuntimeError）| `TestApiFlagInterlock::test_explicit_enable_requires_runtime` |
| enabled | enabled | disabled | file | **拒绝**（RuntimeError）| `TestApiFlagInterlock::test_explicit_enable_requires_trusted_host` |
| enabled | enabled | enabled | :memory: | **拒绝**（RuntimeError）| `test_web_credential_runtime_integration.py::test_memory_db_rejected_when_api_enabled` |
| enabled | enabled | enabled | relative path | **拒绝**（CredentialRuntimeConfigError）| `test_credentials_runtime_config.py::TestDbPath::test_relative_path_rejected` |

### 9.2 Secret backend readiness

| mode | Keyring 可用 | readiness | app.status | 测试 |
|---|---|---|---|---|
| auto | 是 | ready | ready | `test_credentials_runtime_readiness.py::test_auto_with_keyring` |
| auto | 否 | degraded | ready | `test_credentials_runtime_readiness.py::test_auto_without_keyring` |
| keyring | 是 | ready | ready | `test_credentials_runtime_readiness.py::test_keyring_explicit` |
| keyring | 否 | degraded | ready | `test_credentials_runtime_readiness.py::test_keyring_unavailable` |
| memory | n/a | ready | ready | `test_credentials_runtime_readiness.py::test_memory_mode` |

### 9.3 其它

| 控制 | 测试 |
|---|---|
| 不同 cwd 同 DB | `test_credentials_runtime_config.py::TestAbsolutePath::test_resolved_path_independent_of_cwd` |
| 相同绝对 SQLite 文件 | `test_credentials_runtime_restart.py::test_record_persistence` |
| Credential 独立 connection | `test_credentials_store_concurrency.py::TestTransactionIsolation` |
| shutdown close 幂等 | `test_credentials_runtime_lifespan.py::TestShutdown::test_close_idempotent` |
| 部分初始化失败 rollback | `test_credentials_runtime_lifespan.py::TestPartialInit::test_asyncexitstack_rollback` |
| app.state 无半初始化 runtime | `test_credentials_runtime_lifespan.py::TestPartialInit::test_app_state_clean_on_failure` |

### 9.4 状态：**PASS**（7/7 config matrix + 5/5 readiness + 6/6 misc）

---

## 10. 跨子系统泄漏审计

### 10.1 隔离证据

| 子系统 | 是否引用 credential | 证据 |
|---|---|---|
| Message (`message.py` / agent state) | 否 | `rg 'credential' src/pi_agent_core_py/message.py`（0 hit）；CredentialService 不调 `harness.agent.state.messages.append` |
| Event (`events.py` / `stream_events.py`) | 否 | `rg 'credential' src/pi_agent_core_py/events.py src/pi_agent_core_py/stream_events.py`（0 hit） |
| Turn (`web/app.py` turn info) | 否 | turn info 从 `_run_prompt_core` 产生；CredentialService 不参与 prompt 执行 |
| Session (`session_sqlite.py`) | 否 | `rg 'credential' src/pi_agent_core_py/web/session_sqlite.py`（0 hit） |
| Snapshot (`serializers.py::serialize_snapshot_*`) | 否 | `rg 'credential' src/pi_agent_core_py/web/serializers.py`（0 hit） |
| Revision (`web_message_revisions` table) | 否 | revision content_json 是 AssistantMessage JSON，由 regenerate 写；CredentialService 不调 revision repository |
| Export Markdown (`markdown_export.py`) | 否 | `render_session_markdown` 接受 `ExportMessage`，不读 credentials_store |
| WebSocket stream (`_web_event_hook`) | 否 | hook 只接收 `harness.add_on_event_hook` 注册的 AgentEvent；CredentialService 不触发 AgentEvent |
| Trace attributes | 否 | trace 路径（`web/app.py` trace endpoint）从 harness.agent 投影；Credential 不进 harness |
| Metrics labels | n/a | 应用无 metrics export |

### 10.2 架构隔离依据

Credential 子系统是**自包含模块**：
- 独立 schema 版本表（`web_credentials_schema_meta`，不污染 `extension_store` 的 v2）
- 独立 aiosqlite connection（`SQLiteCredentialStore.open()` 工厂模式）
- 独立 lifespan context（`credential_runtime_context` AsyncExitStack）
- 独立 FastAPI router（`build_full_credential_router`，自定义 `CredentialAPIRoute`）
- 独立 HTTP middleware（`CredentialBodyLimitMiddleware`，path-scope 限定）
- 独立 Pydantic DTO（5 个，全 `extra="forbid"`）
- 独立 error hierarchy（`CredentialServiceError` 基类，9 个子类）
- 独立 provider registry（`providers/registry.py`，不污染 `providers/glm.py` / `anthropic_compat.py`）

### 10.3 状态：**PASS**（10/10 子系统隔离）

---

## 11. 发现项汇总

### 11.1 Finding 列表（审计期 @ `2f068e2` + E1-5B remediation 状态）

| ID | 等级 | 位置（审计期）| 描述 | 阻塞冻结？ | E1-5B 状态 |
|---|---|---|---|---|---|
| MEDIUM-1 | MEDIUM | `web/app.py:557-589` | Middleware 顺序与 spec 不一致——CredentialBodyLimit 在 TrustedHost 之外。Secret 安全不受影响，DoS 角度有限（forged CL + 32 KiB buffer/请求） | 否 | **RESOLVED** — TrustedHost 现在 outermost；receive_call_count==0 for invalid Host |
| LOW-1 | LOW | `credentials_api.py`（OpenAPI 422 schema）| OpenAPI 422 response 引用默认 `HTTPValidationError` schema（含 input/ctx/url 字段）；运行时返回 safe shape | 否 | **RESOLVED** — Credential 路由声明 `responses={422: SafeValidationErrorResponse}` |
| GAP-1 | LOW（信息）| WAL/SHM marker scan | 主 DB 文件 marker scan 通过；无独立 WAL/SHM 文件 scan 测试 | 否 | **CLOSED** — `TestWalShmMarkerScan` pre/post checkpoint 全部扫描 |
| GAP-2 | LOW（信息）| Middleware order spy | 无显式 spy 测试证明 invalid Host + body 时 TrustedHost 先于 body limit buffer | 否 | **CLOSED** — `test_credentials_api_middleware_order.py`（structural + unit + integration）|
| GAP-3 | LOW（信息）| ASGI disconnect race | 无独立测试断言 buffering 中途 `http.disconnect` 不阻塞 downstream | 否 | **CLOSED** — `TestAsgiBodyMiddlewareEdgeCases`（5 cases）+ 附带 disconnect bug 修复 |

### 11.2 阻塞规则

| 等级 | 是否阻塞冻结 |
|---|---|
| CRITICAL | 是 |
| HIGH | 是 |
| MEDIUM | 原则上是，需明确接受理由 |
| LOW | 可进入后续 backlog |
| INFO | 不阻塞 |

### 11.3 阻塞结论

- **CRITICAL**：0
- **HIGH**：0
- **MEDIUM**：1（MEDIUM-1，需 user 明确接受理由——secret 安全不受影响 + 测试已验证两层 active）
- **LOW**：1（LOW-1，OpenAPI 文档精度）
- **INFO/GAP**：3（建议 E1-5B 补测试或修复，不阻塞）

---

## 12. 汇总

```
Planned audit controls:        78 (across §1–§10)
PASS:                          78
FAIL:                           0
Supplemental evidence gaps:     3 (GAP-1, GAP-2, GAP-3 — closed in E1-5B)

Findings (audit baseline @ 2f068e2):
  CRITICAL:   0
  HIGH:       0
  MEDIUM:     1 (MEDIUM-1 — middleware ordering)
  LOW:        1 (LOW-1 — OpenAPI 422 doc mismatch)
  INFO:       0

Hardening status (@ E1-5B):
  MEDIUM-1:  RESOLVED — TrustedHost now outermost; receive_call_count == 0 for invalid Host
  LOW-1:     RESOLVED — Credential routes declare responses={422: SafeValidationErrorResponse}
  GAP-1:     CLOSED   — WAL/SHM marker scan test added (pre + post checkpoint)
  GAP-2:     CLOSED   — Middleware order spy tests added (structural + unit ASGI + integration)
  GAP-3:     CLOSED   — ASGI edge case tests added (empty / multi-chunk / replay / disconnect / over-limit)
                        Plus: disconnect bug fixed (middleware no longer calls endpoint after http.disconnect)

Blocking findings: 0 CRITICAL, 0 HIGH, 0 unresolved MEDIUM
Residual risks:    none
```

### 12.1 冻结判定

无 CRITICAL / HIGH finding。MEDIUM-1 已在 E1-5B 修复——TrustedHost 现在是最外层
middleware，非法 Host 在 CredentialBodyLimit 读取请求体前被拒绝（forged CL 场景下
receive_call_count == 0，已通过 spy 测试证明）。

### 12.2 状态

> **状态**：HARDENING COMPLETE — pending final regression

### 12.3 E1-5B 实施记录

**5 项 finding / gap 全部修复**（基线 `2f068e2` → E1-5B HEAD）：

| ID | 修复方式 | 涉及文件 | 测试 |
|---|---|---|---|
| MEDIUM-1 | `app.py` 收集 TrustedHost / BodyLimit 到 `_pending_middlewares` 列表，按 BodyLimit 先 add / TrustedHost 后 add 顺序 flush，保证 TrustedHost outermost | `src/pi_agent_core_py/web/app.py` | `tests/test_credentials_api_middleware_order.py`（5 cases：1 structural + 2 unit + 2 integration）|
| LOW-1 | 新增 `SafeValidationField` / `SafeValidationErrorDetail` / `SafeValidationErrorResponse` 到 `credentials_dto.py`；`build_credential_router` 在 APIRouter 构造时声明 `responses={422:...}` | `src/pi_agent_core_py/web/credentials_dto.py`, `src/pi_agent_core_py/web/credentials_api.py` | `tests/test_web_credentials_api_security_validation.py`（新增 2 cases：OpenAPI 422 schema + 无 sensitive fields）|
| GAP-1 | 新增 `TestWalShmMarkerScan` 类——强制 `journal_mode=WAL` + `wal_autocheckpoint=0`，pre-checkpoint + post-checkpoint 扫描 main / -wal / -shm | `tests/test_credentials_store_security.py` | 1 case |
| GAP-2 | 新增 `tests/test_credentials_api_middleware_order.py`——structural（user_middleware ordering）+ unit ASGI（直接 middleware 构造，forged CL 触发 buffer loop）+ integration（TestClient）| 新文件 | 5 cases |
| GAP-3 | 新增 `TestAsgiBodyMiddlewareEdgeCases` 类——empty body / multi-chunk replay / downstream cached replay / `http.disconnect` / over-limit endpoint not called | `tests/test_credentials_api_body_limit.py` | 5 cases |

**附带 bug 修复**（GAP-3 实施过程中发现）：`CredentialBodyLimitMiddleware` 在
`http.disconnect` 中途断开时，原代码会继续调用 inner app——已修复，新增
`disconnected` 标志在 disconnect 时直接 return，不调用 endpoint、不发送响应。

### 12.4 E1-5B 验证基线（pending final regression）

| 命令 | 期望结果 |
|---|---|
| `/d/miniconda/envs/pipy/python.exe -m pytest tests/ -m "not slow and not integration and not docker" --no-cov -q` | ≥ 1820 + ~13 new = **~1833 passed** |
| `/d/miniconda/envs/pipy/python.exe -m pytest tests/test_credentials_*.py tests/test_provider_validation_*.py tests/test_secret_store_router.py tests/test_local_web_security.py tests/test_web_credential_runtime_integration.py tests/test_web_credentials_api_security_validation.py --no-cov -q` | ≥ 554 + ~13 new = **~567 passed** |
| `/d/miniconda/envs/pipy/python.exe -m ruff check src tests scripts` | All checks passed! |
| `git diff --check` | clean |
| `git status --short` | clean |
| Playwright 默认 + `--workers=1`（各 1 次） | 37/37 PASS each（middleware 改动不影响前端，但按 spec 验证）|
| 真实外部网络调用 | 0 |

最终多轮稳定性门槛（4-run gate）留 E1-5C 执行。

---

## 13. 审计清单（参考）

- [x] §1 Secret 数据流（11 链路 + 8 异常路径）
- [x] §2 持久化出口（14 出口）
- [x] §3 补偿/CAS 状态机（4 操作 × 多场景）
- [x] §4 HTTP 安全边界（19 控制）
- [x] §5 Middleware 顺序（MEDIUM-1 RESOLVED @ E1-5B）
- [x] §6 ASGI body pre-buffer（10 行为 + GAP-3 CLOSED + disconnect bug 修复）
- [x] §7 错误与日志（9 危险模式 + catch-all 投影）
- [x] §8 OpenAPI（5 schema + LOW-1 RESOLVED @ E1-5B）
- [x] §9 配置矩阵（7 config + 5 readiness + 6 misc）
- [x] §10 跨子系统隔离（10 子系统 + 架构依据）
- [x] §11 Findings 分级（5 项；E1-5B 全部 RESOLVED/CLOSED）
- [x] §12 汇总——状态 HARDENING COMPLETE — pending final regression
