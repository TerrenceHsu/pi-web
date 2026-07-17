# P1-E1-4 Web API 设计

> **状态**：APPROVED — P1-E1-4 design frozen（2026-07-17）
> **阶段**：P1-E1 第 4 子阶段——Web Harness 接入
> **基线**：`feat/p1-e1-secure-credentials` HEAD `05429ab`（P1-E1-3B2 FROZEN）
> **前置条件**：E1-1 / E1-2 / E1-3A / E1-3B1 / E1-3B2 全部 PASS / FROZEN
> **后续阶段**：E1-5 Security Freeze；P1-E2 ProviderProfile

---

## 0. 审核修订记录

| 日期 | 修订 |
|---|---|
| 2026-07-17 | 初稿（DRAFT）——E1-4A/E1-4B 拆分；8 endpoints；safe DTO；secret 长度限制；HTTP 错误映射；测试矩阵 45 项. |
| 2026-07-17 | **APPROVED—— CONDITIONAL PASS 升级为 APPROVED**：补 6 项阻塞设计——① Localhost Web Security Boundaries（Host allowlist + Origin 校验 + `X-PI-Agent-UI` 自定义 header + 严格 CORS） ② HTTP body-size 32 KiB 限制（流式累计，不只看 Content-Length） ③ Pydantic 错误脱敏只投影 `loc` + 受控 `code`（不返回 input / ctx / url / msg） ④ 绝对 DB path 一次性解析 ⑤ `AsyncExitStack` 部分初始化回滚 ⑥ readiness/degraded 语义（keyring 不可用不阻塞 app `ready`）. 新增 §7 SecretStr + `writeOnly` + 不提供 example / default. 测试矩阵从 45 扩到 68（+23：Local Web Security 8 + Body/Validation 9 + Lifespan/Path 6）. E1-4A scope 扩至含 TrustedHost + allowed origins；E1-4B 含 32 KiB body limit + Origin/Host/header enforcement + 安全 ValidationError handler. |

---

## 1. 目标

把已经冻结的 Credentials 领域能力（SecretStore + SQLiteCredentialStore + CredentialService + ValidationStrategy）接入 Web Harness，对外暴露 **8 个 REST endpoint**，对内完成 **Composition Root + Lifespan**.

**威胁模型升级**：Credentials API 开始接触真实 API Key——威胁从"本地聊天应用"升级为"本地秘密管理服务". 本设计必须建立 localhost 跨站 / DNS rebinding / CSRF 防护（§7）.

实现拆为两个子阶段，先验证启动 / 关闭 / 重启语义，再暴露外部 API：

| 子阶段 | 范围 |
|---|---|
| **E1-4A** | Composition Root + Lifespan（Secret backend 选择 / SQLite connection / CredentialService 构造 / app.state 注入 / 重启恢复 / 安全降级 / 绝对 DB path / TrustedHost + allowed origins 配置 / AsyncExitStack 失败清理） |
| **E1-4B** | Credential REST API（8 endpoints / 32 KiB body limit / Origin/Host/`X-PI-Agent-UI` enforcement / 严格 DTO / safe serializer / 安全 ValidationError handler / 固定 HTTP 错误映射 / Validation endpoint / Secret leak 测试） |

### 1.1 本阶段实现

```text
SecretStore factory
  → SecretStoreRouter
  → SQLiteCredentialStore (独立 connection, 绝对路径)
  → CredentialService
  → REST API (8 endpoints, 32 KiB body limit, localhost 安全边界)
  → safe serializer
  → lifespan initialize / close (AsyncExitStack)
```

### 1.2 本阶段不实现（明确排除）

- ProviderProfile（留 P1-E2）
- Model Catalog（留 P1-E2）
- Session 模型绑定（留 P1-E3）
- 顶部模型选择器 / 前端 Key 管理 Modal（前端工作留后续）
- Custom Base URL（留 P1-E2）
- OpenAI-compatible Adapter（留 P1-E2）
- 请求级模型切换（留 P1-E3）
- GLM 远端验证（DEFERRED）
- Messages probe（PERMANENTLY BANNED）
- PDF / RAG（DEFERRED）
- 用户认证 / 多租户（P1-E1 仍假设单用户本地）
- Core Runtime 修改（`loop.py` / `agent.py` / `context.py` / `providers/base.py` 等不动）

---

## 2. 文件结构

```
src/pi_agent_core_py/
├── providers/
│   ├── registry.py                   # ❌ 不动（E1-3B1 FROZEN）
│   ├── base.py / glm.py / ...        # ❌ 不动
├── secrets/
│   ├── base.py / memory.py / ...     # ❌ 不动（E1-1 FROZEN）
├── web/
│   ├── app.py                        # ← lifespan initialize/close + router include + TrustedHost middleware
│   ├── state.py                      # ← 加 CredentialRuntimeState + CredentialRuntimeConfig
│   ├── credentials_runtime.py        # ✨ 新：build_credential_runtime + AsyncExitStack + 绝对 path 解析 + WebSecurityConfig
│   ├── credentials_security.py       # ✨ 新：TrustedHost / Origin / X-PI-Agent-UI middleware + 32 KiB body limit + 安全 ValidationError handler
│   ├── credentials_store.py          # ❌ 不动（E1-2 FROZEN）
│   ├── credentials_service.py        # ❌ 不动（E1-3A + E1-3B2 FROZEN）
│   ├── credentials_errors.py         # ❌ 不动（E1-3A FROZEN）
│   ├── credentials_api.py            # ✨ 新：FastAPI router（8 endpoints）+ DTO（SecretStr + writeOnly）+ serializer
│   ├── provider_validation.py        # ❌ 不动（E1-3B1 FROZEN）
│   ├── serializers.py                # ← 仅加 credential / validation 序列化函数
│   └── frontend/                     # ❌ 不动（本阶段不做前端）
```

