# P1-E1 — Secure Credentials 实施计划

> **状态**：APPROVED — P1-E1 design frozen（2026-07-17）
> **阶段**：P1-E Multi-Provider 第 1 子阶段
> **基线**：master HEAD `1c2289d`
> **冻结依据**：见 [ROADMAP.md P1-E](../../ROADMAP.md) + 设计冻结备忘（2026-07-16/17 conversation history）

---

## 0. 审核修订记录

| 日期 | 修订 |
|---|---|
| 2026-07-16 | 初稿（DRAFT） |
| 2026-07-17 | **APPROVED**——补 6 项阻塞设计：① SecretStore/SQLite 补偿事务顺序 ② storage_status / validation_status 拆分 ③ fingerprint 仅内部使用 ④ validate 显式 provider target（Custom URL 移 P1-E2） ⑤ 固定错误码替代截断异常 ⑥ HTTP 不 retry / 不 redirect / 短超时 / 强制 HTTPS。新增 `POST /api/provider-hints`、`PUT /api/credentials/{id}/secret`、E1-1..E1-5 拆分。 |

## 1. 目标

让用户在本地安全保存多个 API Key，校验 Key 是否可用，**且 Key 永远不离开受信任边界**（不出现在 SQLite 明文 / REST response / WS event / snapshot / log / export markdown / 前端持久化 / exception str/repr）。

P1-E1 只做**凭证管理与验证**——不含模型目录、Session 绑定、请求执行切换、前端选择器、Regenerate 模型切换、OpenAI Adapter 完整实现、Markdown 面板、**Custom Base URL 验证**（移 P1-E2）。

## 2. 文件结构

```
src/pi_agent_core_py/
├── providers/                       # 已存在，D2 baseline 内
│   ├── __init__.py                  # ← 修改：export 新增类
│   ├── base.py                      # ❌ 不动
│   ├── errors.py                    # ❌ 不动（可加新异常子类，不动既有）
│   ├── fake.py                      # ❌ 不动
│   ├── glm.py                       # ❌ 不动
│   ├── anthropic_compat.py          # ❌ 不动
│   ├── registry.py                  # ✨ 新：ProviderDefinition 内置表 + detect_provider_hint
│   └── openai_compat.py             # ⏸ P1-E2 按需（不在 P1-E1）
├── secrets/                         # ✨ 新模块
│   ├── __init__.py
│   ├── base.py                      # SecretStore Protocol
│   ├── keyring_store.py             # OSKeyringSecretStore（asyncio.to_thread + lazy import keyring）
│   ├── memory_store.py              # InMemorySecretStore（测试 + keyring 不可用降级）
│   ├── env_store.py                 # EnvSecretStore（读 os.environ；不支持 set）
│   ├── factory.py                   # backend 探测 + 选择（PI_AGENT_SECRET_BACKEND 覆盖）
│   └── fingerprint.py               # mask_secret / fingerprint_secret 工具
└── web/
    ├── extension_store.py           # ❌ schema 不动（SCHEMA_VERSION 仍为 2）
    ├── credentials_store.py         # ✨ 新：CredentialRecord 表 + repository（独立 schema_meta）
    ├── credentials_service.py       # ✨ 新：create/update/rotate/delete 补偿事务 + validate 调度
    ├── credentials_api.py           # ✨ 新：FastAPI router（8 endpoint）
    ├── provider_validation.py       # ✨ 新：built-in provider 验证 client（httpx + 固定 timeout/redirect）
    ├── app.py                       # ← 仅新增 router include + lifespan 接入 SecretStore
    └── serializers.py               # ← 加 credential / validation 序列化函数
```

**禁止修改**：`loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py` / `providers/base.py` / `providers/glm.py` / `providers/anthropic_compat.py`。

## 3. 数据模型

### 3.1 ProviderDefinition（providers/registry.py）

代码内置的静态定义，不含用户秘密。

```python
@dataclass(frozen=True)
class ProviderDefinition:
    id: str                                    # e.g. "glm", "anthropic", "openai-compat", "custom"
    display_name: str                          # e.g. "Zhipu GLM"
    api_style: Literal["anthropic_compatible", "openai_compatible", "custom"]
    default_base_url: str | None               # P1-E1 必须非 None（Custom 移 P1-E2）
    key_hints: tuple[str, ...]                 # 用于 local heuristic——见 §6
    supports_model_listing: bool
    validation_endpoint: str                   # P1-E1 冻结：固定 HTTPS endpoint，不接受 user override
    capabilities: Mapping[str, bool | int | None]  # 默认能力，可被 model_catalog 覆盖
```

**初始内置表**（4 项，但 P1-E1 只允许验证前 3 项；custom 留 P1-E2）：

| id | display_name | api_style | default_base_url | key_hints | supports_model_listing | validation_endpoint |
|---|---|---|---|---|---|---|
| `glm` | Zhipu GLM | `anthropic_compatible` | `https://open.bigmodel.cn/api/paas/v4` | `("glm-",)`, prefix `sk-` 长度 ≥ 30 | True | `https://open.bigmodel.cn/api/paas/v4/models` |
| `anthropic` | Anthropic | `anthropic_compatible` | `https://api.anthropic.com` | prefix `sk-ant-` | True | `https://api.anthropic.com/v1/models` |
| `openai-compatible` | OpenAI Compatible | `openai_compatible` | None | prefix `sk-` | True | **P1-E1 不支持**——base_url 未知，留 P1-E2 profile |
| `custom` | Custom Endpoint | `custom` | None | `()` | False | **P1-E1 不支持**——留 P1-E2 profile |

