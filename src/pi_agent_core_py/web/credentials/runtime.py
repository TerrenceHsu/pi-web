"""Credential Runtime Composition Root（P1-E1-4A）.

构造 CredentialRuntimeState——含 SQLiteCredentialStore + SecretStoreRouter +
CredentialService + CredentialReadiness.

**关键不变量**：

1. **绝对文件型 DB path**——App 创建时一次性 expanduser + resolve（拒绝 :memory: /
   SQLite URI / 相对路径）；请求处理时不重新解析
2. **独立 aiosqlite.Connection**——不共享 session/extension store 的 connection
   （`:memory:` 在独立 connection 语义下会变成不同空数据库——拒绝）
3. **AsyncExitStack 部分初始化回滚**——任何步骤失败自动 close 已 enter 的资源
4. **keyring 不可用不阻塞启动**——readiness=degraded；memory 模式是用户主动禁用
   keyring，readiness=ready
5. **不自动降级 keyring→session_only**——语义失真
6. **不缓存 secret / 不持久化 runtime 引用 / 不发网络**
7. **readiness 区分 app-level ready 与 credential-level ready/degraded**
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ...providers.registry import (
    ProviderRegistry,
    list_provider_definitions,
)
from ...secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    OSKeyringSecretStore,
    SecretStore,
)
from ..local_web_security import WebSecurityConfig
from ..providers.validation import (
    get_default_validation_strategy_registry,
)
from .secret_store import SecretStoreRouter
from .service import CredentialService
from .store import SQLiteCredentialStore

__all__ = [
    "CredentialRuntimeConfigError",
    "CredentialWebSecurityConfigurationError",
    "CredentialRuntimeConfig",
    "CredentialReadiness",
    "CredentialRuntimeState",
    "credential_runtime_context",
    "resolve_credential_api_configuration",
    "ResolvedCredentialApiConfig",
]


SecretBackendMode = Literal["auto", "keyring", "memory"]
ReadinessStatus = Literal["ready", "degraded"]


# ============================================================================
# Errors
# ============================================================================


class CredentialRuntimeConfigError(RuntimeError):
    """Credential runtime 配置错误——App 创建阶段抛出.

    不会进 app.state（未到达写入步骤）.
    """


class CredentialWebSecurityConfigurationError(RuntimeError):
    """Credential API 安全配置错误（P1-E1-4B3）.

    在 create_app() 阶段抛出——当 enable_credentials_api=True 但
    TrustedHost / Runtime / 文件型 SQLite 任一未满足时.

    Examples:
        enable_credentials_api=True + enable_trusted_host=False
        enable_credentials_api=True + db_path=':memory:'
        enable_credentials_api=True + enable_credential_runtime=False
    """


@dataclass(frozen=True)
class ResolvedCredentialApiConfig:
    """Final flags resolved by create_app() at construction time.

    All three flags are interlocked——API requires Runtime + TrustedHost.
    """

    api_enabled: bool
    runtime_enabled: bool
    trusted_host_enabled: bool


def _is_file_type_db_path(db_path: str | Path | None) -> bool:
    """Check if db_path is a real file-type SQLite path (not :memory: / URI)."""
    if db_path is None:
        return False
    s = str(db_path)
    if s == ":memory:":
        return False
    if s.startswith("file:"):
        return False
    return True


def resolve_credential_api_configuration(
    *,
    enable_credentials_api: bool | None,
    enable_credential_runtime: bool | None,
    enable_trusted_host: bool,
    db_path: str | Path | None,
) -> ResolvedCredentialApiConfig:
    """Resolve final (api_enabled, runtime_enabled, trusted_host_enabled)
    per the interlock rules.

    Rules:
        - enable_credentials_api=None: CONSERVATIVE auto——enable API only when
          Runtime will be active AND user already enabled TrustedHost.
          This preserves backward compat for existing create_app callers.
        - enable_credentials_api=True: require Runtime + TrustedHost +
          file-type SQLite——raise on violation
        - enable_credentials_api=False: API disabled; TrustedHost stays as-is
        - enable_credentials_api=True + enable_trusted_host=False → raise
        - enable_credentials_api=True + :memory: db_path → raise

    Raises:
        CredentialWebSecurityConfigurationError
    """
    file_type = _is_file_type_db_path(db_path)

    # Resolve runtime flag
    if enable_credential_runtime is None:
        runtime_enabled = file_type
    else:
        runtime_enabled = enable_credential_runtime
        if runtime_enabled and not file_type:
            raise CredentialWebSecurityConfigurationError(
                "enable_credential_runtime=True requires file-type SQLite "
                "db_path (got :memory: / URI / None)"
            )

    # Resolve API flag
    if enable_credentials_api is None:
        # Conservative auto: only enable when BOTH runtime AND TrustedHost
        # are active. Existing callers with default enable_trusted_host=False
        # get API disabled——preserves backward compat.
        api_enabled = runtime_enabled and enable_trusted_host
        trusted_host_enabled = enable_trusted_host
    elif enable_credentials_api:
        api_enabled = True
        if not runtime_enabled:
            raise CredentialWebSecurityConfigurationError(
                "enable_credentials_api=True requires Credential Runtime "
                "(set db_path to a file path / enable_credential_runtime=True)"
            )
        if not enable_trusted_host:
            raise CredentialWebSecurityConfigurationError(
                "enable_credentials_api=True requires enable_trusted_host=True "
                "(TrustedHost must be enabled when Credential API is mounted)"
            )
        trusted_host_enabled = enable_trusted_host
    else:
        api_enabled = False
        trusted_host_enabled = enable_trusted_host

    return ResolvedCredentialApiConfig(
        api_enabled=api_enabled,
        runtime_enabled=runtime_enabled,
        trusted_host_enabled=trusted_host_enabled,
    )


# ============================================================================
# Config
# ============================================================================


@dataclass(frozen=True)
class CredentialRuntimeConfig:
    """Frozen configuration resolved once at app creation.

    Attributes:
        database_path: 绝对文件型 SQLite 路径（拒绝 :memory: / URI / 相对）.
        secret_backend_mode: auto / keyring / memory.
        web_security: WebSecurityConfig（localhost 边界）.
    """

    database_path: Path
    secret_backend_mode: SecretBackendMode
    web_security: WebSecurityConfig


@dataclass(frozen=True)
class CredentialReadiness:
    """Credential 子系统 readiness——内部诊断用.

    App-level `status` 在 keyring 缺失时仍为 `ready`——本字段独立报告
    credentials 子系统状态.
    """

    status: ReadinessStatus
    configured_backend: SecretBackendMode
    keyring_available: bool
    reason_code: str | None


@dataclass(repr=False)
class CredentialRuntimeState:
    """Owned resources for the Credentials subsystem.

    Held in `app.state.credential_runtime` but **never** serialized or sent
    to clients.

    `repr=False`: 默认 object repr 不暴露 Store 内部映射 / Repository
    connection 细节.
    """

    config: CredentialRuntimeConfig
    repository: SQLiteCredentialStore
    router: SecretStoreRouter
    service: CredentialService
    readiness: CredentialReadiness
    # Internal: 不暴露额外属性——__slots__ 风格控制
    _owned_resources: list[object] = field(default_factory=list, repr=False, compare=False)


# ============================================================================
# DB path resolution
# ============================================================================


def _resolve_db_path(configured: str | Path) -> Path:
    """Resolve DB path at app creation; never re-resolve per request.

    Rejects:
        - empty path
        - `:memory:`（independent connection semantics）
        - SQLite URI form (`file:...?...`)
        - relative path（after expanduser）
    """
    if not configured:
        raise CredentialRuntimeConfigError(
            "credential DB path must be non-empty"
        )
    if isinstance(configured, str):
        s = configured.strip()
        if s == ":memory:":
            raise CredentialRuntimeConfigError(
                "credential DB path must be a filesystem path "
                "(:memory: is rejected——independent connection "
                "would see a different empty database)"
            )
        if s.startswith("file:"):
            raise CredentialRuntimeConfigError(
                "credential DB path must not use SQLite URI form "
                "(file:...?...)——only plain filesystem paths are supported"
            )
        configured_for_expand = s
    else:
        # Path object——转 str 检查特殊形式后再 expanduser
        s = str(configured)
        if s == ":memory:":
            raise CredentialRuntimeConfigError(
                "credential DB path must be a filesystem path "
                "(:memory: rejected——independent connection semantics)"
            )
        if s.startswith("file:"):
            raise CredentialRuntimeConfigError(
                "credential DB path must not use SQLite URI form"
            )
        configured_for_expand = s

    expanded = Path(configured_for_expand).expanduser()
    if not expanded.is_absolute():
        raise CredentialRuntimeConfigError(
            f"credential DB path must be absolute after expanduser "
            f"(got {configured!r})"
        )
    return expanded.resolve(strict=False)


# ============================================================================
# SecretStoreRouter construction
# ============================================================================


async def _probe_keyring_available() -> bool:
    """Verify Keyring discovery and real write/read/delete capability.

    A backend type check is not sufficient on Windows because Credential
    Manager can reject ``CredWrite`` for restricted/non-interactive process
    tokens.  Returns False for package/backend failures, write failures,
    round-trip mismatches, cleanup failures, or any unexpected exception.
    Never raises.
    """
    try:
        store = OSKeyringSecretStore()
    except Exception:
        return False
    try:
        return bool(await store.probe_write_access())
    except Exception:
        return False


async def _build_secret_router(
    mode: SecretBackendMode,
) -> tuple[SecretStoreRouter, bool]:
    """Construct SecretStoreRouter per mode.

    Returns:
        (router, keyring_available)
    """
    session_only_store: SecretStore = InMemorySecretStore()
    env_store: SecretStore = EnvSecretStore()

    keyring_store: SecretStore | None = None
    keyring_available = False

    if mode == "memory":
        # User-explicit disable of keyring——do NOT mark as degraded
        keyring_store = None
        keyring_available = False
    elif mode in ("auto", "keyring"):
        # Probe OSKeyringSecretStore——failure leaves keyring=None
        keyring_available = await _probe_keyring_available()
        if keyring_available:
            try:
                keyring_store = OSKeyringSecretStore()
            except Exception:
                keyring_store = None
                keyring_available = False
        else:
            keyring_store = None
    else:
        raise CredentialRuntimeConfigError(
            f"unknown secret_backend mode: {mode!r} "
            "(expected 'auto' / 'keyring' / 'memory')"
        )

    router = SecretStoreRouter(
        stores={
            "keyring": keyring_store,
            "session_only": session_only_store,
            "env": env_store,
        }
    )
    return router, keyring_available


def _compute_readiness(
    mode: SecretBackendMode,
    keyring_available: bool,
) -> CredentialReadiness:
    """Map (mode, keyring_available) → CredentialReadiness."""
    if mode == "memory":
        # User-explicit disable——ready, not degraded
        return CredentialReadiness(
            status="ready",
            configured_backend="memory",
            keyring_available=False,
            reason_code=None,
        )
    # auto / keyring
    if keyring_available:
        return CredentialReadiness(
            status="ready",
            configured_backend=mode,
            keyring_available=True,
            reason_code=None,
        )
    return CredentialReadiness(
        status="degraded",
        configured_backend=mode,
        keyring_available=False,
        reason_code="keyring_unavailable",
    )


# ============================================================================
# Repository lifecycle (async context manager)
# ============================================================================


@asynccontextmanager
async def _open_credential_repository(
    db_path: Path,
) -> AsyncIterator[SQLiteCredentialStore]:
    """Open + close SQLiteCredentialStore as async CM.

    Schema init happens inside SQLiteCredentialStore.open(). Any failure
    (version mismatch / corruption / I/O) raises here——caller's ExitStack
    will propagate and clean up.
    """
    store = await SQLiteCredentialStore.open(str(db_path))
    try:
        yield store
    finally:
        try:
            await store.close()
        except Exception:
            # Close 失败不抛——避免掩盖 init 路径的原始异常
            pass


# ============================================================================
# CredentialService factory
# ============================================================================


def _build_credential_service(
    *,
    repository: SQLiteCredentialStore,
    router: SecretStoreRouter,
) -> CredentialService:
    """Build CredentialService with default provider + strategy registries.

    No network calls during construction.
    """
    provider_registry = ProviderRegistry(list_provider_definitions())
    strategy_registry = get_default_validation_strategy_registry()
    return CredentialService(
        repository=repository,
        router=router,
        provider_registry=provider_registry,
        validation_strategy_registry=strategy_registry,
    )


# ============================================================================
# Composition Root——credential_runtime_context
# ============================================================================


@asynccontextmanager
async def credential_runtime_context(
    config: CredentialRuntimeConfig,
) -> AsyncIterator[CredentialRuntimeState]:
    """Construct CredentialRuntimeState with AsyncExitStack rollback safety.

    Startup sequence:
        1. (config already validated——CredentialRuntimeConfig is frozen)
        2. Build SecretStoreRouter (probe keyring availability)
        3. Open SQLiteCredentialStore (independent connection + schema init)
        4. Build CredentialService
        5. Compute readiness
        6. Yield runtime

    Any step failure → AsyncExitStack auto-closes opened resources.

    Shutdown (after yield):
        - AsyncExitStack closes repository's owned connection
        - Release references to InMemorySecretStore (GC)
        - Idempotent close() (handled by _open_credential_repository)
    """
    async with AsyncExitStack() as stack:
        # Step 2: Router——pure memory construction, no resources to clean
        router, keyring_available = await _build_secret_router(
            config.secret_backend_mode
        )

        # Step 3: Repository——owns aiosqlite connection
        # _open_credential_repository handles its own close in its finally
        repository = await stack.enter_async_context(
            _open_credential_repository(config.database_path)
        )

        # Step 4: Service——pure memory
        service = _build_credential_service(
            repository=repository,
            router=router,
        )

        # Step 5: Readiness
        readiness = _compute_readiness(
            config.secret_backend_mode, keyring_available
        )

        # Step 6: All success → build runtime + yield
        runtime = CredentialRuntimeState(
            config=config,
            repository=repository,
            router=router,
            service=service,
            readiness=readiness,
        )

        yield runtime
        # After yield: AsyncExitStack closes repository's connection
        # via _open_credential_repository's finally block.


# ============================================================================
# Convenience: build from raw kwargs
# ============================================================================


def build_credential_runtime_config(
    *,
    database_path: str | Path,
    secret_backend_mode: str,
    web_security: WebSecurityConfig,
) -> CredentialRuntimeConfig:
    """Validate inputs + build frozen CredentialRuntimeConfig.

    Use this from create_app() / lifespan——single point of validation.

    Raises:
        CredentialRuntimeConfigError: unknown backend / relative path /
            :memory: / SQLite URI / empty path.
    """
    if secret_backend_mode not in ("auto", "keyring", "memory"):
        raise CredentialRuntimeConfigError(
            f"unknown PI_AGENT_SECRET_BACKEND: {secret_backend_mode!r} "
            "(expected 'auto' / 'keyring' / 'memory')"
        )
    resolved_path = _resolve_db_path(database_path)
    return CredentialRuntimeConfig(
        database_path=resolved_path,
        secret_backend_mode=secret_backend_mode,  # type: ignore[arg-type]
        web_security=web_security,
    )
