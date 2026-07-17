# P1-E1 Provider Validation 设计冻结

> **状态**：APPROVED — Anthropic validation only（2026-07-17）
> **阶段**：P1-E1-3B Provider Validation
> **基线**：`feat/p1-e1-secure-credentials` HEAD `5c84693`（P1-E1-3A FROZEN）
> **冻结依据**：E1-3A 已 FROZEN；本审计完成于 2026-07-17 conversation；GLM 远端验证暂不可批准

---

## 0. 审核修订记录

| 日期 | 修订 |
|---|---|
| 2026-07-17 | **APPROVED — Anthropic validation only**——E1-3B 拆分（B1 strategy + B2 service wiring）；Anthropic Models API 列为唯一可批准远端验证；GLM 暂不批准远端验证；Messages probe 永久禁止；`validation_endpoint` 字段重命名为 `credential_validation_endpoint` 并加 strategy 字段；固定错误码表；HTTP 安全配置（短超时 / no retry / no redirect / verify TLS / 不存 raw response）；状态语义（401 → invalid，其余 → error）。 |

## 1. 范围

### 1.1 本阶段实现（E1-3B1 + E1-3B2）

| 子阶段 | 范围 |
|---|---|
| **E1-3B1** | Validation 抽象 + Anthropic Models API strategy + 错误码 + HTTP 安全配置 |
| **E1-3B2** | `CredentialService.validate(credential_id, provider_id)` 接线 + CAS 保护 |

### 1.2 实现 Anthropic 远端验证

```text
provider_id            = anthropic
strategy               = list_models
HTTP method            = GET
endpoint               = https://api.anthropic.com/v1/models
auth header            = x-api-key: <secret>
requires_model         = false
inference_cost         = none
follows_redirect       = false
```

成功语义：HTTP 2xx + JSON body 包含合法 `models` / `data` 数组 → `validation_status = valid`.

### 1.3 暂不实现 / 永久禁止

| 项 | 决定 |
|---|---|
| GLM 远端验证 | ⏸ **DESIGN DEFERRED**——公开 API 索引中找不到无推理 / 无费用的统一模型列表 endpoint。本阶段 GLM strategy = `unsupported`. |
| Messages probe（任何 provider） | ⛔ **PERMANENTLY BANNED**——会触发实际推理 + 费用；需要预知 model id；易把权限 / 套餐 / 模型不存在误判为凭证无效；Coding Plan Key 与平台 Key 行为不同. |
| OpenAI-compatible / Custom Base URL | ⏸ 留 P1-E2（ProviderProfile）. |
| Model Catalog（远端模型列表渲染） | ⏸ E1-3B 之外. |

## 2. ProviderDefinition 调整

### 2.1 当前 schema 的误导风险

现有 `ProviderDefinition.validation_endpoint: str` 字段同时被 GLM 与 Anthropic 指向 Messages endpoint. 这会让后续代码简单地对该 endpoint 发请求 → 实际执行推理 + 产生费用. 必须在编码前修正字段语义.

### 2.2 新 schema（E1-3B1 落地）

```python
CredentialValidationStrategy = Literal[
    "list_models",       # Anthropic Models API
    "unsupported",       # GLM / 任何暂不可远端验证的 provider
]


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    display_name: str
    api_style: ProviderAPIStyle
    default_base_url: str

    credential_validation_strategy: CredentialValidationStrategy
    credential_validation_endpoint: str | None      # None 当且仅当 strategy = "unsupported"

    supports_model_listing: bool
    key_prefix_hints: tuple[str, ...] = ()
```

### 2.3 内置初始值

| provider_id | strategy | endpoint | supports_model_listing |
|---|---|---|---|
| `anthropic` | `list_models` | `https://api.anthropic.com/v1/models` | `True` |
| `glm` | `unsupported` | `None` | `False` |

### 2.4 字段命名约束

- 旧字段名 `validation_endpoint` **禁止继续使用**——必须从代码中移除，避免新代码误用 Messages endpoint.
- Adapter 内部如需 Messages URL，由 `default_base_url` 在 Adapter 内构造，字段名建议 `messages_endpoint`（不在 ProviderDefinition 中）.

## 3. Strategy Protocol 与数据类型

### 3.1 文件位置

```text
src/pi_agent_core_py/web/provider_validation.py
src/pi_agent_core_py/providers/registry.py     # 扩展 ProviderDefinition
tests/test_provider_validation_anthropic.py
tests/test_provider_validation_errors.py
tests/test_provider_validation_security.py
```