**key_hints 仅用于本地格式匹配——不触发网络请求**（见 §6）。

### 3.2 SecretStore（secrets/base.py）

```python
class SecretStore(Protocol):
    async def is_available(self) -> bool: ...
    async def set(self, secret_id: str, value: str) -> None: ...
    async def get(self, secret_id: str) -> str | None: ...
    async def delete(self, secret_id: str) -> bool: ...  # 返回是否确实删除（False = 不存在或不支持）
    def backend_name(self) -> str: ...  # "keyring" / "memory" / "env"
```

**`secret_id` 约定**：`"cred-{uuid}"`——全局唯一，不含 Key 片段。

### 3.3 三实现

#### OSKeyringSecretStore（secrets/keyring_store.py）

```python
try:
    import keyring  # type: ignore
except ImportError:
    keyring = None  # type: ignore

class OSKeyringSecretStore:
    SERVICE_NAME = "pi-agent-core-py"

    def __init__(self) -> None:
        if keyring is None:
            raise RuntimeError("keyring not installed")

    async def is_available(self) -> bool:
        # 必须实际探测 backend——不只看模块是否 import 成功
        # 识别 fail backend / null backend / 实际不可写 backend
        try:
            backend = await asyncio.to_thread(keyring.get_keyring)
            return backend is not None and type(backend).__name__ != "FailKeyring"
        except Exception:
            return False

    async def set(self, secret_id: str, value: str) -> None:
        await asyncio.to_thread(
            keyring.set_password, self.SERVICE_NAME, secret_id, value
        )

    async def get(self, secret_id: str) -> str | None:
        return await asyncio.to_thread(
            keyring.get_password, self.SERVICE_NAME, secret_id
        )

    async def delete(self, secret_id: str) -> bool:
        try:
            await asyncio.to_thread(keyring.delete_password, self.SERVICE_NAME, secret_id)
            return True
        except keyring.errors.PasswordDeleteError:
            return False
```

**所有 keyring 调用必须经 `asyncio.to_thread`**——keyring 是同步 API，直接在事件循环中调用会阻塞。

#### InMemorySecretStore（secrets/memory_store.py）

进程内存 dict——测试和 keyring 不可用降级用。**不持久化**——server restart 后所有 keyring/memory 模式 credential 进 `needs_key` 状态（不能误报 `ready`）。

```python
class InMemorySecretStore:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def is_available(self) -> bool:
        return True

    async def set(self, secret_id: str, value: str) -> None:
        self._store[secret_id] = value

    async def get(self, secret_id: str) -> str | None:
        return self._store.get(secret_id)

    async def delete(self, secret_id: str) -> bool:
        return self._store.pop(secret_id, None) is not None

    def backend_name(self) -> str:
        return "memory"
```

#### EnvSecretStore（secrets/env_store.py）

```python
class EnvSecretStore:
    """读 os.environ——用户配置时提供 env var name，不存 Key 本体。
    不支持 set()（环境变量不能由应用写入）。
    删除 Credential 不删除环境变量。"""

    def __init__(self) -> None:
        pass  # 无需初始化参数——env var name 在 CredentialRecord.secret_ref

    async def is_available(self) -> bool:
        return True  # os.environ 总是存在

    async def set(self, secret_id: str, value: str) -> None:
        raise NotImplementedError("EnvSecretStore does not support set()")

    async def get(self, secret_id: str) -> str | None:
        # secret_id 在此实现下 = env var name
        return os.environ.get(secret_id)

    async def delete(self, secret_id: str) -> bool:
        # 不修改用户环境变量——返回 True 表示 SQLite 引用可删
        return True

    def backend_name(self) -> str:
        return "env"
```

### 3.4 CredentialRecord（SQLite + 运行时派生 storage_status）

```python
CredentialStorageMode = Literal["keyring", "session_only", "env"]
# 注："session_only" 是 InMemory 的对外名称（语义清晰：进程结束即失）

CredentialValidationStatus = Literal[
    "never_validated",
    "valid",
    "invalid",   # 远端明确认证失败
    "error",     # 网络失败、超时或协议错误
]

CredentialStorageStatus = Literal[
    "ready",               # SecretStore 中存在可读取的秘密
    "needs_key",           # SQLite 记录存在但秘密读不到（restart 后内存丢失 / env 未设置）
    "backend_unavailable", # 当前 SecretStore 不可用（如 keyring backend 失效）
]

@dataclass(frozen=True)
class CredentialRecord:
    id: str
    label: str
    storage_mode: CredentialStorageMode
    secret_ref: str                    # keyring: cred-{uuid}；env: env var name

    masked_value: str                  # "sk****8A31" / "********" 短 key
    fingerprint_sha256: str | None     # 内部去重用——绝不返回前端

    provider_hint: str | None
    provider_hint_confidence: str | None   # "high" / "medium" / "low" / "unknown"

    validation_status: CredentialValidationStatus
    last_validated_provider_id: str | None
    last_validated_at: int | None          # ms epoch
    last_error_code: str | None            # CredentialValidationErrorCode（不是 str(exception)）

    created_at: int                        # ms epoch
    updated_at: int
```

**关键决策**：

1. **`storage_status` 不持久化**——运行时由 `(record, current SecretStore)` 实时计算：
   ```python
   async def resolve_storage_status(rec: CredentialRecord, store: SecretStore) -> CredentialStorageStatus:
       if not await store.is_available():
           return "backend_unavailable"
       secret = await store.get(rec.secret_ref)
       return "ready" if secret is not None else "needs_key"
   ```

