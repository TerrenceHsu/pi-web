"""Request-scoped Provider Runtime（P1-E M1-4）.

每个 Prompt / Regenerate 请求**临时**把 ``harness.agent.client`` 替换为由当前
Session Binding 决定的 Provider Adapter，请求结束后恢复原 client 并关闭临时
ModelClient. M1-4 仅提供 Runtime，**不接入** Prompt / Regenerate 入口——
那是 M1-5 / M1-6 的事.

生命周期（请求级，由调用方驱动）::

    SessionModelBinding  (E2 持久化)
        ↓
    ProviderProfile      (E2 持久化)
        ↓
    CredentialService.resolve_secret_for_request()  (E1 窄接口)
        ↓
    ProviderRegistry.get()
        ↓
    create_provider()    (M1-3 Factory)
        ↓
    ProviderAdapter
        ↓
    ModelClient(adapter)
        ↓
    临时替换 harness.agent.client
        ↓
    finally: 恢复 original + 关闭 request client

职责边界:
- ✅ 解析 SessionModelBinding → Profile → credential_id → secret → adapter
- ✅ 临时替换 ``harness.agent.client``，请求结束恢复 + 关闭
- ❌ 不修改 ``web/app.py`` / Prompt / Regenerate 入口（M1-5 / M1-6）
- ❌ 不直接依赖 SecretStoreRouter / CredentialRepository / 具体 SecretStore 实现
- ❌ 不直接依赖 SQLiteProviderConfigStore / HTTP Client
- ❌ 不缓存 secret / adapter
- ❌ 不写 Message / Event / Snapshot / Revision / context.metadata
- ❌ 不创建第二把 active-request lock（M1-5 由调用方现有锁串行化）

依赖方向:
- CredentialService（E1）— 通过 ``resolve_secret_for_request`` 窄接口
- ProviderConfigService（E2）— 通过 ``get_session_binding`` / ``get_profile``
- ProviderRegistry（M1-0）— 通过 ``get(provider_id)``
- create_provider（M1-3）— ProviderFactoryCallable 注入点
- ModelClient（Core Runtime）— 仅 wrap Adapter + close

安全不变量:
- ``RequestProviderSelection.credential_id`` ``repr=False``
- 所有 wrap 异常 ``from None``，固定 4 条安全消息
- ``secret`` 局部变量 ``finally: del``——缩短生命周期
- ``RequestProviderRuntime`` / ``RequestProviderSelection`` 不实现 ``__repr__``
  暴露 secret——``credential_id`` 已 ``repr=False``，其它字段无敏感信息
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from ..harness import AgentHarness
from ..model_client import ModelClient
from ..providers.base import ProviderAdapter
from ..providers.errors import ProviderConfigError
from ..providers.factory import create_provider
from ..providers.registry import ProviderRegistry
from .credentials.errors import (
    CredentialRequestSecretBackendError,
    CredentialRequestSecretUnavailableError,
)
from .credentials.service import CredentialService
from .credentials.store import CredentialNotFoundError
from .provider_config_service import ProviderConfigService
from .provider_config_store import ProviderProfileNotFoundError

__all__ = [
    "RequestProviderSelection",
    "RequestProviderRuntimeError",
    "ProviderSelectionNotFoundError",
    "ProviderSelectionDisabledError",
    "ProviderSelectionUnavailableError",
    "ProviderInitializationError",
    "ProviderFactoryCallable",
    "RequestProviderRuntime",
]


# ============================================================================
# Selection snapshot
# ============================================================================


@dataclass(frozen=True)
class RequestProviderSelection:
    """请求启动瞬间的不可变 Provider 选择快照.

    M1-0 已冻结：请求启动后 Provider/Model 不可变——运行中修改 Binding 只影响
    下一次请求.

    ``credential_id`` 仅用于 ``build_adapter`` 阶段调用
    ``CredentialService.resolve_secret_for_request``；不进入 repr、日志、事件
    或持久化. ``field(repr=False)`` 防止 ``repr(selection)`` 暴露内部 ID.
    """

    profile_id: str
    provider_id: str
    model_id: str
    selection_source: Literal["default", "explicit"]
    credential_id: str = field(repr=False)


# ============================================================================
# Error hierarchy
# ============================================================================


class RequestProviderRuntimeError(Exception):
    """RequestProviderRuntime 所有固定安全错误的基类."""


class ProviderSelectionNotFoundError(RequestProviderRuntimeError):
    """Binding 指向的 Profile 不存在. 固定消息：'session provider profile is unavailable'."""


class ProviderSelectionDisabledError(RequestProviderRuntimeError):
    """Binding 指向的 Profile 已禁用. 固定消息：'session provider profile is disabled'."""


class ProviderSelectionUnavailableError(RequestProviderRuntimeError):
    """Credential 不可用（缺失 / backend 不可用 / secret 为空）.
    固定消息：'session provider credential is unavailable'.
    """


class ProviderInitializationError(RequestProviderRuntimeError):
    """ProviderDefinition 不存在或 Factory 构造失败.
    固定消息：'session provider initialization failed'.
    """


_MSG_PROFILE_UNAVAILABLE = "session provider profile is unavailable"
_MSG_PROFILE_DISABLED = "session provider profile is disabled"
_MSG_CREDENTIAL_UNAVAILABLE = "session provider credential is unavailable"
_MSG_INITIALIZATION_FAILED = "session provider initialization failed"


# ============================================================================
# Factory callable injection point
# ============================================================================


ProviderFactoryCallable = Callable[..., ProviderAdapter]


# ============================================================================
# Runtime
# ============================================================================


class RequestProviderRuntime:
    """请求级 Provider Adapter 解析 + Harness 临时绑定.

    一个 Runtime 实例可服务多个 Session / 多次请求——``resolve_selection`` +
    ``build_adapter`` 都是无状态函数（调用方传 ``session_id`` / ``selection``），
    Runtime 本身不缓存任何请求级状态.
    """

    def __init__(
        self,
        *,
        provider_config_service: ProviderConfigService,
        credential_service: CredentialService,
        provider_registry: ProviderRegistry,
        provider_factory: ProviderFactoryCallable = create_provider,
    ) -> None:
        self._provider_config_service = provider_config_service
        self._credential_service = credential_service
        self._provider_registry = provider_registry
        self._provider_factory = provider_factory

    # ------------------------------------------------------------------
    # resolve_selection
    # ------------------------------------------------------------------

    async def resolve_selection(
        self,
        session_id: str,
    ) -> RequestProviderSelection | None:
        """Snapshot the Session's Provider selection.

        Returns:
            None: Session 无 Binding（legacy client fallback path）.
            RequestProviderSelection: 启动瞬间的不可变快照.

        Raises:
            SessionNotFoundError: 传播——Session 不存在（E2 领域错误）.
            ProviderSelectionNotFoundError: Binding 指向的 Profile 不存在.
            ProviderSelectionDisabledError: Profile.enabled=False.
            ProviderInitializationError: ProviderDefinition 不在 registry.
        """
        # binding is None → legacy client path（旧 Session 兼容）
        binding = await self._provider_config_service.get_session_binding(
            session_id,
        )
        if binding is None:
            return None

        # Profile 一次性读取——之后不再访问 Profile
        try:
            profile = await self._provider_config_service.get_profile(
                binding.profile_id,
            )
        except ProviderProfileNotFoundError:
            raise ProviderSelectionNotFoundError(_MSG_PROFILE_UNAVAILABLE) from None

        # disabled Profile 拒绝——不 fallback 到 default Profile
        if not profile.enabled:
            raise ProviderSelectionDisabledError(_MSG_PROFILE_DISABLED) from None

        # 确认 Provider 当前仍存在（不读 Secret）——Registry 是同步操作
        definition = self._provider_registry.get(profile.provider_id)
        if definition is None:
            raise ProviderInitializationError(_MSG_INITIALIZATION_FAILED) from None

        # model_id 来自 Binding——不从 Profile.default_model 投影
        return RequestProviderSelection(
            profile_id=profile.id,
            provider_id=profile.provider_id,
            model_id=binding.model_id,
            selection_source=binding.source,
            credential_id=profile.credential_id,
        )

    # ------------------------------------------------------------------
    # build_adapter
    # ------------------------------------------------------------------

    async def build_adapter(
        self,
        selection: RequestProviderSelection,
    ) -> ProviderAdapter:
        """Resolve secret + construct ProviderAdapter.

        Raises:
            ProviderSelectionUnavailableError: Credential 缺失 / backend 不可用
                                              / secret 为空.
            ProviderInitializationError: ProviderDefinition 缺失 / Factory 失败.
        """
        # 1. CredentialService 窄接口——一次 secret 读取
        try:
            secret = await self._credential_service.resolve_secret_for_request(
                selection.credential_id,
            )
        except (
            CredentialNotFoundError,
            CredentialRequestSecretUnavailableError,
            CredentialRequestSecretBackendError,
        ):
            raise ProviderSelectionUnavailableError(
                _MSG_CREDENTIAL_UNAVAILABLE
            ) from None

        # 2-5. Factory 构造——finally 缩短 secret 生命周期
        try:
            definition = self._provider_registry.get(selection.provider_id)
            if definition is None:
                raise ProviderInitializationError(
                    _MSG_INITIALIZATION_FAILED
                ) from None

            try:
                return self._provider_factory(
                    provider_definition=definition,
                    api_key=secret,
                    model_id=selection.model_id,
                )
            except ProviderConfigError:
                raise ProviderInitializationError(
                    _MSG_INITIALIZATION_FAILED
                ) from None
            except Exception:
                raise ProviderInitializationError(
                    _MSG_INITIALIZATION_FAILED
                ) from None
        finally:
            del secret

    # ------------------------------------------------------------------
    # bind_to_harness
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def bind_to_harness(
        self,
        *,
        harness: AgentHarness,
        selection: RequestProviderSelection | None,
    ) -> AsyncIterator[RequestProviderSelection | None]:
        """Temporarily swap ``harness.agent.client`` with a request-scoped client.

        ``selection is None`` 走 legacy 兼容路径：不替换 / 不读 secret /
        不关闭 original.

        非 None 路径：build Adapter → ModelClient → 临时替换 → yield →
        finally 先恢复 original client，再 cancellation-safely 关闭 request
        client.

        请求执行期 ``harness.agent.client`` 指向 request_client；调用方通过
        ``async with runtime.bind_to_harness(...) as selection:`` 获取 selection
        快照（``None`` 表示走 legacy 路径）.
        """
        if selection is None:
            # 无 Binding——不读 Secret / 不调 Factory / 不动 original client
            yield None
            return

        adapter = await self.build_adapter(selection)
        request_client = ModelClient(adapter)

        original_client = harness.agent.client
        harness.agent.client = request_client

        try:
            yield selection
        finally:
            # 先恢复 original client——即使 close 卡住 / 取消，业务结果不被覆盖
            harness.agent.client = original_client
            await _close_request_client_safely(request_client)


# ============================================================================
# close helper
# ============================================================================


async def _close_request_client_safely(client: ModelClient) -> None:
    """Cancellation-safe close.

    ModelClient.close() 幂等 + 吞 ``Exception``——但 ``asyncio.CancelledError``
    在 Python 3.8+ 是 ``BaseException`` 子类，不被吞；本 helper 用
    ``asyncio.shield`` 保证 close 本身完成，同时让 CancelledError 原样传播.
    """
    close_task = asyncio.create_task(client.close())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        try:
            await close_task
        finally:
            raise