### 3.2 Strategy Protocol

```python
class ProviderValidationStrategy(Protocol):
    """凭证远端验证策略.

    **关键安全契约**：
    - Secret 只在 `validate(secret)` 调用期间存在；**不**存入对象字段
    - Secret 不进入异常 str / repr / 日志 / httpx request repr
    - 不重试；不跟随 redirect；不存完整 response body
    """

    async def validate(
        self,
        secret: str,
    ) -> ProviderValidationResult:
        ...
```

### 3.3 Result 与 ErrorCode

```python
CredentialValidationErrorCode = Literal[
    # 凭证层错误（无远端调用）
    "credential_missing",          # SecretStore.get 返回 None
    "provider_not_supported",      # Registry 中无此 provider_id
    "validation_not_supported",    # Provider 存在但 strategy = "unsupported"
    # 远端 HTTP 错误
    "authentication_failed",       # HTTP 401
    "permission_denied",           # HTTP 403
    "rate_limited",                # HTTP 429
    "endpoint_unreachable",        # connect / DNS failure
    "request_timeout",             # 读取 / 连接超时
    "tls_error",                   # TLS handshake / cert failure
    "protocol_error",              # 3xx / 5xx / 非法 JSON / 缺字段
    "unknown_error",               # fallback
]


@dataclass(frozen=True)
class ProviderValidationResult:
    valid: bool
    error_code: CredentialValidationErrorCode | None    # None 当且仅当 valid=True
    provider_id: str
```

### 3.4 错误码语义表

| HTTP / 网络状态 | error_code | validation_status（Service 写入 DB） |
|---|---|---|
| 2xx + 合法 models body | `None` | `valid` |
| 401 | `authentication_failed` | `invalid` |
| 403 | `permission_denied` | `error` |
| 429 | `rate_limited` | `error` |
| 3xx | `protocol_error` | `error` |
| 5xx | `protocol_error` | `error` |
| Connect / DNS failure | `endpoint_unreachable` | `error` |
| Timeout | `request_timeout` | `error` |
| TLS handshake / cert | `tls_error` | `error` |
| 2xx + 非法 JSON / 缺 `data` | `protocol_error` | `error` |
| 未匹配 | `unknown_error` | `error` |

### 3.5 为什么 403 → error 而非 invalid

403 可能表示 Key 本身有效但缺少特定 API 权限——不能证明 Key 失效. 若误判为 `invalid` 会诱导用户重新换 Key，而 Key 其实是好的. 一律写 `error` + `permission_denied` 让用户检查权限而非 Key.

## 4. Anthropic Models API strategy

### 4.1 实现要点

```python
class AnthropicModelsValidationStrategy:
    """GET https://api.anthropic.com/v1/models with x-api-key header."""

    _ENDPOINT = "https://api.anthropic.com/v1/models"
    _MAX_ERROR_BODY_BYTES = 1024    # 仅读有限错误正文（用于分类），不放入异常

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        # client_factory 用于注入测试 fake；生产路径用默认安全配置
        self._client_factory = client_factory or _default_safe_client
```

### 4.2 HTTP 安全配置（生产路径默认）

```python
def _default_safe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=5.0,
            read=10.0,
            write=5.0,
            pool=5.0,
        ),
        follow_redirects=False,   # 永不跟随——3xx 直接 protocol_error
        verify=True,              # 强制 TLS cert 校验
        max_redirects=0,
    )
```

**绝对禁止**：
- 自动 retry
- 跟随 redirect（包括同一 Host 内的 redirect）
- 把 `Authorization` / `x-api-key` header 写入日志
- 把完整 response body 写入异常 str / repr
- 在异常 message 中放 raw response text
- 把 secret 放入 client 实例的任何 attr

### 4.3 错误正文处理

失败响应最多读 `_MAX_ERROR_BODY_BYTES` 字节，仅用于内部错误分类（不放入异常 message）. 异常 message 只允许包含 HTTP 状态码与错误类型名.

## 5. GLM 远端验证策略

GLM 当前**不**实现远端验证. 公开 API 索引中未文档化无推理 / 无费用的统一模型列表 endpoint. 即便 `https://open.bigmodel.cn/api/anthropic/v1/messages` 是已知的可用 endpoint，**禁止**用它做最小 prompt probe——会触发实际推理 + 费用，且无法稳定区分 Key 失效与模型 / 套餐 / 权限错误.