### 2.1 允许修改

- `src/pi_agent_core_py/web/app.py`——lifespan initialize / close + TrustedHost middleware + router include
- `src/pi_agent_core_py/web/state.py`——加 `CredentialRuntimeState` + `CredentialRuntimeConfig`
- `src/pi_agent_core_py/web/serializers.py`——加 credential / validation 序列化函数
- `src/pi_agent_core_py/web/credentials_runtime.py`——新文件
- `src/pi_agent_core_py/web/credentials_security.py`——新文件
- `src/pi_agent_core_py/web/credentials_api.py`——新文件
- `tests/test_credentials_runtime_*.py` / `tests/test_credentials_security_*.py` / `tests/test_credentials_api_*.py`——新测试

### 2.2 禁止修改

- 任何 Core Runtime 文件（`loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`）
- `providers/registry.py` / `providers/base.py` / `providers/glm.py` / `providers/anthropic_compat.py` / `providers/fake.py`
- `secrets/*`（E1-1 FROZEN）
- `web/credentials_store.py`（E1-2 FROZEN——schema / transaction / CAS 不动）
- `web/credentials_service.py`（E1-3A + E1-3B2 FROZEN）
- `web/credentials_errors.py`（E1-3A FROZEN）
- `web/provider_validation.py`（E1-3B1 FROZEN）
- `web/extension_store.py` / `session_sqlite.py`（D2 baseline）
- `web/frontend/*`

---

## 3. Composition Root（E1-4A）

### 3.1 Secret backend mode

通过环境变量配置：

```text
PI_AGENT_SECRET_BACKEND = auto | keyring | memory
```

未设置时默认 `auto`. **未知值 → 启动失败**（不静默降级）.

**注意**：`env` 不是全局 backend——它是单条 Credential 的 storage_mode.

#### `auto`（默认）

```text
尝试初始化 OSKeyringSecretStore
  ├─ is_available() = True  → keyring slot 注册 OSKeyringSecretStore
  └─ is_available() = False → keyring slot 注册 None
始终注册：
  session_only → InMemorySecretStore
  env          → EnvSecretStore
```

**禁止**：自动把 keyring 写入降级成 session_only（语义失真）.

#### `memory`

```text
keyring      → None
session_only → InMemorySecretStore
env          → EnvSecretStore
```

用于 CI / E2E / 无桌面 Secret Service 的开发环境.

**禁止**：把 keyring slot 也指向 InMemorySecretStore.

#### `keyring`

```text
keyring      → OSKeyringSecretStore  (or None if unavailable)
session_only → InMemorySecretStore
env          → EnvSecretStore
```

显式要求 keyring backend. 不可用时应用仍可启动，但 readiness 报告 `degraded`（§6.3）.

### 3.2 绝对 DB path 一次性解析（阻塞项 §四）

**冻结规则**：

- Credential Repository 使用**与 session/extension store 相同的绝对数据库文件路径** + 独立 aiosqlite connection
- 数据库路径在**创建 App 时解析一次**（expanduser + resolve absolute）
- 解析结果存入 `CredentialRuntimeConfig`，请求过程中不得重新解析
- 不得依赖 `cwd`——避免重复 `VirtualFileStore("./uploads")` 类问题

实现：

```python
# web/credentials_runtime.py
def _resolve_db_path(configured: str) -> str:
    """Resolve at app creation; never re-resolve per request."""
    from pathlib import Path
    p = Path(configured).expanduser()
    if not p.is_absolute():
        # 显式拒绝相对路径——必须由调用方在 app 配置层完成 join
        raise CredentialRuntimeConfigError(
            f"credential DB path must be absolute after expanduser (got {configured!r})"
        )
    return str(p.resolve())
```

**关键不变量**：

- `same database file` ≠ `same connection`
- Session/Extension Store 与 Credential Store **共享 DB 文件**，但事务和生命周期独立
- 测试覆盖：从不同 cwd 启动 → Credential DB 仍写入同一文件；不产生第二个意外 SQLite 文件

### 3.3 SecretStoreRouter 构造

复用 E1-3A 的 `SecretStoreRouter(stores={...})`. 传入 dict：

```python
{
    "keyring":      OSKeyringSecretStore() | None,
    "session_only": InMemorySecretStore(),
    "env":          EnvSecretStore(),
}
```

### 3.4 SQLiteCredentialStore lifecycle

复用 E1-2 的 `await SQLiteCredentialStore.open(db_path)`. 独立 aiosqlite.Connection.

### 3.5 CredentialService 构造

```python
service = CredentialService(
    repository=repository,
    router=router,
    provider_registry=ProviderRegistry(list_provider_definitions()),
    validation_strategy_registry=get_default_validation_strategy_registry(),
    now_ms=default_now_ms,
)
```

### 3.6 WebSecurityConfig（阻塞项 §一）

Composition Root 一次性构造 localhost 安全配置：

```python
@dataclass(frozen=True)
class WebSecurityConfig:
    """Localhost security boundary for credential endpoints.

    Frozen at app creation; loaded from env / config file.
    """
    allowed_hosts: frozenset[str]           # {"localhost", "127.0.0.1", "[::1]"} + dev hosts
    allowed_ui_origins: frozenset[str]      # 精确 origin（scheme://host:port）
    require_ui_header: bool = True          # X-PI-Agent-UI required on mutating endpoints
    max_request_body_bytes: int = 32 * 1024 # 32 KiB
```

读取来源（优先级从高到低）：

- 环境变量 `PI_AGENT_ALLOWED_HOSTS` / `PI_AGENT_ALLOWED_UI_ORIGINS`
- 默认值：`localhost` / `127.0.0.1` / `[::1]` + `http://localhost:{FRONTEND_PORT}`

详见 §7.

### 3.7 推荐入口