2. **`fingerprint_sha256` 仅内部使用**——绝不返回前端、不进 log、不进 WS、不进 snapshot、不进 export。

3. **`last_error_code` 替代 `last_safe_error`**——数据库只存固定错误码字符串，固定错误消息由 serializer 根据 code 生成（避免数据库长期保存第三方错误文本，也避免截断失败导致泄漏）。

4. **`storage_mode` 用 `"session_only"` 而非 `"memory"`**——对用户更清晰。

## 4. SQLite Schema（web/credentials_store.py）

**独立 schema 版本表**——专用名 `web_credentials_schema_meta`（**不**用通用名 `schema_meta`，避免与 extension_store 在同一 SQLite 中冲突）：

```sql
CREATE TABLE IF NOT EXISTS web_credentials_schema_meta (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

INSERT INTO web_credentials_schema_meta (id, version) VALUES (1, 1)
    ON CONFLICT(id) DO NOTHING;

CREATE TABLE IF NOT EXISTS web_credentials (
    id                          TEXT PRIMARY KEY,           -- "cred-{uuid}"
    label                       TEXT NOT NULL,
    storage_mode                TEXT NOT NULL
                                CHECK (storage_mode IN ('keyring', 'session_only', 'env')),
    secret_ref                  TEXT NOT NULL,              -- keyring: cred-{uuid}；env: env var name

    masked_value                TEXT NOT NULL,
    fingerprint_sha256          TEXT,                       -- 可空（env 模式可能无 Key 可 hash）

    provider_hint               TEXT,
    provider_hint_confidence    TEXT,

    validation_status           TEXT NOT NULL DEFAULT 'never_validated'
                                CHECK (validation_status IN
                                    ('never_validated', 'valid', 'invalid', 'error')),
    last_validated_provider_id  TEXT,
    last_validated_at           INTEGER,                    -- ms epoch；NULL 表示从未验证
    last_error_code             TEXT,                       -- CredentialValidationErrorCode

    created_at                  INTEGER NOT NULL,           -- ms epoch
    updated_at                  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_web_credentials_label ON web_credentials(label);
CREATE INDEX IF NOT EXISTS idx_web_credentials_provider_hint ON web_credentials(provider_hint);
```

**SCHEMA_VERSION = 1**（新表独立起始版本，**不**进 extension_store 的 SCHEMA_VERSION=2）。

**禁止列**：`api_key` / `authorization` / `bearer` / 任何明文 Key 容器——schema CHECK 约束不可强制语义，由 serializer + repository + service 三层保证。

**Fingerprint 算法**（secrets/fingerprint.py）：
```python
def fingerprint_sha256(api_key: str) -> str:
    """内部去重——绝不返回前端。"""
    h = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"sha256:{h}"
```

**masked_value 算法**：
```python
def mask_secret(api_key: str) -> str:
    if len(api_key) < 12:
        return "********"   # 短 key 全遮蔽
    return f"{api_key[:2]}****{api_key[-4:]}"
    # 例: "sk-abc...8A31" → "sk****8A31"
```

## 5. Repository（web/credentials_store.py）

复用 `extension_store` 的 connection（共享 `:memory:` 必须），但独立 schema_meta。

```python
class CredentialStore:
    def __init__(self, conn: sqlite3.Connection): ...

    async def insert(self, rec: CredentialRecord) -> None: ...
    async def list(self) -> list[CredentialRecord]: ...
    async def get(self, cred_id: str) -> CredentialRecord | None: ...
    async def update_label(self, cred_id: str, label: str) -> CredentialRecord: ...
    async def update_secret_ref(
        self, cred_id: str, *,
        new_secret_ref: str, new_masked: str, new_fingerprint: str | None,
    ) -> CredentialRecord:
        """原子切换 secret_ref——见 §6.2 rotate 流程。
        同时重置 validation_status='never_validated', last_validated_at=NULL, last_error_code=NULL。"""

    async def update_validation_status(
        self, cred_id: str, *,
        status: CredentialValidationStatus,
        provider_id: str | None = None,
        error_code: str | None = None,
    ) -> CredentialRecord: ...

    async def delete(self, cred_id: str) -> bool: ...
```

**事务边界**：每方法独立 `BEGIN IMMEDIATE`，与 `extension_store` 风格一致。

## 6. Service 补偿事务（web/credentials_service.py）

**核心约束**：SQLite 与系统 Keyring 不能进同一事务——必须固定操作顺序 + 失败补偿。

### 6.1 Create credential

```
1. 生成 credential_id 和 secret_ref = f"cred-{uuid4()}"
2. secret_store.set(secret_ref, api_key)
   └─ 失败 → 返回 backend_error（无 SQLite 副作用）
3. SQLite BEGIN IMMEDIATE → INSERT CredentialRecord → COMMIT
   └─ 失败 → best-effort secret_store.delete(secret_ref) → 返回 db_error
4. 返回 serialize_credential(rec)
```

**关键**：先 secret 后 SQLite——SQLite 失败时可回滚（删 secret）；反向则可能产生"SQLite 有 row 但 secret 没存"的虚假可用 credential。

### 6.2 Rotate secret（替换 Key）

**禁止**直接覆盖 `old_secret_ref`——SQLite 更新失败时旧 Key 永久丢失。