GLM ProviderDefinition：
```python
ProviderDefinition(
    id="glm",
    display_name="Zhipu GLM",
    api_style="anthropic_compatible",
    default_base_url="https://open.bigmodel.cn/api/paas/v4",
    credential_validation_strategy="unsupported",
    credential_validation_endpoint=None,
    supports_model_listing=False,
    key_prefix_hints=("glm-",),
)
```

Service 调用 `validate(credential_id, "glm")` 时返回 `validation_not_supported` 错误，**不**修改 DB 中的 validation_status.

## 6. CredentialService.validate 接线（E1-3B2）

### 6.1 方法签名

```python
async def validate(
    self,
    credential_id: str,
    provider_id: str,
) -> CredentialOperationResult:
    """远端验证 credential + 把结果 CAS 写入 repository.

    Raises:
        CredentialNotFoundError: credential_id 不存在
        CredentialInputError: provider_id 不在 Registry
        CredentialBackendUnavailableError: SecretStore 不可用
        CredentialOperationConflictError: 验证期间发生 rotate（CAS 拒绝）
        CredentialCompensationError: 其它存储失败
    """
```

### 6.2 固定流程

```text
1. Repository.get(credential_id) → record
2. ProviderDefinition = Registry.get(provider_id)
   - 不存在 → CredentialInputError(provider_not_supported)
3. strategy = definition.credential_validation_strategy
   - strategy == "unsupported" → return early with
     validation_status 不变，warning=("validation_not_supported",)
4. Router.try_resolve(storage_mode) → store | None
   - None → CredentialBackendUnavailableError
5. secret = await store.get(record.secret_ref)
   - secret is None → return early with
     validation_status 不变，warning=("credential_missing",)
6. validated_secret_ref = record.secret_ref    # CAS guard
7. result = await strategy.validate(secret)
8. mapped_status = _map_result_to_validation_status(result)  # valid / invalid / error
9. Repository.update_validation_state(
       credential_id,
       expected_secret_ref=validated_secret_ref,
       validation_status=mapped_status,
       provider_id=provider_id if result.valid else None,
       validated_at=now_ms if result.valid else None,
       error_code=result.error_code,
   )
   - CredentialConcurrentModificationError →
     CredentialOperationConflictError（验证结果丢弃，不写新 Key）
10. Return CredentialOperationResult(record=updated_record, warnings=())
```

### 6.3 关键不变量

- **旧 Key 发出的验证结果不能写到 rotate 后的新 Key 上**——CAS 通过 `expected_secret_ref=validated_secret_ref` 拒绝
- 验证**不**修改 `masked_value` / `fingerprint_sha256` / `secret_ref`
- 验证**不**把 raw response body 写入 DB（`last_error_code` 只放固定 error_code 字符串）
- `valid=True` 才写 `provider_id` 与 `validated_at`；失败时这两字段置 None
- Service `validate` 不调 `mask_secret` / `fingerprint_secret`——Secret 不进 Service 内存之外

### 6.4 验证失败也要持久化

与 P1-E1-3A 的 create/rotate 不同——`validate` 即使返回 `valid=False` 也算**成功完成**操作. Service 把结果（包括 error_code）写入 DB 后返回 `CredentialOperationResult`. 不抛异常.

唯一抛异常的情形：
- credential_id 不存在
- provider_id 不在 Registry
- backend 不可用（无法读 Secret）
- CAS 冲突（rotate 期间验证结果作废）
- Repository 自身故障（DB 错误）

## 7. 测试覆盖

### 7.1 Anthropic Strategy（`test_provider_validation_anthropic.py`）

至少覆盖：

| 用例 | 期望 |
|---|---|
| 2xx 合法 `{data: [...]}` | `valid=True`, `error_code=None` |
| 2xx 合法 `{models: [...]}` | `valid=True`, `error_code=None` |
| 2xx 非法 JSON | `valid=False`, `error_code=protocol_error` |
| 2xx 缺 `data` / `models` 字段 | `valid=False`, `error_code=protocol_error` |
| 401 | `valid=False`, `error_code=authentication_failed` |
| 403 | `valid=False`, `error_code=permission_denied` |
| 429 | `valid=False`, `error_code=rate_limited` |
| 3xx 不跟随 | `valid=False`, `error_code=protocol_error` |
| 5xx | `valid=False`, `error_code=protocol_error` |
| Connect timeout | `valid=False`, `error_code=request_timeout` |
| Read timeout | `valid=False`, `error_code=request_timeout` |
| DNS failure | `valid=False`, `error_code=endpoint_unreachable` |
| TLS cert failure | `valid=False`, `error_code=tls_error` |
| Response body 超大（10MB） | `valid=False`, `error_code=protocol_error`；不 OOM |