```python
# web/credentials_runtime.py

async def build_credential_runtime(
    *,
    db_path: str,
    secret_backend: Literal["auto", "keyring", "memory"] = "auto",
    web_security: WebSecurityConfig,
) -> CredentialRuntimeState:
    """Construct repository + router + service per env config.

    Uses AsyncExitStack to guarantee partial-init cleanup.
    Returns:
        CredentialRuntimeState with all owned resources.
    Raises:
        CredentialRuntimeConfigError: 未知 backend / 相对 DB path
        CredentialsSchemaError:       schema 损坏不可恢复
    """
    ...
```

---

## 4. App State

在 `web/state.py` 新增：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class CredentialRuntimeConfig:
    """Frozen configuration resolved once at app creation."""
    db_path_absolute: str
    secret_backend_mode: Literal["auto", "keyring", "memory"]
    web_security: WebSecurityConfig

@dataclass
class CredentialRuntimeState:
    """Owned resources for the Credentials subsystem.

    Held in app.state but **never** serialized or sent to clients.
    """
    config: CredentialRuntimeConfig
    repository: SQLiteCredentialStore
    secret_store_router: SecretStoreRouter
    service: CredentialService
    keyring_available: bool           # 仅诊断 readiness
```

**绝对禁止**：

- 把 `SecretStore` 实例放进通用事件状态、Snapshot、Session DTO
- 把 API Key / secret_ref 放进 app.state 的可序列化字段
- 在 WebSocket event / SSE envelope / session snapshot / export markdown 中输出任何 Credentials 内部对象
- 把 `CredentialRuntimeState` 序列化或 pickle
- 在请求处理过程中重新解析 DB path（必须用 `config.db_path_absolute`）

app.state 可以持有 `CredentialRuntimeState` 实例（供 router 注入），但所有字段属于**后端内部状态**.

---

## 5. Lifespan 顺序

### 5.1 启动顺序（含 AsyncExitStack 部分初始化回滚——阻塞项 §五）

```text
1. 解析配置（PI_AGENT_SECRET_BACKEND / 绝对 DB path / WebSecurityConfig）
   → 任何配置错误（未知 backend / 相对 path）→ CredentialRuntimeConfigError
2. 创建 SecretStoreRouter（含 keyring 可用性探测）
3. 通过 AsyncExitStack 打开独立 SQLiteCredentialStore connection
4. initialize schema（schema 失败 → ExitStack 自动关 connection）
5. 创建 CredentialService（注入 repo + router + registries）
6. 把所有 owned resources 转移到 app.state.credentials
7. Web App ready
```

**部分初始化回滚语义**：

```python
async with AsyncExitStack() as stack:
    try:
        router = _build_router(secret_backend)
        repository = await stack.enter_async_context(
            _cm_open_credential_store(db_path)
        )
        # schema initialize 在 open() 内完成——失败会抛 CredentialsSchemaError
        service = CredentialService(...)
    except BaseException:
        # ExitStack 自动 close 已 enter 的资源（即 repository）
        # 不留下半初始化 CredentialRuntimeState
        raise
    # 全部成功 → 转移 ownership 到 app.state
    app.state.credentials = CredentialRuntimeState(
        config=config,
        repository=repository,
        secret_store_router=router,
        service=service,
        keyring_available=router._stores["keyring"] is not None,
    )
    # 防止 ExitStack 在 lifespan 末尾关闭已转移的资源
    stack.pop_all()