```
1. 生成 new_secret_ref = f"cred-{uuid4()}"
2. secret_store.set(new_secret_ref, new_api_key)
   └─ 失败 → 返回 backend_error（旧 Key 不受影响）
3. 读 rec 当前 old_secret_ref
4. SQLite BEGIN IMMEDIATE：
     UPDATE web_credentials SET
       secret_ref = new_secret_ref,
       masked_value = new_masked,
       fingerprint_sha256 = new_fingerprint,
       validation_status = 'never_validated',
       last_validated_at = NULL,
       last_error_code = NULL,
       updated_at = ?
     WHERE id = cred_id AND secret_ref = old_secret_ref  -- optimistic concurrency
   COMMIT
   └─ 失败 → best-effort secret_store.delete(new_secret_ref) → 返回 db_error（旧 Key 仍可用）
   └─ rowcount=0（他人已 rotate）→ best-effort secret_store.delete(new_secret_ref) → 返回 conflict
5. best-effort secret_store.delete(old_secret_ref)
   └─ 失败仅记日志——主操作已成功
```

### 6.3 Update label

简单 SQLite UPDATE，无 secret_store 调用。

### 6.4 Delete credential

```
1. 读 rec（确认存在）
2. secret_store.delete(rec.secret_ref)
   ├─ 成功 → SQLite DELETE → 返回 deleted=True
   ├─ 失败 + storage_mode=keyring → SQLite 不删，返回 backend_error（避免孤儿 secret）
   ├─ storage_mode=env → secret_store.delete 永远 True（EnvSecretStore 不动环境变量）
   │                  → SQLite DELETE → 返回 deleted=True
   └─ storage_mode=session_only → 内存中可能已丢（False）——仍 SQLite DELETE（idempotent）
3. SQLite DELETE 失败 → 返回 db_error（secret 已删但 row 还在——可重试 delete）
```

### 6.5 Resolve secret（验证用）

```python
async def resolve_secret_for_validation(
    rec: CredentialRecord, store: SecretStore
) -> tuple[str | None, CredentialStorageStatus]:
    """仅用于 provider validation——返回的 str 绝不能赋值给 dataclass 字段、
    不进 log、不进 serializer。仅在调用者 try/finally 内作临时变量。"""
    if not await store.is_available():
        return None, "backend_unavailable"
    secret = await store.get(rec.secret_ref)
    if secret is None:
        return None, "needs_key"
    return secret, "ready"
```

## 7. Provider Hint Local Heuristic（providers/registry.py）

**核心原则：不向任何第三方发请求探测——只看格式**。

```python
ProviderHintConfidence = Literal["high", "medium", "low", "unknown"]

@dataclass(frozen=True)
class ProviderHintResult:
    candidates: list[str]              # provider_id 列表，可能 0/1/2+ 个
    confidence: ProviderHintConfidence
    reason_code: str                   # 内部审计用："prefix_match_sk_ant" / "ambiguous_sk_prefix" / "no_hint_match"

def detect_provider_hint(api_key: str) -> ProviderHintResult:
    """纯本地规则匹配。无 HTTP / socket / DNS 调用。"""
```

**confidence 语义**：

| confidence | 条件 | 前端文案（P1-E4） |
|---|---|---|
| `high` | 唯一确定 provider（如 `sk-ant-` → anthropic） | "可能是 Anthropic" |
| `medium` | 多个候选但其中一个明显更可能 | "可能是 GLM 或 OpenAI-compatible" |
| `low` | 仅 `sk-` 前缀——多 provider 都可能 | "可能是 OpenAI-compatible 类" |
| `unknown` | 无 hint 命中 | 无建议——user 手选或选 custom |

**关键安全约束**：
- 函数纯本地——无 `import httpx` / `aiohttp` / `requests` / `socket` / `urllib`
- 测试必须 mock 这些库——确保即使误改也不会发请求
- **前端文案严格**"可能属于 X"——**绝不**写 "已检测为 X" / "已识别为 X"

## 8. Serializer 安全规则（web/serializers.py）

```python
def serialize_credential(
    rec: CredentialRecord,
    storage_status: CredentialStorageStatus,  # 运行时计算后传入
) -> dict:
    return {
        "credential_id": rec.id,
        "label": rec.label,
        "storage_mode": rec.storage_mode,
        "storage_status": storage_status,          # 运行时派生
        "validation_status": rec.validation_status,
        "masked_value": rec.masked_value,
        "provider_hint": rec.provider_hint,
        "provider_hint_confidence": rec.provider_hint_confidence,
        "last_validated_provider_id": rec.last_validated_provider_id,
        "last_validated_at": rec.last_validated_at,
        "last_error_code": rec.last_error_code,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }
    # ❌ fingerprint_sha256 永远不出现
    # ❌ secret_ref 永远不出现
    # ❌ api_key 永远不出现
```

**固定错误消息映射**（基于 error_code，不基于 exception）：

```python
_VALIDATION_ERROR_MESSAGES: Mapping[CredentialValidationErrorCode, str] = {
    "credential_missing":    "Credential not found in storage backend.",
    "provider_not_supported":"Provider does not support validation in this release.",
    "authentication_failed": "The provider rejected this credential.",
    "permission_denied":     "Credential authenticated but lacks permission.",
    "rate_limited":          "Provider rate-limited the validation request.",
    "endpoint_unreachable":  "Could not reach provider endpoint.",
    "request_timeout":       "Validation request timed out.",
    "tls_error":             "TLS handshake failed.",
    "protocol_error":        "Provider returned an unexpected protocol response.",
    "unknown_error":         "Validation failed for an unknown reason.",
}

def serialize_validation(result: ValidationResult) -> dict:
    return {
        "valid": result.valid,
        "provider_id": result.provider_id,
        "validated_at": result.validated_at,
        "error_code": result.error_code,                       # 固定枚举
        "message": _VALIDATION_ERROR_MESSAGES.get(result.error_code or "unknown_error", "...")
            if not result.valid else None,
    }
    # ❌ 不返回 response.text / exception repr / request headers / provider raw error body
```

