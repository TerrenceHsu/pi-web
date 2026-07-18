"""Provider Config runtime composition root（P1-E2-3B1）.

镜像 ``credentials_runtime.py``（E1-4A）的结构，把 E2-1 的 ``SQLiteProviderConfigStore``
与 E2-2 的 ``ProviderConfigService`` 组合成可挂入 ``app.state`` 的 frozen runtime。

**职责边界（E2-3B1）**：
- ✅ ``ProviderConfigRuntimeState``（frozen dataclass，含 store + service）
- ✅ ``resolve_provider_profiles_api_configuration()``——校验 enable_provider_profiles_api
   与 Credential runtime / TrustedHost / file SQLite 的关系
- ✅ ``provider_config_runtime_context()``——AsyncExitStack 包裹 Store 生命周期
- ✅ ``ProviderConfigWebSecurityConfigurationError``——App 创建阶段配置错误
- ❌ 不创建 HTTP client / 不读 Secret
- ❌ 不挂 Router（E2-3B2 负责）
- ❌ 不接 Session 创建 handler（E2-3B3 负责）

**与 Credential runtime 的依赖关系**：
- Provider Config Runtime **必须在** Credential Runtime 之后初始化
- 因为 ProviderConfigService 依赖 CredentialService（safe API）
- shutdown 顺序相反：先关 Provider Config，再关 Credential
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from ..providers.registry import _DEFAULT_REGISTRY, ProviderRegistry
from .credentials_runtime import _is_file_type_db_path
from .credentials_service import CredentialService
from .provider_config_service import ProviderConfigService, SessionExistsCallback
from .provider_config_store import SQLiteProviderConfigStore

# ============================================================================
# Errors
# ============================================================================


class ProviderConfigWebSecurityConfigurationError(RuntimeError):
    """Provider Profiles API 安全配置错误（App 创建阶段）.

    Examples:
        - ``enable_provider_profiles_api=True`` 但 Credential API 未启用
        - ``enable_provider_profiles_api=True`` 但 TrustedHost 未启用
        - ``enable_provider_profiles_api=True`` 但 db_path 是 :memory:
    """


# ============================================================================
# Resolver
# ============================================================================


@dataclass(frozen=True)
class ResolvedProviderProfilesApiConfig:
    """Result of resolve_provider_profiles_api_configuration()."""

    api_enabled: bool
    runtime_enabled: bool


def resolve_provider_profiles_api_configuration(
    *,
    enable_provider_profiles_api: bool | None,
    credential_api_enabled: bool,
    credential_runtime_enabled: bool,
    trusted_host_enabled: bool,
    db_path: str | Path | None,
) -> ResolvedProviderProfilesApiConfig:
    """Resolve ``enable_provider_profiles_api`` flag against Credential + TrustedHost + DB.

    Semantics:
        - ``False`` → fully disabled (no Store open, no Router mount)
        - ``True`` → 强制要求：Credential API enabled + TrustedHost + file SQLite
        - ``None``（auto）→ 仅当 Credential API enabled + TrustedHost + file SQLite
          全部满足时启用——否则禁用（保守）

    Raises:
        ProviderConfigWebSecurityConfigurationError: 显式 ``True`` 但前置条件不满足.
    """
    file_db = _is_file_type_db_path(db_path)

    if enable_provider_profiles_api is False:
        return ResolvedProviderProfilesApiConfig(
            api_enabled=False,
            runtime_enabled=False,
        )

    if enable_provider_profiles_api is True:
        if not credential_runtime_enabled:
            raise ProviderConfigWebSecurityConfigurationError(
                "enable_provider_profiles_api=True requires Credential runtime enabled"
            )
        if not credential_api_enabled:
            raise ProviderConfigWebSecurityConfigurationError(
                "enable_provider_profiles_api=True requires Credential API enabled"
            )
        if not trusted_host_enabled:
            raise ProviderConfigWebSecurityConfigurationError(
                "enable_provider_profiles_api=True requires TrustedHost enabled"
            )
        if not file_db:
            raise ProviderConfigWebSecurityConfigurationError(
                "enable_provider_profiles_api=True requires file-type SQLite DB "
                "(:memory: / URI rejected)"
            )
        return ResolvedProviderProfilesApiConfig(
            api_enabled=True,
            runtime_enabled=True,
        )

    # None: conservative auto—only when ALL conditions already verified
    if (
        credential_runtime_enabled
        and credential_api_enabled
        and trusted_host_enabled
        and file_db
    ):
        return ResolvedProviderProfilesApiConfig(
            api_enabled=True,
            runtime_enabled=True,
        )
    return ResolvedProviderProfilesApiConfig(
        api_enabled=False,
        runtime_enabled=False,
    )


# ============================================================================
# Runtime state
# ============================================================================


@dataclass(frozen=True)
class ProviderConfigRuntimeState:
    """Frozen runtime handle stored in ``app.state.provider_config_runtime``.

    repr 默认安全——``SQLiteProviderConfigStore`` / ``ProviderConfigService`` 不暴露
    connection / secret. 两者都已显式禁用 ``repr`` 泄漏（见各自模块）.
    """

    store: SQLiteProviderConfigStore
    service: ProviderConfigService


# ============================================================================
# Composition root (async context manager)
# ============================================================================


@asynccontextmanager
async def provider_config_runtime_context(
    *,
    database_path: str | Path,
    credential_service: CredentialService,
    provider_registry: ProviderRegistry | None = None,
    session_exists: SessionExistsCallback,
) -> AsyncIterator[ProviderConfigRuntimeState]:
    """Open Store + construct Service; reverse-order shutdown via AsyncExitStack.

    Args:
        database_path: 绝对文件路径（与 Credential / Session Store 同一文件）.
        credential_service: from Credential runtime（safe API—no secret reads）.
        provider_registry: 默认用 ``_DEFAULT_REGISTRY``（E1 frozen）.
        session_exists: async callable ``Callable[[str], Awaitable[bool]]``.

    Yields:
        ProviderConfigRuntimeState.

    Raises:
        ProviderConfigStoreError: Store 初始化失败（schema 损坏 / path 非法）.
    """
    registry = provider_registry if provider_registry is not None else _DEFAULT_REGISTRY

    async with AsyncExitStack() as stack:
        store = await SQLiteProviderConfigStore.open(database_path)
        # push_async_callback takes a no-arg async callable
        stack.push_async_callback(store.close)

        service = ProviderConfigService(
            store=store,
            provider_registry=registry,
            credential_service=credential_service,
            session_exists=session_exists,
        )
        yield ProviderConfigRuntimeState(store=store, service=service)


__all__ = [
    "ProviderConfigWebSecurityConfigurationError",
    "ProviderConfigRuntimeState",
    "ResolvedProviderProfilesApiConfig",
    "resolve_provider_profiles_api_configuration",
    "provider_config_runtime_context",
]