```

**关键不变量**：

- Step 1 配置错误**不**打开任何资源——直接抛
- Step 2 的 keyring 探测失败**不**抛——记录 `keyring_available=False` 后继续
- Step 3–4 schema 失败 → ExitStack 自动关 connection
- Step 5 不发任何网络请求（CredentialService 构造纯内存）
- Step 6 写入 app.state 前**所有**资源已就绪——不会出现半初始化 runtime

### 5.2 关闭顺序

```text
1. ASGI server 停止接受新请求（uvicorn 标准行为）
2. 现有 Credential API 请求自然完成（不实现请求计数器——非必要）
3. close SQLiteCredentialStore（owned connection）
4. 清空对 InMemorySecretStore 的运行时引用（GC）
```

**关键不变量**：

- `close()` 幂等——多次调用安全（测试覆盖）
- shutdown 路径**不**调用任何 SecretStore.delete（用户 secret 不应被应用关闭清理）
- API handler **不**创建脱离请求生命周期的后台 Credential task（避免 close 时仍有飞行中操作）

### 5.3 重启恢复语义

| storage_mode | 重启后 storage_status |
|---|---|
| `keyring` | Keyring 可用 → `ready`；不可用 → `backend_unavailable` |
| `session_only` | `needs_key`（内存丢失） |
| `env` | 当前环境变量存在 → `ready`；不存在 → `needs_key` |

`storage_status` 由 `resolve_storage_status(record, store)` 运行时派生（E1-2 已实现），重启后无需额外操作.

`validation_status` 持久化在 SQLite——重启后保留.

---

## 6. 启动降级（阻塞项 §六 readiness/degraded 定稿）

### 6.1 失败情况分类

#### 阻塞启动（抛 + app 退出）

- 未知 `PI_AGENT_SECRET_BACKEND`（非 auto/keyring/memory）
- Credential schema version 高于支持版本（防 downgrade）
- Schema v1 损坏（缺表 / 列 / 索引 / CHECK）
- 数据库路径不可创建或不可写（PermissionError / OSError）
- Repository 初始化失败（aiosqlite 连接错误）
- 相对 DB path（违反 §3.2）

#### 不阻塞启动（keyring 缺失但 session/env 可用）

- Keyring 包未安装（`pip install keyring` 未做）
- 系统无 Keyring backend（Linux 无 Secret Service / Windows Credential Manager 异常）
- InMemory Store 为空（首次启动）
- 某个 Env Credential 对应变量缺失（运行时 storage_status=needs_key）

### 6.2 Readiness 字段（app 级 vs credentials 子系统级）

应用全局 `status` 与 credentials 子系统 `status` 分开——**关键不变量**：

> 应用全局 `status` 在 keyring 缺失时仍为 `ready`——否则容器 / 开发脚本会因可选 Keyring 功能不可用而不断重启整个聊天服务.

```json
{
  "status": "ready",
  "credentials": {
    "status": "ready",
    "configured_backend": "auto",
    "keyring_available": true,
    "schema_version": 1
  }
}
```

Keyring 不可用但 session/env 仍可用：

```json
{
  "status": "ready",
  "credentials": {
    "status": "degraded",
    "configured_backend": "auto",
    "keyring_available": false,
    "schema_version": 1
  }
}
```

Schema 损坏（不会真的进入运行态——启动失败）：

```json
{
  "status": "starting",
  "credentials": {
    "status": "schema_error"
  }
}
```

### 6.3 Readiness endpoint（可选）

P1-E1-4 可暴露 `/api/health` 或复用现有 readiness endpoint. 若暴露，必须：

- 不返回 secret_ref / fingerprint / 任何 secret 字段
- 不调用 SecretStore.get（避免每次 readiness 探测都打开 backend）
- 字段固定为 §6.2 列举的安全集合

未在 §8 endpoints 清单中暴露 `/api/health`——若需要作为 E1-4A 验收手段，可在 router 中加最小 readiness 路径，但**不**算入"8 个 credentials endpoint".

---

## 7. Localhost Web Security Boundaries（阻塞项 §一）

Credentials API 没有用户认证. 任意能向 localhost 端口发请求的实体（恶意网页 / 浏览器扩展 / DNS rebinding / 配置错误的 CORS）都可能操作用户 Credential. 同源策略**不够**——必须叠加多层防护.

### 7.1 Host allowlist

使用等价 `TrustedHostMiddleware` 的校验. 默认仅允许：

```text
localhost
127.0.0.1
[::1]
+ 显式配置的本地开发 Host
```

**关键不变量**：

- 拒绝任意公网 Host
- 拒绝 DNS rebinding 风格 Host（如 `evil.com` 解析到 127.0.0.1）
- Host 校验按 **hostname** 处理（开发环境含端口时只比 hostname）
- 默认拒绝 `Host: null` / 缺失 / 空

### 7.2 Origin 校验

以下 6 个端点接触秘密，必须要求同源：

```text
POST   /api/provider-hints
POST   /api/credentials
PATCH  /api/credentials/{credential_id}
PUT    /api/credentials/{id}/secret
DELETE /api/credentials/{credential_id}
POST   /api/credentials/{id}/validate
```

允许：

- `Origin` 与 `WebSecurityConfig.allowed_ui_origins` 精确匹配（scheme + host + port 三者都对）
- 无 `Origin` 的受信任非浏览器本地调用（按配置决定是否允许，默认拒绝）

拒绝：

- `Origin: null`
- 任意外部站点
- 仅 host 相似但 scheme/port 不一致
- `Origin` 缺失且配置要求浏览器路径（默认）

### 7.3 自定义请求头

所有修改或接触 Secret 的请求（§7.2 列举的 6 个 endpoint）必须携带：

```text
X-PI-Agent-UI: 1
```

前端未来统一携带该 header. 外部网页无法在不经过 CORS preflight 的情况下静默发送自定义 header——CORS preflight 会失败（因为外部 origin 不在 allowlist）.

GET `/api/provider-definitions` 与 GET `/api/credentials` 不强制要求该 header（只读 + 已要求 Host/Origin）.

### 7.4 CORS

- **禁止** `Access-Control-Allow-Origin: *`
- 仅允许 `WebSecurityConfig.allowed_ui_origins` 中的精确 origin
- 禁止 `Access-Control-Allow-Credentials: true` 配 `Allow-Origin: *`
- preflight (`OPTIONS`) 必须响应固定 `Access-Control-Allow-Headers`（含 `X-PI-Agent-UI` / `Content-Type`），不回显请求的 `Access-Control-Request-Headers`

### 7.5 中间件实现位置

新建 `web/credentials_security.py`：

```python
def install_credentials_security_middleware(
    app: FastAPI,
    *,
    config: WebSecurityConfig,
) -> None:
    """Install TrustedHost + Origin + body-size middleware.

    Order (outermost → innermost):
        1. TrustedHost (reject before routing)
        2. Body-size limit (reject before parsing)
        3. Origin + X-PI-Agent-UI (per-route enforcement)
    """
    ...