## 9. HTTP API（web/credentials_api.py）

**8 个 endpoint**（GET single credential 不实现——列表已返回完整安全 DTO）：

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/provider-definitions` | 列出内置 ProviderDefinition（不含 custom 的 validation_endpoint） |
| `POST` | `/api/provider-hints` | 接受 `{api_key}` → 返回 `detect_provider_hint(api_key)` 结果（本地规则） |
| `GET` | `/api/credentials` | 列出 credentials（含运行时 storage_status） |
| `POST` | `/api/credentials` | 创建 credential |
| `PATCH` | `/api/credentials/{id}` | **仅改 label**——不改 secret / storage_mode / provider_hint |
| `PUT` | `/api/credentials/{id}/secret` | **Rotate secret**——独立 API，便于审计 + 补偿事务 |
| `DELETE` | `/api/credentials/{id}` | 删除 credential |
| `POST` | `/api/credentials/{id}/validate` | 验证 Key——**必须** `{provider_id}`，不接受 base_url |

### 9.1 创建请求 schema

```python
class CreateCredentialRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field(..., min_length=1, max_length=512)
    storage_mode: Literal["keyring", "session_only", "env"] = "keyring"
    provider_hint: str | None = None       # user 可显式指定；None 则 client 先调 /api/provider-hints 再传回
    env_var_name: str | None = None        # storage_mode=env 时必填

class RotateSecretRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=512)
    # 不接受 storage_mode 改变——rotation 不切 backend

class ValidateCredentialRequest(BaseModel):
    provider_id: str                       # 必须是 ProviderDefinition.id 之一；custom/openai-compatible 拒绝
    # ❌ 不接受 base_url——Custom endpoint 留 P1-E2 ProviderProfile
```

### 9.2 Custom Base URL 明确移到 P1-E2

P1-E1 **不**支持：
- Custom Provider
- Custom Base URL
- localhost vLLM 验证
- OpenAI-compatible（无固定 base_url）

这些都需要：
- SSRF 防护
- URL 标准化
- redirect 安全
- 局域网地址规则
- profile identity
- 模型缓存失效

统一在 P1-E2 ProviderProfile 中实现。**P1-E1 严格 fixed-endpoint validation only**——内置 ProviderDefinition 中冻结的 HTTPS endpoint。

## 10. Validation HTTP 安全策略（web/provider_validation.py）

```python
import httpx

_VALIDATION_HTTP_TIMEOUT = httpx.Timeout(
    connect=5.0,
    read=10.0,
    write=5.0,
    pool=5.0,
)

# 全局复用——避免每次验证新建 client（连接池效率）
_validation_client: httpx.AsyncClient | None = None

async def _get_validation_client() -> httpx.AsyncClient:
    global _validation_client
    if _validation_client is None:
        _validation_client = httpx.AsyncClient(
            timeout=_VALIDATION_HTTP_TIMEOUT,
            follow_redirects=False,        # ❗ 关键：禁止跟随 redirect
            verify=True,                   # 强制 TLS verify
        )
    return _validation_client
```

**关键不变量**：
- **不自动重试**——单次失败即返回错误码
- **不跟随 redirect**——302 到其他域名会带 Authorization 到错误目标
- **短超时**——connect 5s / read 10s / write 5s / pool 5s
- **强制 HTTPS endpoint**——`ProviderDefinition.validation_endpoint` 必须是 https://
- **TLS verify enabled**——不接受用户 disable
- **不记录 request headers**——日志只记 `(credential_id, provider_id, error_code, latency_ms)`

**错误码映射**（替代截断 exception）：

```python
def _map_validation_error(exc: Exception) -> CredentialValidationErrorCode:
    if isinstance(exc, httpx.ConnectError):
        return "endpoint_unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "request_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return "authentication_failed"
        if status == 403:
            return "permission_denied"
        if status == 429:
            return "rate_limited"
        if 400 <= status < 500:
            return "protocol_error"
        return "unknown_error"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    return "unknown_error"
```

`str(exc)` 永远不进 serializer、不进 log、不进数据库——只内部映射成 `error_code`。

## 11. keyring 依赖与 lifespan

### 11.1 pyproject.toml

```toml
[project.optional-dependencies]
web = [
    "fastapi",
    "uvicorn",
    "keyring>=25",   # optional——keyring backend 不可用时不阻塞
]
```

CI/E2E 启动时通过 `PI_AGENT_SECRET_BACKEND=memory` 强制走 InMemory。

### 11.2 Factory（secrets/factory.py）

```python
def build_secret_store(
    requested_backend: str | None = None,
) -> SecretStore:
    """Backend 探测顺序：
    1. PI_AGENT_SECRET_BACKEND 环境变量覆盖（memory / env / keyring）
    2. 显式 requested_backend 参数
    3. 默认尝试 keyring，失败 fallback memory

    永远不返回 None——至少返回 InMemorySecretStore。"""
```

### 11.3 lifespan 接入

```python
# web/app.py create_app() 内
secret_store = build_secret_store(
    requested_backend=os.environ.get("PI_AGENT_SECRET_BACKEND"),
)
state.secret_store = secret_store
state.credential_store = CredentialStore(
    conn=state.extension_store._conn,  # 共享 connection
)
state.credential_service = CredentialService(
    credential_store=state.credential_store,
    secret_store=secret_store,
)

