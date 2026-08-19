"""Provider credential validation strategies（P1-E1-3B1）.

定义**远端凭证验证**的策略层——不发推理请求，只做"Key 是否有效"的最小探测.

**关键安全契约（绝对不可破坏）**：

1. Secret 只在 `validate(secret)` 调用期间存在；**不**存入 Strategy 对象 / closure
2. Secret 不进入：
   - 异常 `str()` / `repr()`
   - `ProviderValidationResult`
   - 日志
   - httpx request repr / client attrs
3. HTTP 配置固定——不接受调用方注入 URL / Header / 超时
4. 不自动 retry；不跟随 redirect；强制 TLS verify
5. 不保存 raw response body——非 2xx 不读 body；2xx 最多 `_MAX_SUCCESS_BODY_BYTES`
6. Strategy 只发到 ProviderDefinition 中冻结的 endpoint

**E1-3B1 实现**：

- `AnthropicModelsValidationStrategy` — GET /v1/models with x-api-key
- `ValidationStrategyRegistry` — strategy ID → Strategy 实例（不含 secret）

**E1-3B1 不实现**（留 E1-3B2）：

- `CredentialService.validate()` 接线
- Repository validation_status 写入
- 任何 Messages probe（永久禁止）

**错误码**：见 `CredentialValidationErrorCode`——固定 11 项 Literal，异常 message
不含 raw response / secret / 异常文本，只允许含错误码与 HTTP 状态（如适用）.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Never, Protocol

import httpx

from ...providers.registry import CredentialValidationStrategyId

__all__ = [
    # Types
    "CredentialValidationErrorCode",
    "ProviderValidationResult",
    "ProviderValidationStrategy",
    "HttpClientFactory",
    # Strategies
    "AnthropicModelsValidationStrategy",
    # Registry
    "ValidationStrategyRegistry",
    # Default safe client factory
    "default_safe_http_client_factory",
]


# ============================================================================
# Types
# ============================================================================


CredentialValidationErrorCode = Literal[
    # 凭证层错误（E1-3B2 Service 层产生——本模块不产生）
    "credential_missing",
    "provider_not_supported",
    "validation_not_supported",
    # 远端 HTTP 错误（本模块产生）
    "authentication_failed",       # HTTP 401
    "permission_denied",           # HTTP 403
    "rate_limited",                # HTTP 429
    "endpoint_unreachable",        # connect / DNS failure
    "request_timeout",             # 读取 / 连接超时
    "tls_error",                   # TLS handshake / cert failure
    "protocol_error",              # 3xx / 4xx (除 401/403/429) / 5xx / 非法 JSON
    "unknown_error",               # 未分类异常
]


@dataclass(frozen=True)
class ProviderValidationResult:
    """远端验证结果——不含 secret / raw response / 模型列表.

    Invariants:
        valid=True  → error_code=None
        valid=False → error_code 必须存在
    """

    provider_id: str
    valid: bool
    error_code: CredentialValidationErrorCode | None


# ============================================================================
# HTTP client factory
# ============================================================================


HttpClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=5.0,
    read=10.0,
    write=5.0,
    pool=5.0,
)


@asynccontextmanager
async def default_safe_http_client_factory() -> AsyncIterator[httpx.AsyncClient]:
    """生产路径默认 HTTP client factory——短生命周期、no retry、no redirect.

    Safety:
        - timeout: connect/read/write/pool 各 5/10/5/5 秒
        - follow_redirects=False: 3xx 直接返回，由 strategy 映射为 protocol_error
        - verify=True: 强制 TLS cert 校验
        - max_redirects=0: 显式禁用
    """
    async with httpx.AsyncClient(
        timeout=_DEFAULT_TIMEOUT,
        follow_redirects=False,
        verify=True,
        max_redirects=0,
    ) as client:
        yield client


# ============================================================================
# Strategy Protocol
# ============================================================================


class ProviderValidationStrategy(Protocol):
    """远端凭证验证策略.

    Implementation contract:
        - `provider_id` 是只读 attr——Strategy 实例不持有 secret
        - `validate(secret)` 在调用期间使用 secret；返回前清空局部引用；
          secret 不出现在任何异常 / 日志 / result
        - 不接受调用方传入 URL——endpoint 在 Strategy 中冻结
        - 实例可复用（无状态）
    """

    provider_id: str

    async def validate(
        self,
        secret: str,
    ) -> ProviderValidationResult:
        """Run remote credential check. Returns ProviderValidationResult."""
        ...


# ============================================================================
# Anthropic Models API strategy
# ============================================================================


class AnthropicModelsValidationStrategy:
    """GET https://api.anthropic.com/v1/models with x-api-key header.

    成功条件（全部满足）：
        - HTTP 2xx
        - Content-Type 是 JSON
        - 顶层是 object
        - 含 `data` 字段且为 list

    失败映射：
        401 → authentication_failed
        403 → permission_denied
        429 → rate_limited
        3xx → protocol_error（不跟随）
        400/404/405/409/422/5xx → protocol_error
        connect/DNS → endpoint_unreachable
        timeout → request_timeout
        TLS/cert → tls_error
        其它 → unknown_error

    Body 处理：
        - 非 2xx：**不**调用 response.json() / response.text
        - 2xx：最多读 `_MAX_SUCCESS_BODY_BYTES`（默认 1MB）；超限 → protocol_error

    不变量：
        - Secret 仅在 validate() 内部存活
        - 实例不持有 secret / client（client 由 factory 每次创建）
        - 不自动 retry
        - 不跟随 redirect
        - 不存 raw response 在异常 / result
    """

    provider_id: str = "anthropic"

    _ENDPOINT = "https://api.anthropic.com/v1/models"
    _API_VERSION_HEADER = "2023-06-01"
    _MAX_SUCCESS_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

    def __init__(
        self,
        *,
        client_factory: HttpClientFactory | None = None,
    ) -> None:
        """Initialize.

        Args:
            client_factory: 注入测试 fake；生产路径用
                `default_safe_http_client_factory`. factory 每次返回**新的**
                short-lived AsyncClient——Strategy 不持有 client 实例.
        """
        self._client_factory = client_factory or default_safe_http_client_factory

    async def validate(
        self,
        secret: str,
    ) -> ProviderValidationResult:
        """Run GET /v1/models with x-api-key. Returns safe result."""
        # 局部引用 secret——函数返回前自然释放；不写入实例 attr
        api_key = secret
        headers = {
            "x-api-key": api_key,
            "anthropic-version": self._API_VERSION_HEADER,
            "accept": "application/json",
        }

        try:
            async with self._client_factory() as client:
                response = await client.get(self._ENDPOINT, headers=headers)
        except httpx.TimeoutException:
            return ProviderValidationResult(
                provider_id=self.provider_id,
                valid=False,
                error_code="request_timeout",
            )
        except _TLS_EXCEPTIONS as e:
            return _classify_tls_error(e, self.provider_id)
        except httpx.ConnectError:
            return ProviderValidationResult(
                provider_id=self.provider_id,
                valid=False,
                error_code="endpoint_unreachable",
            )
        except httpx.HTTPError:
            return ProviderValidationResult(
                provider_id=self.provider_id,
                valid=False,
                error_code="protocol_error",
            )
        except Exception:
            # 未分类——保守映射 unknown_error
            return ProviderValidationResult(
                provider_id=self.provider_id,
                valid=False,
                error_code="unknown_error",
            )

        # Status 映射——不读 body
        status = response.status_code
        if status == 200:
            return await self._parse_success(response)
        if status == 401:
            return self._fail("authentication_failed")
        if status == 403:
            return self._fail("permission_denied")
        if status == 429:
            return self._fail("rate_limited")
        # 3xx / 4xx (除已处理) / 5xx / 其它
        return self._fail("protocol_error")

    def _fail(self, code: CredentialValidationErrorCode) -> ProviderValidationResult:
        return ProviderValidationResult(
            provider_id=self.provider_id,
            valid=False,
            error_code=code,
        )

    async def _parse_success(self, response: httpx.Response) -> ProviderValidationResult:
        """Parse 2xx response with bounded body read.

        - Content-Length > _MAX_SUCCESS_BODY_BYTES → protocol_error
        - Non-JSON content-type → protocol_error
        - Top-level not object → protocol_error
        - Missing `data` field / `data` not list → protocol_error
        """
        # 先用 Content-Length 做粗粒度 guard——避免下载超大响应
        cl = response.headers.get("content-length")
        if cl is not None:
            try:
                cl_int = int(cl)
            except ValueError:
                return self._fail("protocol_error")
            if cl_int > self._MAX_SUCCESS_BODY_BYTES:
                return self._fail("protocol_error")

        ctype = response.headers.get("content-type", "")
        if "application/json" not in ctype.lower():
            return self._fail("protocol_error")

        # 读 body——httpx 默认已读；额外检查实际长度
        body = response.content
        if len(body) > self._MAX_SUCCESS_BODY_BYTES:
            return self._fail("protocol_error")

        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._fail("protocol_error")

        if not isinstance(parsed, dict):
            return self._fail("protocol_error")

        # Anthropic Models API 返回 {data: [...], ...}——也兼容 {models: [...]}
        # 但必须有其一，且为 list
        data = parsed.get("data")
        if data is None:
            data = parsed.get("models")
        if not isinstance(data, list):
            return self._fail("protocol_error")

        # 不返回模型列表给上层——仅返回 valid=True
        return ProviderValidationResult(
            provider_id=self.provider_id,
            valid=True,
            error_code=None,
        )

    def __repr__(self) -> str:
        return (
            f"AnthropicModelsValidationStrategy(provider_id={self.provider_id!r}, "
            f"endpoint={self._ENDPOINT!r})"
        )


# ============================================================================
# TLS error classification helper
# ============================================================================


# ssl.SSLError + OSError 子类——TLS handshake / cert failure 通常被 httpx 包装
# 在此导入 ssl 以便 isinstance 检查（lazy import 避免顶层依赖）.
def _import_ssl_module() -> object | None:
    try:
        import ssl
        return ssl
    except ImportError:
        return None


# 类型化的异常元组——用于 isinstance 检查
def _build_tls_exception_tuple() -> tuple[type[BaseException], ...]:
    """Build tuple of TLS-related exception types. Returns empty if unavailable."""
    ssl_mod = _import_ssl_module()
    types: list[type[BaseException]] = []
    if ssl_mod is not None:
        ssl_error = getattr(ssl_mod, "SSLError", None)
        if ssl_error is not None:
            types.append(ssl_error)
        cert_error = getattr(ssl_mod, "SSLCertVerificationError", None)
        if cert_error is not None:
            types.append(cert_error)
    return tuple(types)


_TLS_EXCEPTIONS: tuple[type[BaseException], ...] = _build_tls_exception_tuple()


def _classify_tls_error(
    e: BaseException,
    provider_id: str,
) -> ProviderValidationResult:
    """Classify a TLS-related exception. Returns tls_error or endpoint_unreachable.

    只检查异常类型与 cause 类型，不读 str(e) / repr(e)（可能含 cert 细节）.
    """
    # ssl.SSLCertVerificationError 是 ssl.SSLError 子类——先检查更具体的
    ssl_mod = _import_ssl_module()
    if ssl_mod is not None:
        cert_error = getattr(ssl_mod, "SSLCertVerificationError", None)
        if cert_error is not None and isinstance(e, cert_error):
            return ProviderValidationResult(
                provider_id=provider_id,
                valid=False,
                error_code="tls_error",
            )
        ssl_error = getattr(ssl_mod, "SSLError", None)
        if ssl_error is not None and isinstance(e, ssl_error):
            return ProviderValidationResult(
                provider_id=provider_id,
                valid=False,
                error_code="tls_error",
            )
    # 未分类——保守 endpoint_unreachable（连接层失败）
    return ProviderValidationResult(
        provider_id=provider_id,
        valid=False,
        error_code="endpoint_unreachable",
    )


# ============================================================================
# Validation Strategy Registry
# ============================================================================


class ValidationStrategyRegistry:
    """Strategy ID → Strategy 实例映射.

    **关键不变量**：
        - 实例不持有 secret
        - 构造阶段不发网络（lazy 创建 strategy）
        - `unsupported` → 返回 None（不发网络）
        - 不通过 Provider ID 猜测 Strategy（只按 strategy ID 查）

    Service 层（E1-3B2）调用链：
        ProviderDefinition
          → credential_validation_strategy
            → ValidationStrategyRegistry.get(strategy_id)
    """

    __slots__ = ("_strategies",)

    def __init__(
        self,
        strategies: dict[CredentialValidationStrategyId, ProviderValidationStrategy]
        | None = None,
    ) -> None:
        """Initialize.

        Args:
            strategies: 可选注入——测试用 fake. 默认用生产 strategy 集合.
        """
        if strategies is None:
            strategies = {
                "anthropic_models": AnthropicModelsValidationStrategy(),
            }
        # 'unsupported' 不进 dict——get() 返回 None
        if "unsupported" in strategies:
            raise ValueError(
                "'unsupported' strategy must not be registered——"
                "Service should short-circuit before lookup"
            )
        self._strategies: dict[CredentialValidationStrategyId, ProviderValidationStrategy] = (
            dict(strategies)
        )

    def get(
        self,
        strategy_id: CredentialValidationStrategyId,
    ) -> ProviderValidationStrategy | None:
        """Return Strategy for strategy_id. None 表示该 provider 当前不支持远端验证.

        'unsupported' 总是返回 None（即便有人误注册也防御性返回 None）.
        """
        if strategy_id == "unsupported":
            return None
        return self._strategies.get(strategy_id)

    def __repr__(self) -> str:
        # 不展示 strategy 对象（其 repr 可能含 endpoint / client factory 细节）
        # 也不暴露 secret（registry 不持有 secret）
        ids = sorted(self._strategies.keys())
        return f"ValidationStrategyRegistry(registered_strategy_ids={ids!r})"

    def __reduce__(self) -> Never:
        """Disable pickling——prevent accidental secret leakage via persistence."""
        raise TypeError("ValidationStrategyRegistry is not picklable")


# ============================================================================
# Module-level default registry
# ============================================================================


_DEFAULT_REGISTRY: ValidationStrategyRegistry = ValidationStrategyRegistry()


def get_default_validation_strategy_registry() -> ValidationStrategyRegistry:
    """Module-level accessor for default registry."""
    return _DEFAULT_REGISTRY