```

### 7.6 Pydantic 错误脱敏（阻塞项 §三）

Pydantic 默认 ValidationError 包含 `input` / `ctx` / `url` / 原始 `msg`. 这些字段可能含 secret_value 或自定义异常文本——必须脱敏.

**固定安全错误结构**——不直接返回原始 `exc.errors()`：

```json
{
  "error": {
    "code": "request_validation_failed",
    "message": "The request body is invalid.",
    "fields": [
      {
        "path": "secret_value",
        "code": "value_error"
      }
    ]
  }
}
```

只投影：

- `loc` / `path`（JSON path——不含值）
- 受控 `code`（type 简化到固定枚举：`value_error` / `type_error` / `missing` / `extra_forbidden` / `too_long`）

**禁止返回**：

- `input`（原始输入值）
- `ctx`（自定义 validator 的 context 可能含原始异常）
- `url`（Pydantic doc URL——暴露内部 pydantic 版本）
- 原始 `msg`（自定义 validator 可能把 secret 放进 message）
- 原始异常对象 / `__cause__`

**Handler 范围限制**（避免影响其他 API）：

- Credential Router 路径下的 ValidationError → 用安全 handler
- 其他路径 → 保持现有 handler（不修改既有 API 的 422 schema）
- 实现：在 Credential Router 上加 `@router.exception_handler(RequestValidationError)` 或依赖注入做错误转换
- 必须加现有 Web API 回归测试，确认其他 endpoint 422 schema 未变

---

## 8. REST API（8 endpoints）

### 8.1 路由清单

| Method | Path | 用途 | 安全约束 |
|---|---|---|---|
| `GET` | `/api/provider-definitions` | 列内置 provider | Host allowlist |
| `POST` | `/api/provider-hints` | 本地 hint 探测 | Host + Origin + `X-PI-Agent-UI` |
| `GET` | `/api/credentials` | 列当前用户凭证 | Host allowlist |
| `POST` | `/api/credentials` | 创建凭证 | Host + Origin + `X-PI-Agent-UI` + 32 KiB |
| `PATCH` | `/api/credentials/{credential_id}` | 改 label | Host + Origin + `X-PI-Agent-UI` + 32 KiB |
| `PUT` | `/api/credentials/{credential_id}/secret` | rotate | Host + Origin + `X-PI-Agent-UI` + 32 KiB |
| `DELETE` | `/api/credentials/{credential_id}` | 删除 | Host + Origin + `X-PI-Agent-UI` |
| `POST` | `/api/credentials/{credential_id}/validate` | 远端验证 | Host + Origin + `X-PI-Agent-UI` + 32 KiB |

**不增加**：

- `GET /api/credentials/{credential_id}`——列表 DTO 已足够

### 8.2 GET /api/provider-definitions

响应：

```json
[
  {
    "id": "anthropic",
    "display_name": "Anthropic",
    "api_style": "anthropic_compatible",
    "validation_supported": true,
    "supports_model_listing": true
  },
  {
    "id": "glm",
    "display_name": "Zhipu GLM (Anthropic-compatible)",
    "api_style": "anthropic_compatible",
    "validation_supported": false,
    "supports_model_listing": false
  }
]
```

**禁止返回**：`credential_validation_endpoint` / `credential_validation_strategy` / `key_prefix_hints`.

### 8.3 POST /api/provider-hints

请求（§9 DTO）：

```json
{
  "secret_value": "sk-ant-..."
}
```

响应：

```json
{
  "candidates": ["anthropic"],
  "confidence": "high",
  "reason_code": "prefix_match_sk_ant"
}
```

**绝对禁止**：

- 记录 request body
- 持久化 Key
- 调用网络
- 调用 SecretStore
- 生成 Credential
- 在响应中返回 masked Key

调用 `detect_provider_hint(secret_value)`（E1-1 纯本地函数）→ 投影为安全响应.

### 8.4 POST /api/credentials（Create）

请求 DTO（严格——extra fields 拒绝）：

```json
{
  "label": "Work Key",
  "storage_mode": "keyring",
  "secret_value": "..."
}
```

Env：

```json
{
  "label": "GLM Env",
  "storage_mode": "env",
  "env_var_name": "GLM_API_KEY"
}
```

**必须拒绝**（Pydantic 422 → 安全脱敏后响应）：

- `{storage_mode: "env", secret_value: "...", env_var_name: "..."}`——env 禁止 secret_value
- `{storage_mode: "keyring", env_var_name: "..."}`——keyring 禁止 env_var_name
- 缺 label / label 全空白
- `env_var_name` 不匹配 `^[A-Za-z_][A-Za-z0-9_]*$`
- extra fields（`fingerprint` / `id` / `created_at` 等）

### 8.5 PATCH /api/credentials/{credential_id}

```json
{
  "label": "New label"
}
```

只允许 `label`. extra fields 拒绝.

### 8.6 PUT /api/credentials/{credential_id}/secret（Rotate）

Keyring / session_only：

```json
{
  "secret_value": "new-secret"
}
```

Env：

```json
{
  "env_var_name": "NEW_API_KEY_VAR"
}
```

**禁止**：改变 storage_mode.

### 8.7 DELETE /api/credentials/{credential_id}

返回（spec §10）：

```json
{
  "credential_id": "cred-...",
  "deleted": true,
  "warnings": []
}
```

`warnings` 当前可能包含：`"old_secret_cleanup_failed"`.

### 8.8 POST /api/credentials/{credential_id}/validate

请求：

```json
{
  "provider_id": "anthropic"
}
```

**禁止接受**：`base_url` / `endpoint` / `model` / `headers` / `provider_hint`.

响应（spec §8）：

成功（Anthropic）：

```json
{
  "credential_id": "cred-...",
  "provider_id": "anthropic",
  "attempted": true,
  "valid": true,
  "error_code": null,
  "validation_status": "valid",
  "last_validated_at": 1784...
}
```

GLM unsupported：

```json
{
  "credential_id": "cred-...",
  "provider_id": "glm",
  "attempted": false,
  "valid": null,
  "error_code": "validation_not_supported",
  "validation_status": "never_validated",
  "last_validated_at": null
}
```

Secret missing：

```json
{
  "attempted": false,
  "valid": null,
  "error_code": "credential_missing"
}
```

**non-attempted outcome 是合法业务结果——返回 200，不是 4xx**.

---

## 9. Request DTO（含 SecretStr + OpenAPI 约束）

### 9.1 SecretStr 用法（阻塞项 §七）

请求模型中 `secret_value` 使用 `pydantic.SecretStr`：

```python
from pydantic import BaseModel, SecretStr, Field, ConfigDict

class CreateCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    storage_mode: Literal["keyring", "session_only", "env"]
    secret_value: SecretStr | None = None
    env_var_name: str | None = None