if not await secret_store.is_available():
    # 不阻塞启动——所有 keyring 模式 credential 进 needs_key 状态
    logger.warning(
        "SecretStore backend %s not available; keyring-mode credentials will need re-entry",
        secret_store.backend_name(),
    )
```

**禁止行为**：
- `keyring` 不可用时 app 启动失败
- 自动 fallback 到 SQLite 明文（永远不）
- 把 `secret_store` 实例放进任何会被序列化的 state（如 session snapshot）

## 12. 测试要求

### 12.1 实施拆分（E1-1 → E1-5）

| 子阶段 | 范围 | 完成后停止点 |
|---|---|---|
| **E1-1 Secret primitives** | SecretStore Protocol / InMemorySecretStore / EnvSecretStore / OSKeyringSecretStore / mask_secret / fingerprint_sha256 / detect_provider_hint | E1-1 单元测试全 PASS |
| **E1-2 Credential repository** | web_credentials_schema_meta / web_credentials table / init / CredentialRecord repository / restart recovery / resolve_storage_status | E1-2 单元测试全 PASS |
| **E1-3 Credential service + validation** | create / update / rotate / delete 补偿事务 / built-in provider validation / 固定错误码映射 / HTTP timeout/no-redirect | E1-3 单元测试全 PASS |
| **E1-4 Web API + lifespan** | 8 endpoints / safe serializers / SecretStore injection / PI_AGENT_SECRET_BACKEND / startup degradation | E1-4 单元测试全 PASS + 现有 E2E 不回归 |
| **E1-5 Security validation + Docs + Freeze** | secret leak matrix / server restart / CI memory backend / full pytest / E2E regression / docs / freeze report | 全部 15 项验收通过 → P1-E1 FROZEN |

每个子阶段独立验证通过后再进入下一阶段——**不**一次性落 10 个测试文件和全部 API。

### 12.2 单元测试文件

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_secret_store_memory.py` | InMemorySecretStore CRUD + backend_name + restart 后丢 |
| `tests/test_secret_store_env.py` | EnvSecretStore 读 os.environ + 缺失返回 None + 不支持 set() |
| `tests/test_secret_store_keyring.py` | fake keyring backend 注入；is_available 探测正确（mock D-Bus 缺失场景）；asyncio.to_thread 调用 |
| `tests/test_secret_store_factory.py` | backend 探测顺序 + `PI_AGENT_SECRET_BACKEND=memory` 覆盖 + keyring 失败 fallback |
| `tests/test_fingerprint_mask.py` | fingerprint_sha256 算法稳定 + mask 长度边界（短 key / 长 key / 8 字符 key） |
| `tests/test_provider_registry.py` | 4 个内置 definition + detect_provider_hint 四态（high / medium / low / unknown） |
| `tests/test_provider_hint_safety.py` | **关键**：mock `httpx` / `socket` / `urllib` / `dns`——断言 detect_provider_hint 不触发任何网络调用 |
| `tests/test_credentials_store.py` | SQLite CRUD + 独立 schema_meta（不影响 extension_store version） |
| `tests/test_credentials_service_create.py` | create 补偿：SQLite 失败 → secret 删除；secret 失败 → 无 SQLite 副作用 |
| `tests/test_credentials_service_rotate.py` | rotate 补偿：双 secret_ref 切换；SQLite 失败旧 Key 仍可用；optimistic concurrency |
| `tests/test_credentials_service_delete.py` | delete 三模式（keyring 失败保留 row / env 不删环境变量 / session_only 幂等） |
| `tests/test_provider_validation.py` | validation client：4 错误类别（auth/timeout/network/tls） + 不跟随 redirect + 不重试 |
| `tests/test_credentials_api.py` | 8 endpoint happy path + 404 + 400 + 验证错误响应只含 error_code |
| `tests/test_validate_api.py` | validate 成功 / auth fail / network fail / provider 不支持 / 缺 provider_id |
| `tests/test_storage_status_resolution.py` | storage_status 三态（ready / needs_key / backend_unavailable）运行时计算 |

### 12.3 Secret Leak Tests（tests/test_p1_e1_secret_leak.py）

**这是 P1-E1 最重要的测试类别**。所有可能泄露 Key 的出口必须 0 命中。

**SECRET_MARKER**：`"PI_E1_SECRET_MARKER_7F3A91D2"`（避免被现有 secret-scan 误判为真实凭证）

**必须覆盖的出口**（≥ 10 类）：

| # | 出口 | 检查项 |
|---|---|---|
| 1 | SQLite 数据库内容 | dump 整个 DB，grep SECRET_MARKER |
| 2 | REST JSON response body | 所有 GET endpoint |
| 3 | HTTP error response（4xx/5xx）detail | 触发各类错误 |
| 4 | WebSocket / event buffer | subscribe + 触发任意事件 |
| 5 | Python log records（caplog） | validation 成功 + 失败 |
| 6 | `exception str` 和 `repr` | validation 异常路径 |
| 7 | Session / Snapshot JSON | 创建 credential 后 snapshot |
| 8 | Export Markdown | session export |
| 9 | Frontend static bundle | `npm run build` 产物 |
| 10 | API validation history 字段 | validation_status / last_error_code 序列化 |

**额外检查**（在以上出口中搜）：
- `Authorization` header（任何大小写）
- `Bearer <marker>` 模式
- `secret_ref` 字段值
- 完整 `fingerprint_sha256` 值
- 部分 Key 片段（`marker[:8]` / `marker[-4:]` 除外，因 masked_value 会保留首 2 + 末 4）