### 7.2 Strategy 通用错误（`test_provider_validation_errors.py`）

- 错误码到 `validation_status` 映射符合 §3.4 表
- 异常 message 不含 secret
- 异常 message 不含 response body
- httpx request 不含 secret in repr（通过注入 mock client 验证）
- 不自动 retry（注入总是 5xx 的 client，断言只调用 1 次）
- 不跟随 redirect（注入 302 → 不同 Host，断言不请求新 Host）

### 7.3 Strategy 安全（`test_provider_validation_security.py`）

使用 `SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"`：
- 异常 str / repr 不含 marker
- 异常 `__cause__` 链不含 marker
- httpx request headers / url / repr 不含 marker（注入 spy client）
- 错误日志（caplog）不含 marker
- strategy 对象自身 attrs 不含 marker（验证完 Secret 不残留）

### 7.4 Service 接线（在 `test_credentials_service_validate.py` 新文件）

- Keyring credential 验证成功 → DB 写 `valid` + `provider_id` + `validated_at`
- Session-only credential 验证成功
- Env credential 动态读当前 env 值验证
- Secret 缺失 → warning=`credential_missing`，validation_status 不变
- Backend unavailable → CredentialBackendUnavailableError
- Unknown provider → CredentialInputError(provider_not_supported)
- GLM validate → warning=`validation_not_supported`，DB 不变
- 401 → DB 写 `invalid` + `error_code=authentication_failed`
- 403 → DB 写 `error` + `error_code=permission_denied`
- 429 → DB 写 `error` + `error_code=rate_limited`
- Timeout → DB 写 `error` + `error_code=request_timeout`
- 验证期间发生 rotate → CredentialOperationConflictError，新 Key 的 row 不被旧验证结果污染
- 验证不修改 `masked_value` / `fingerprint` / `secret_ref`
- Raw response body 不进入 DB
- 错误码字段是 Literal 字符串（不放 raw response text）

## 8. 文件清单

### 8.1 E1-3B1（strategy 层）

```text
src/pi_agent_core_py/web/provider_validation.py            # 新
src/pi_agent_core_py/providers/registry.py                 # 改：ProviderDefinition schema
tests/test_provider_validation_anthropic.py                # 新
tests/test_provider_validation_errors.py                   # 新
tests/test_provider_validation_security.py                 # 新
```

### 8.2 E1-3B2（service 接线）

```text
src/pi_agent_core_py/web/credentials_service.py            # 改：加 validate() 方法
tests/test_credentials_service_validate.py                 # 新
```

### 8.3 禁止修改

- `web/app.py`（Web API 留 E1-4）
- `web/state.py`
- `web/serializers.py`（serializer 留 E1-4）
- `loop.py` / `agent.py` / `context.py` / `events.py`
- 前端任何文件
- Provider adapter 的 Messages URL 构造（Adapter 内部自行处理）
- `secrets/` 模块任何文件

## 9. 验收标准

E1-3B1 + E1-3B2 完成后必须达到：

- `test_provider_validation_*` 专项测试全 PASS（预计 ≥ 30 项）
- `test_credentials_service_validate` 专项测试全 PASS（预计 ≥ 15 项）
- 完整离线 pytest ≥ 1451（不回归）
- ruff clean
- `git diff --check` clean
- working tree clean

提交建议（两个原子 commit）：

```text
1. feat(credentials): add anthropic credential validation strategy
2. feat(credentials): wire validation into credential service
```

完成后**不**进入 E1-4 Web API——等 user 冻结 E1-3B.

## 10. 后续阶段（非本设计范围）

| 阶段 | 内容 |
|---|---|
| **GLM 远端验证** | 待智谱文档明确提供无推理 / 无费用的模型列表 endpoint 后单独审计 |
| **Model Catalog** | 基于 Anthropic Models API 的模型列表渲染——E1-3B 之外 |
| **P1-E1-4 Web API** | CredentialService → REST endpoints（POST /credentials, GET /credentials, etc.）+ Web lifespan + 安全 serializer |
| **P1-E1-5 Security Freeze** | 全链路 SECRET_MARKER 扫描 + 第三方安全审计 checklist |

---

**冻结人**：user（2026-07-17）
**实施**：Claude Code（待 user 启动 E1-3B1 编码）