```

**关键不变量**：

- Service 调用前只在**局部变量**中执行 `secret_value.get_secret_value()`
- 不把 SecretStr 放进异常 / 日志 / repr
- OpenAPI 中标记 `writeOnly: true`
- 不提供真实格式的 `example`（用 `"***"` 或省略）
- 不设置默认 Key
- 响应模型**绝不**复用请求模型
- `repr(request_dto)` 不得输出 Secret（SecretStr 默认 `SecretStr('**********')`）
- 测试失败信息不得输出 Secret（pytest `assert repr(req) == ...` 用 SecretStr 的 mask 形式）

SecretStr 是**额外防线**——不替代 §7.6 Pydantic 脱敏、§10 body-size 限制.

### 9.2 OpenAPI 约束

`/openapi.json` 中：

- `secret_value` 字段：`writeOnly: true` + 不提供 example
- 不暴露 `CredentialRecord` / `CredentialRuntimeState` / `WebSecurityConfig` 等内部模型
- 路径参数 `credential_id` 用 `str` pattern（不暴露内部 ID 生成规则）
- 错误响应统一用 §10.2 的 `ErrorResponse` 模型

### 9.3 字段长度（同 §10）

- `label`: 1–128 Unicode characters
- `secret_value`: 1–8192 UTF-8 bytes
- `env_var_name`: `^[A-Za-z_][A-Za-z0-9_]*$`

---

## 10. 请求体大小限制（阻塞项 §二）

### 10.1 32 KiB 总 body 上限

字段级 `secret_value ≤ 8192 UTF-8 bytes` 只能在 JSON 解析后生效——攻击者可发几十 MB 的 JSON body，服务器会先读取和解析，再报字段过长. 必须**在流式读取层**累计字节.

Credential API 路由统一限制：

```text
最大 HTTP body: 32 KiB
```

覆盖：

- Create / Rotate / Validate / Provider Hint / Patch label
- DELETE 通常无 body——但若客户端发了 body，仍受同一上限保护

### 10.2 超限响应

```text
HTTP 413 Payload Too Large
```

```json
{
  "error": {
    "code": "request_body_too_large",
    "message": "The request body exceeds the allowed size."
  }
}
```

**禁止**：在错误响应中返回 body 片段.

### 10.3 必须同时防护

- `Content-Length` 已知且超限 → 413（短路径，不读 body）
- chunked body（`Transfer-Encoding: chunked`）→ 在流式 read 时累计字节，超限即 413
- 伪造较小 `Content-Length` 但实际发更多字节 → 实际 read 时累计超限即 413
- 缺失 `Content-Length`（某些客户端会省略）→ 按实际读取累计

**不能只检查 Header**——必须在 ASGI `receive()` event 流中实际累计 `body` chunk size.

### 10.4 实现位置

`web/credentials_security.py` 提供中间件或 route dependency：

```python
class _BodySizeLimit:
    """Wrap ASGI receive() to count body bytes; abort at limit."""

    def __init__(self, receive: Callable, max_bytes: int) -> None: ...
    async def __call__(self) -> Message: ...  # raises _BodyTooLargeError
```

或用 FastAPI route dependency 在 endpoint 入口前消费 body（更简单，但要看 FastAPI/Starlette 版本支持）.

---

## 11. 安全响应 DTO

### 11.1 Credential response（list / create / patch / rotate / delete）

```json
{
  "credential_id": "cred-...",
  "label": "Work Key",
  "storage_mode": "keyring",
  "storage_status": "ready",
  "masked_value": "sk-****5678",
  "provider_hint": "anthropic",
  "provider_hint_confidence": "high",
  "validation_status": "valid",
  "last_validated_provider_id": "anthropic",
  "last_validated_at": 1784...,
  "last_error_code": null,
  "created_at": 1784...,
  "updated_at": 1784...
}
```

**绝对禁止返回**：

- `secret_value`（明文 Key）
- `secret_ref`
- `fingerprint_sha256`
- `Authorization` header / `Bearer` token
- `raw_error`（任何 raw response / exception）
- database row
- Keyring service name（如 `pi-agent-core-py`）
- environment variable value（env Credential 的 `masked_value` 用 `ENV[NAME]` 形式，不返回环境变量的当前值）

`storage_status` 来自 `resolve_storage_status()`（运行时派生，不持久化）.

### 11.2 Env Credential 特殊处理

```json
{
  "storage_mode": "env",
  "masked_value": "ENV[GLM_API_KEY]"
}
```

环境变量**名**是已知配置引用，可以返回；环境变量**值**永远不返回.

### 11.3 Validation response

见 §8.8. 7 字段固定 DTO——不含 raw response / HTTP status / endpoint.

---

## 12. HTTP 错误映射

### 12.1 固定映射

| Domain Error | HTTP | error.code |
|---|---|---|
| TrustedHost 拒绝 | 403 | `invalid_host` |
| Origin 拒绝 / 缺失 / `null` | 403 | `invalid_origin` |
| 缺 `X-PI-Agent-UI` header | 400 | `missing_ui_header` |
| Body 超过 32 KiB | 413 | `request_body_too_large` |
| Pydantic ValidationError（credential router 内） | 422 | `request_validation_failed` |
| `CredentialInputError`（输入组合非法） | 422 | `invalid_input` |
| `CredentialNotFoundError` | 404 | `credential_not_found` |
| `CredentialAlreadyExistsError` / `CredentialSecretRefConflictError` | 409 | `credential_conflict` |
| `CredentialOperationConflictError`（CAS） | 409 | `credential_conflict` |
| `CredentialBackendUnavailableError` | 503 | `credential_backend_unavailable` |
| `CredentialSecretWriteError` / `CredentialSecretDeleteError` | 503 | `credential_backend_unavailable` |
| `CredentialCompensationError` | 500 | `credential_internal_error` |
| `CredentialServiceError`（generic） | 500 | `credential_internal_error` |
| `CredentialsSchemaError` | 503 | `credential_schema_error` |

### 12.2 错误响应格式

```json
{
  "error": {
    "code": "credential_backend_unavailable",
    "message": "The selected credential storage backend is unavailable."
  }
}
```

**固定 message 映射**——按 `error.code` 查表（runtime 配置在 `credentials_api.py` 顶部）.

**禁止**：

- `str(exc)` 进 message
- `repr(exc)` 进 message
- traceback 进 response
- raw SQLite error / raw Keyring error 进 response
- 异常类型名进 response（除安全错误码外）
- `__cause__` 链进 response

### 12.3 Non-attempted validation outcomes

GLM unsupported / unknown provider / credential missing → **200 + non-attempted DTO**（§8.8）.

不是错误——业务正常路径.

---

## 13. 日志与异常 hygiene

### 13.1 Access log

- **禁止**：记录 request body
- **禁止**：记录 `Authorization` / `x-api-key` / `X-PI-Agent-UI` 值（header 存在与否可记，但值不记）
- **禁止**：记录 `secret_value` 字段
- **允许**：method / path / status / duration / request_id / Origin（值）

FastAPI / uvicorn 默认不记录 body——保持默认. 如启用 structured logging，需显式 redact secret-bearing 字段.

### 13.2 异常 handler

```python
@app.exception_handler(CredentialServiceError)
async def _credential_service_error_handler(request, exc):
    log.warning(
        "credential service error",
        code=map_to_error_code(exc),
        credential_id=getattr(exc, "credential_id", None),  # safe
    )
    return JSONResponse(...)