```python
@pytest.fixture
async def leak_test_client(web_client, secret_store_memory):
    cred = await web_client.state.credential_service.create(
        label="leak-test",
        provider_hint="glm",
        api_key=SECRET_MARKER,
        storage_mode="keyring",
    )
    return web_client, cred.id
```

### 12.4 E2E（P1-E5 阶段统一）

P1-E1 **不**新增 E2E 测试——前端选择器在 P1-E4，P1-E5 才跑跨阶段 E2E。

但 P1-E1 必须确保**现有 37 个 E2E 不回归**——因为 lifespan 接入 SecretStore 可能影响 `start_test_web_app.py` 启动。

## 13. 不包含项（P1-E1 显式 out of scope）

- ❌ 模型目录（`providers/model_catalog.py`——P1-E2）
- ❌ ProviderProfile（聚合 credential + provider + base_url + default_model——P1-E2）
- ❌ SessionModelBinding（每 session 独立选择——P1-E2）
- ❌ `web/provider_runtime.py`（请求级 ModelClient 绑定——P1-E3）
- ❌ `_run_prompt_core` / `_run_regeneration_core` 改造（P1-E3）
- ❌ request metadata 加 `provider_profile_id` / `model_id`（P1-E3）
- ❌ 前端 `components/provider/*` 和 `stores/providerStore.ts`（P1-E4）
- ❌ Regenerate 用当前模型（P1-E3，依赖 P1-E2）
- ❌ `providers/openai_compat.py` 完整实现（P1-E2 按需）
- ❌ Markdown 面板（P1-F1/F2）
- ❌ **Custom Base URL validation**（P1-E2 ProviderProfile，含 SSRF 防护 / redirect 安全 / 局域网规则）
- ❌ **OpenAI-compatible provider validation**（P1-E2，需要 user-supplied base_url）
- ❌ **Custom endpoint validation**（P1-E2）

## 14. 验收标准（P1-E1 Freeze 条件）

| # | 标准 | 验证方式 |
|---|---|---|
| 1 | 4 个内置 ProviderDefinition 正确 | `test_provider_registry.py` |
| 2 | 3 个 SecretStore 实现各自通过 | `test_secret_store_*.py` |
| 3 | SecretStore backend 不可用不阻塞 app 启动 | `test_lifespan.py` 新增 |
| 4 | CredentialRecord SQLite CRUD 全通过 | `test_credentials_store.py` |
| 5 | **Create 补偿**：SQLite 失败 → secret 被删；secret 失败 → 无 SQLite 副作用 | `test_credentials_service_create.py` |
| 6 | **Rotate 补偿**：SQLite 失败时旧 Key 仍可用 | `test_credentials_service_rotate.py` |
| 7 | **Delete 三模式**：keyring 失败保留 row / env 不删环境变量 / session_only 幂等 | `test_credentials_service_delete.py` |
| 8 | detect_provider_hint 四态正确 | `test_provider_registry.py` |
| 9 | **detect_provider_hint 无网络调用** | `test_provider_hint_safety.py` mock httpx/socket/dns |
| 10 | 8 个 HTTP endpoint happy path | `test_credentials_api.py` |
| 11 | **validate 不跟随 redirect + 不重试** | `test_provider_validation.py` mock httpx 302 |
| 12 | **validate 错误返回固定 error_code，不返回 str(exception)** | `test_validate_api.py` |
| 13 | **storage_status 三态运行时计算正确**（不持久化） | `test_storage_status_resolution.py` |
| 14 | **fingerprint_sha256 不在任何出口** | secret leak matrix #1-10 |
| 15 | **SECRET_MARKER 不在 ≥10 类出口**（含 Authorization/Bearer/secret_ref/fingerprint） | `test_p1_e1_secret_leak.py` |
| 16 | 现有 1131 个 pytest 不回归 | `pytest tests/ -m "not slow"` |
| 17 | 现有 37 个 E2E 不回归 | `cd tests/e2e && npx playwright test` |
| 18 | coverage ≥ 75% 保持 | `pytest --cov` |
| 19 | ruff clean | `ruff check src tests scripts` |
| 20 | production hooks scan `__storeHooks` / `__e2eHooks` = 0 | `npm run build` 后 grep |

**不要求**：tag（P1-E 整体在 P1-E5 freeze 时打 tag）；前端 build size 保持（P1-E1 不动前端）。

## 15. 风险与已拒绝方案

| 风险 | 已拒绝方案 | P1-E1 选择 |
|---|---|---|
| Key 跨边界泄漏 | SQLite 明文 + 应用层加密 | OS keyring + secret_ref |
| CI 环境无 keyring | mock 全局 keyring 模块 | `SecretStore` Protocol 注入 + `PI_AGENT_SECRET_BACKEND=memory` |
| 用户粘错 provider | 自动向多个 provider 探测 | 本地格式 hint + user 显式确认 |
| validate 中泄漏 Key | 详细错误信息（含 Authorization echo） | 固定错误码 + serializer 映射安全消息（**不**截断 str(exception)） |
| Provider 探测中泄漏 Key | `sk-` 前缀触发多 provider 并发验证 | 只本地匹配，单 provider 验证 |
| Keyring backend 假阳性 | 只检查 `import keyring` 成功 | `is_available()` 实际探测 backend（识别 FailKeyring / null backend） |
| SQLite / keyring 双写不一致 | 顺序无要求 / 直接覆盖 secret_ref | **先 secret 后 SQLite**；rotate 用新 secret_ref + optimistic concurrency；delete 区分 storage_mode |
| Validate 跟随 redirect | 默认 httpx follow_redirects=True | `follow_redirects=False`——302 不带 Authorization 到他域 |
| Validate 中 retry 泄漏多次 Key | httpx 默认重试 | 单次失败即返回 error_code |
| Fingerprint 跨接口泄漏 | API 返回完整 hash 作 "id" 用 | 仅 SQLite 内部去重——serializer 绝不返回 |
| storage_status 长期失同步 | 持久化列 | 运行时计算（secret + backend 实时探测） |
| Custom URL 验证 SSRF | P1-E1 接受 base_url | **移 P1-E2** + ProviderProfile（含 SSRF 防护 / 局域网规则） |

## 16. 时序图（关键流程）

### 16.1 Create credential

```
User → POST /api/credentials {label, api_key, storage_mode="keyring"}
  ↓
credentials_service.create()
  ↓
  cred_id = f"cred-{uuid4()}"
  secret_ref = cred_id
  ↓
  secret_store.set(secret_ref, api_key)            ← ① 先写 secret
     fail → raise backend_error（无 SQLite 副作用）
  ↓
  SQLite BEGIN IMMEDIATE                            ← ② 后写 SQLite
    INSERT web_credentials(...)
  COMMIT
     fail → best-effort secret_store.delete(secret_ref) → raise db_error
  ↓
return serialize_credential(rec, storage_status="ready")
```

### 16.2 Rotate secret

```
User → PUT /api/credentials/{id}/secret {api_key: new_key}
  ↓
credentials_service.rotate_secret()
  ↓
  new_secret_ref = f"cred-{uuid4()}"
  old_secret_ref = rec.secret_ref
  ↓
  secret_store.set(new_secret_ref, new_key)        ← ① 写新 secret（不动旧）
     fail → raise backend_error（旧 Key 仍可用）
  ↓
  SQLite BEGIN IMMEDIATE                            ← ② 原子切换
    UPDATE web_credentials SET
      secret_ref = new_secret_ref,
      masked_value = new_masked,
      fingerprint_sha256 = new_fingerprint,
      validation_status = 'never_validated',
      last_validated_at = NULL,
      last_error_code = NULL,
      updated_at = ?
    WHERE id = ? AND secret_ref = old_secret_ref   ← optimistic concurrency
  COMMIT
     fail → best-effort secret_store.delete(new_secret_ref) → raise db_error
     rowcount=0 → best-effort secret_store.delete(new_secret_ref) → raise conflict
  ↓
  best-effort secret_store.delete(old_secret_ref)  ← ③ 旧 Key 清理（失败仅记日志）
  ↓
return serialize_credential(rec, storage_status="ready")
```

### 16.3 Validate credential

```
User → POST /api/credentials/{id}/validate {provider_id: "glm"}
  ↓
credentials_service.validate()
  ↓
  rec = credential_store.get(id)                   ← 读 record
  api_key, storage_status = resolve_secret_for_validation(rec, secret_store)
     storage_status="backend_unavailable" → result.error_code = "credential_missing"
     storage_status="needs_key"            → result.error_code = "credential_missing"
  ↓
  provider = get_provider_definition("glm")
     None / not supports_model_listing → result.error_code = "provider_not_supported"
  ↓
  provider_validation.check(provider.validation_endpoint, api_key)
    using httpx.AsyncClient(timeout=Timeout(...), follow_redirects=False, verify=True)
     ↓
     try: HTTP 200 → result.valid = True, error_code = None
     except httpx.HTTPStatusError(401):  error_code = "authentication_failed"
     except httpx.HTTPStatusError(403):  error_code = "permission_denied"
     except httpx.HTTPStatusError(429):  error_code = "rate_limited"
     except httpx.TimeoutException:      error_code = "request_timeout"
     except httpx.ConnectError:          error_code = "endpoint_unreachable"
     except ssl.SSLError:                error_code = "tls_error"
     except Exception:                   error_code = "unknown_error"
  ↓
  finally: del api_key                              ← 栈内消亡——绝不进 log/serializer
  ↓
  credential_store.update_validation_status(id, status, provider_id, error_code)
  ↓
return serialize_validation(result)                 ← 只含 error_code + 固定 message
```

## 17. 文档交付（E1-5 完成时更新）

- `STATUS.md`——Current phase → P1-E2
- `CHANGELOG.md` Unreleased——加 P1-E1 段
- `TODO.md`——切换到 P1-E2 deliverables
- `docs/api/web-api.md`——加 8 个新 endpoint
- `docs/architecture/`——新增 `secrets-and-credentials.md`（150 行内）
- `docs/validation/p1-e/`——新增 `P1_E1_VALIDATION_REPORT.md`

## 18. 启动条件

本计划 APPROVED 后：

1. **Commit 5 份文档**——`docs: freeze P1-E roadmap and secure credentials design`
   - `STATUS.md` / `ROADMAP.md` / `CHANGELOG.md` / `TODO.md` / `docs/design/p1-e1-secure-credentials.md`
2. **新建工作分支**——`feat/p1-e1-secure-credentials`
3. **按 §12.1 子阶段顺序实施**：E1-1 → E1-2 → E1-3 → E1-4 → E1-5
4. 每子阶段完成后跑：
   ```bash
   /d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow" \
     -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning"
   ```
   确认无回归才进入下一阶段
5. E1-5 完成时跑 §14 全部 20 项验收
6. PR / merge by user decision

---

**附录**：本计划未涵盖的边界条件（如发现需补充）——回到设计阶段，**不**在编码中临时决定。