```

**禁止**：

- 异常 handler 记录 request JSON
- 异常 handler 记录 `str(exc)` / `repr(exc)`
- 异常 handler 遍历 `__cause__` 链记录
- 测试 client failure 时打印 body
- FastAPI debug 模式在生产路径返回 traceback（必须关闭 debug）

### 13.3 必测的攻击场景

- `secret_value` 类型错误（int / list / null）
- extra field（如 `{"secret_value": "...", "fingerprint": "..."}`）
- 超长 Key（> 8192 bytes）
- 超长 body（> 32 KiB）
- 非法 UTF-8 / 控制字符（NUL / 换行）

所有路径的错误响应都**不能**回显原值.

---

## 14. 测试矩阵（共 68 项）

### 14.1 Lifespan / Runtime（E1-4A，10 项）

1. `auto` + Keyring available → 正常启动
2. `auto` + Keyring unavailable → 应用启动，keyring_available=False
3. `memory` backend → keyring slot 为 None
4. Env credential 创建后重启 env 变量存在 → storage_status=ready
5. session_only credential 创建后重启 → storage_status=needs_key
6. Keyring credential 创建后重启（keyring 可用）→ storage_status=ready
7. Schema initialize from fresh DB → version=1
8. Shutdown 关闭 owned connection（再 open 仍可读）
9. Keyring unavailable 不阻塞 App 启动
10. 坏 schema（version>1）→ 启动失败抛 SchemaError

### 14.2 API CRUD（E1-4B，11 项）

11. Create keyring credential（200）
12. Create session_only credential（200）
13. Create env credential（200）
14. Create 非法字段组合（env+secret_value / keyring+env_var_name）→ 422
15. List credentials（200，按 updated_at DESC）
16. Update label（200）
17. Rotate keyring / env（200）
18. Delete credential（200 + warnings）
19. Duplicate id / secret_ref → 409
20. Concurrent modification → 409
21. Storage status 实时变化（delete secret 后再 GET → needs_key / backend_unavailable）

### 14.3 Validation（E1-4B，8 项）

22. Anthropic success（attempted=true / valid=true / validation_status=valid）
23. Anthropic 401（attempted=true / valid=false / validation_status=invalid）
24. GLM unsupported（attempted=false / error_code=validation_not_supported）
25. Unknown provider（attempted=false / error_code=provider_not_supported）
26. Secret missing（attempted=false / error_code=credential_missing）
27. CAS conflict（concurrent rotate → 409 credential_conflict）
28. Backend unavailable（503 credential_backend_unavailable）
29. 不读取 Provider Hint 自动验证（hint=anthropic 但调用方传 glm 走 unsupported 路径）

### 14.4 Security（E1-4B，16 项）

使用 `SECRET_MARKER = PI_E1_SECRET_MARKER_7F3A91D2`：

30. Request body marker 不进 access log（caplog）
31. REST JSON response 不含 marker（create / list / patch / rotate / delete / validate）
32. HTTP error body 不含 marker（all error paths）
33. SQLite 文件字节不含 marker
34. WebSocket event buffer 不含 marker
35. Session / Snapshot 不含 marker
36. Export Markdown 不含 marker
37. `secret_ref` 不返回（任何 endpoint）
38. `fingerprint_sha256` 不返回（任何 endpoint）
39. Raw provider response 不返回（validation endpoint 只回 7 字段 DTO）
40. Access log 不含 `Authorization`
41. OpenAPI schema 不暴露 internal models（`CredentialRecord` 不出现在 `/openapi.json`）
42. Pydantic ValidationError 不回显 `secret_value` 输入
43. Extra field 422 不回显 input
44. 超长 secret_value 422 不回显 input
45. 非法 UTF-8 / 控制字符 422 不回显 input

### 14.5 Local Web Security（E1-4B，8 项）

46. 外部 Origin 创建 Credential 被拒绝（403 invalid_origin）
47. 外部 Origin 调用 provider-hints 被拒绝
48. `Origin: null` 被拒绝
49. 缺少 `X-PI-Agent-UI` 被拒绝（400 missing_ui_header）
50. 非法 Host 被拒绝（403 invalid_host）
51. 允许的 localhost origin 成功
52. Wildcard CORS 不存在（响应不含 `Access-Control-Allow-Origin: *`）
53. DNS rebinding 风格 Host 被拒绝（Host: evil.com 但解析到 127.0.0.1）

### 14.6 Body / Validation（E1-4B，9 项）

54. Content-Length 超限返回 413（短路径，不读 body）
55. Chunked body 超限返回 413（流式累计）
56. 伪造较小 Content-Length 仍被实际计数拒绝
57. Pydantic `input` 不返回
58. Pydantic `ctx` 不返回
59. 自定义 validator 异常不泄漏（message / 类型名）
60. `SecretStr repr` 不泄漏（`repr(request_dto)` 不含 marker）
61. OpenAPI 标记 `secret_value` 为 `writeOnly`
62. OpenAPI 不包含 `secret_value` example

### 14.7 Lifespan / Path（E1-4A，6 项）

63. 不同 cwd 使用相同绝对 DB（启动两次，cwd 不同，写入同一文件）
64. Schema init 失败后 owned connection 被关闭（文件未被锁）
65. `app.state.credentials` 不留下半初始化 runtime（启动中途失败时为 None 或完整）
66. Unknown backend 配置安全失败（抛 CredentialRuntimeConfigError，不静默降级）
67. Keyring degraded 时全局 readiness 仍为 `ready`
68. Shutdown `close()` 幂等（多次调用安全）

---

## 15. 实施顺序

### 15.1 E1-4A——Composition Root + Lifespan（先做）

**交付**：

- `src/pi_agent_core_py/web/credentials_runtime.py`：
  - `build_credential_runtime()` + `close_credential_runtime()`
  - `CredentialRuntimeConfig` + `WebSecurityConfig`
  - 绝对 DB path 解析（`_resolve_db_path`）
  - `AsyncExitStack` 部分初始化回滚
- `src/pi_agent_core_py/web/state.py`：加 `CredentialRuntimeState` + `CredentialRuntimeConfig`
- `src/pi_agent_core_py/web/credentials_security.py`（仅 install TrustedHost middleware + 配置 allowed origins——body limit / Origin enforcement 在 E1-4B）
- `src/pi_agent_core_py/web/app.py`：lifespan initialize / close 接入 + TrustedHost middleware 安装
- 测试：`tests/test_credentials_runtime_lifespan.py`（§14.1 + §14.7 共 16 项）

**验收**：

- §14.1（10 项）+ §14.7（6 项）全 PASS
- 应用启动 + 关闭 + 重启语义可证
- 不暴露任何 REST endpoint（API 在 E1-4B）

### 15.2 E1-4B——Credential REST API（后做）

**交付**：

- `src/pi_agent_core_py/web/credentials_api.py`：
  - FastAPI router（8 endpoints）
  - DTO（`SecretStr` + `writeOnly` + extra forbidden）
  - 32 KiB body limit route dependency
  - Origin + `X-PI-Agent-UI` enforcement route dependency
  - 安全 ValidationError handler（仅本 router）
  - 固定 Domain Error → HTTP 映射
- `src/pi_agent_core_py/web/credentials_security.py`：扩展——32 KiB body limit + Origin/header enforcement + 安全 ValidationError handler
- `src/pi_agent_core_py/web/serializers.py`：加 `serialize_credential_view` / `serialize_validation_result`
- `src/pi_agent_core_py/web/app.py`：`include_router(credentials_router)`
- 测试：
  - `tests/test_credentials_api_crud.py`（§14.2）
  - `tests/test_credentials_api_validation.py`（§14.3）
  - `tests/test_credentials_api_security.py`（§14.4）
  - `tests/test_credentials_api_web_security.py`（§14.5）
  - `tests/test_credentials_api_body_validation.py`（§14.6）
  - 既有 API 422 schema 回归测试（验证 handler 范围限制不影响其他 endpoint）

**验收**：

- §14.2–14.6 全 PASS（共 43 项 + §14.1/14.7 已 PASS 16 项 = 59 项；加 §14.4 安全 16 项 = 75；再加现有 1541 baseline）
- 完整离线测试无回归（≥1597 + 68 新增）
- 真实外部网络调用 = 0（validation 测试用 httpx.MockTransport）

---

## 16. 不变量回顾

实现 + 测试期间，下列不变量必须保持：

1. **Key 永不离开受信任边界**——不出现在 SQLite 明文 / REST response / WS event / SSE envelope / snapshot / log / export markdown / 前端持久化 / exception str/repr
2. **Provider 必须显式指定**——validation endpoint 不接受 base_url / endpoint / model / headers
3. **Keyring 不可用不阻塞 App**——session_only / env 始终可用；app 全局 `status` 保持 `ready`
4. **Non-attempted validation outcome 是 200**——不是错误
5. **CAS conflict 是 409**——不是 5xx
6. **secret_value 不进 Pydantic error response**——必须显式 redact（只投影 `loc` + 受控 `code`）
7. **CredentialRuntimeState 不被序列化**——只活在 app.state
8. **DB path 一次性解析为绝对路径**——请求处理时不重新解析
9. **Lifespan 失败用 AsyncExitStack 回滚**——不留下半初始化 runtime
10. **Host allowlist + Origin 校验 + X-PI-Agent-UI + 32 KiB body limit**——4 层 localhost 安全边界，缺一不可
11. **CORS 禁止 `*`**——仅精确 UI origin
12. **Pydantic handler 范围限于 Credential Router**——不影响其他 API 的 422 schema
13. **不修改 FROZEN 模块**——credentials_store / credentials_service / credentials_errors / provider_validation / secrets/* / providers/registry.py
14. **不修改 Core Runtime**——loop / agent / context / events / stream_events / messages
15. **不修改前端**——E1-4 是纯后端 + REST

---

## 17. 待 user 决定

- ✅ 本设计已 APPROVED（2026-07-17）
- 启动 E1-4A 实施（基于本文档，仅 Composition Root + Lifespan + TrustedHost，不含 REST endpoint）
- E1-4A 完成后是否暂停审核，再启动 E1-4B
