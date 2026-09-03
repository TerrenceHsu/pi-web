"""ProviderConfigService — business composition layer（P1-E2-2）.

组合 4 个安全依赖，对外暴露 Profile / Binding / Models 的业务 API：

    ProviderConfigStore          (E2-1)   持久化
    ProviderRegistry             (E1-1)   provider 存在性 + 显示名
    CredentialService            (E1-3A)  安全 get/list CredentialView
    session_exists callable      (E2-3 接入点)  Session 存在性

**职责边界（E2-2）**：
- ✅ ``ProviderProfileView`` 业务投影（含 status 派生 + masked_value 投影）
- ✅ ``derive_profile_status`` 纯函数（provider-scoped validation 状态）
- ✅ Profile CRUD（provider_id 校验 + credential 存在性校验 + 名称/模型规范化）
- ✅ list_models（按 ``provider_id`` 返回静态建议）
- ✅ Session binding get/set（依赖注入的 ``session_exists`` callable）
- ❌ 不读取 Secret（``CredentialService`` 只调 ``get`` / ``list`` 安全 API）
- ❌ 不调 SecretStore / SecretStoreRouter
- ❌ 不创建 HTTP client / 不访问网络
- ❌ 不实现 ``initialize_new_session_binding``（E2-3A 审计后）
- ❌ 不修改 ``provider_config_store.py`` / ``web/app.py`` / Core Runtime
- ❌ 不暴露 ``secret_ref`` / ``fingerprint`` / 完整 ``CredentialRecord`` / Keyring service name
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ...ai.providers.registry import ProviderDefinition, ProviderRegistry
from ..credentials.service import CredentialService, CredentialView
from ..credentials.store import CredentialNotFoundError
from .config_store import (
    ProviderConfigStoreError,
    ProviderProfile,
    ProviderProfileNotFoundError,
    ProviderProfileStateError,
    SessionModelBinding,
    SQLiteProviderConfigStore,
)
from .model_options import (
    InvalidModelIdError,
    ModelOption,
    get_static_model_options,
    normalize_model_id,
)

# ============================================================================
# Types
# ============================================================================


ProfileStatus = Literal[
    "ready",
    "disabled",
    "needs_credential",
    "needs_key",
    "backend_unavailable",
    "credential_invalid",
    "credential_error",
]


SessionExistsCallback = Callable[[str], Awaitable[bool]]


# ============================================================================
# Errors
# ============================================================================


class ProviderConfigServiceError(Exception):
    """Base class for ProviderConfigService errors."""


class UnknownProviderError(ProviderConfigServiceError):
    """Raised when ``provider_id`` is not registered in ProviderRegistry."""


class CredentialNotFoundForProfileError(ProviderConfigServiceError):
    """Raised when ``credential_id`` referenced by create/update does not exist.

    Distinguished from ``CredentialNotFoundError`` (E1 internal) so callers can
    attribute the failure to Profile configuration rather than Credential lookup.
    """


class SessionNotFoundError(ProviderConfigServiceError):
    """Raised when ``session_id`` does not exist (verified via injected callback)."""


class ProviderProfileDisabledError(ProviderConfigServiceError):
    """Raised when ``set_session_binding`` targets an ``enabled=False`` Profile.

    Existing Bindings on a Profile that later becomes disabled are NOT
    auto-cleared—this error only applies to NEW binding writes.
    """


class InvalidProfileNameError(ProviderConfigServiceError):
    """Raised when ``name`` fails normalization.

    **Safety**: ``str(exc)`` must NOT echo the input value.
    """


# InvalidModelIdError is re-exported from model_options for convenience.


# ============================================================================
# ProviderProfileView (business projection)
# ============================================================================


@dataclass(frozen=True)
class ProviderProfileView:
    """Business projection of a Profile + its current Credential state.

    **Never** contains ``secret_ref`` / ``fingerprint`` / full CredentialRecord /
    Keyring service name / API Key / Authorization.
    """

    id: str
    name: str
    provider_id: str
    provider_display_name: str
    credential_id: str
    credential_masked_value: str | None
    default_model: str
    enabled: bool
    is_default: bool
    status: ProfileStatus
    created_at: int
    updated_at: int


# ============================================================================
# Profile status derivation (pure function)
# ============================================================================


def derive_profile_status(
    profile: ProviderProfile,
    credential: CredentialView | None,
) -> ProfileStatus:
    """Derive runtime Profile status. Pure function (sync).

    Priority (fixed, must not be reordered):
        1. ``profile.enabled = False`` → ``disabled``
        2. ``credential is None`` → ``needs_credential``
        3. ``credential.storage_status = needs_key`` → ``needs_key``
        4. ``credential.storage_status = backend_unavailable`` → ``backend_unavailable``
        5. ``credential.last_validated_provider_id == profile.provider_id``
           AND ``validation_status = invalid`` → ``credential_invalid``
        6. ``credential.last_validated_provider_id == profile.provider_id``
           AND ``validation_status = error`` → ``credential_error``
        7. else → ``ready`` (includes ``never_validated``, ``valid``, and
           validation results from a *different* provider)

    **Provider scope invariant**:
        ``invalid`` / ``error`` only apply when the validation was performed
        against the *same* ``provider_id`` as the Profile. Other providers'
        validation results do **not** pollute the current Profile's status
        (e.g., a Credential validated against Anthropic and failing must not
        mark a GLM Profile referencing the same Credential as ``credential_invalid``).
    """
    if not profile.enabled:
        return "disabled"
    if credential is None:
        return "needs_credential"
    if credential.storage_status == "needs_key":
        return "needs_key"
    if credential.storage_status == "backend_unavailable":
        return "backend_unavailable"
    if credential.last_validated_provider_id == profile.provider_id:
        if credential.validation_status == "invalid":
            return "credential_invalid"
        if credential.validation_status == "error":
            return "credential_error"
    return "ready"


# ============================================================================
# Name normalization
# ============================================================================


_MIN_NAME_LEN = 1
_MAX_NAME_LEN = 128
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


def normalize_profile_name(value: str) -> str:
    """Validate and trim Profile name.

    Rules:
        - must be ``str``
        - non-empty after ``strip()``
        - length 1–128 (after trim)
        - reject NUL / CR / LF / any ASCII control character (< 0x20 or 0x7F)
        - preserve normal Unicode

    Raises:
        InvalidProfileNameError: validation failed. ``str(exc)`` does NOT
            include the input value.
    """
    if not isinstance(value, str):
        raise InvalidProfileNameError("name must be a string")
    trimmed = value.strip()
    if len(trimmed) < _MIN_NAME_LEN:
        raise InvalidProfileNameError(
            "name must be non-empty after trim"
        )
    if len(trimmed) > _MAX_NAME_LEN:
        raise InvalidProfileNameError(
            f"name must be ≤ {_MAX_NAME_LEN} chars after trim"
        )
    if _CONTROL_CHAR_PATTERN.search(trimmed):
        raise InvalidProfileNameError(
            "name must not contain control characters or newlines"
        )
    return trimmed


def _generate_profile_id() -> str:
    """Generate a random unpredictable Profile ID.

    Delegates to the Store-level helper to keep ID format consistent.
    """
    from .config_store import generate_profile_id
    return generate_profile_id()


# ============================================================================
# ProviderConfigService
# ============================================================================


class ProviderConfigService:
    """Business composition layer over ProviderConfigStore.

    All write/read operations go through this Service so that:
        - Provider existence is validated against the frozen ProviderRegistry
        - Credential existence is validated via the safe CredentialService API
        - Profile status is derived consistently (provider-scoped)
        - ``masked_value`` is projected from CredentialView (never directly
          from CredentialRecord or any secret-bearing object)

    The Service does NOT touch:
        - ``SecretStore`` / ``SecretStoreRouter``
        - HTTP clients / network
        - Provider request execution (E3)
    """

    def __init__(
        self,
        *,
        store: SQLiteProviderConfigStore,
        provider_registry: ProviderRegistry,
        credential_service: CredentialService,
        session_exists: SessionExistsCallback,
    ) -> None:
        self._store = store
        self._registry = provider_registry
        self._cred = credential_service
        self._session_exists = session_exists

    # ------------------------------------------------------------------
    # Profile CRUD
    # ------------------------------------------------------------------

    async def create_profile(
        self,
        *,
        name: str,
        provider_id: str,
        credential_id: str,
        default_model: str,
        enabled: bool = True,
        is_default: bool = False,
    ) -> ProviderProfileView:
        """Create a new ProviderProfile.

        Validates:
            - ``provider_id`` registered in ProviderRegistry
            - ``credential_id`` exists (CredentialService.get)
            - ``name`` normalized
            - ``default_model`` normalized

        Lets Store reject:
            - ``is_default=True`` + ``enabled=False`` (ProviderProfileStateError)
            - Duplicate ``profile_id`` (extremely unlikely with token_urlsafe)

        Lets Store atomically clear other defaults when ``is_default=True``.
        """
        # Pre-validate inputs at service layer (fail before DB write)
        normalized_name = normalize_profile_name(name)
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise UnknownProviderError("provider_id must be non-empty")
        if not isinstance(credential_id, str) or not credential_id.strip():
            raise CredentialNotFoundForProfileError(
                "credential_id must be non-empty"
            )
        normalized_model = normalize_model_id(default_model)

        # Provider existence
        if self._registry.get(provider_id) is None:
            raise UnknownProviderError(
                f"provider_id not registered: {provider_id}"
            )

        # Credential existence (safe API—no secret read)
        try:
            await self._cred.get(credential_id)
        except CredentialNotFoundError as e:
            raise CredentialNotFoundForProfileError(
                "credential not found (credential_id omitted for safety)"
            ) from e

        # Pre-check enabled/is_default cross-constraint for clean service error
        if is_default and not enabled:
            raise ProviderProfileStateError(
                "cannot create default profile with enabled=False"
            )

        profile_id = _generate_profile_id()
        try:
            created = await self._store.create_profile(
                profile_id=profile_id,
                name=normalized_name,
                provider_id=provider_id,
                credential_id=credential_id,
                default_model=normalized_model,
                enabled=enabled,
                is_default=is_default,
            )
        except ProviderConfigStoreError:
            raise

        credential = await self._safe_get_credential(created.credential_id)
        return self._project(created, credential)

    async def update_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        credential_id: str | None = None,
        default_model: str | None = None,
        enabled: bool | None = None,
        is_default: bool | None = None,
    ) -> ProviderProfileView:
        """Update mutable fields of a Profile.

        ``provider_id`` is **immutable**—no parameter is exposed.

        Validations:
            - ``name`` (if provided) normalized
            - ``default_model`` (if provided) normalized
            - ``credential_id`` (if provided) must exist
            - ``is_default=True`` + ``enabled=False`` rejected pre-Store

        Lets Store handle:
            - enabled/is_default atomic semantics (auto-clear on disable)
            - default switching in single transaction
        """
        normalized_name = (
            normalize_profile_name(name) if name is not None else None
        )
        normalized_model = (
            normalize_model_id(default_model)
            if default_model is not None
            else None
        )

        if credential_id is not None:
            if not isinstance(credential_id, str) or not credential_id.strip():
                raise CredentialNotFoundForProfileError(
                    "credential_id must be non-empty"
                )
            try:
                await self._cred.get(credential_id)
            except CredentialNotFoundError as e:
                raise CredentialNotFoundForProfileError(
                    "credential not found (credential_id omitted for safety)"
                ) from e

        # Pre-check explicit disabled + default combination
        if enabled is False and is_default is True:
            raise ProviderProfileStateError(
                "cannot set is_default=True on disabled profile"
            )
        # Pre-check setting default on currently-disabled profile
        if is_default is True:
            current = await self._store.get_profile(profile_id)
            new_enabled = (
                enabled if enabled is not None else current.enabled
            )
            if not new_enabled:
                raise ProviderProfileStateError(
                    "cannot set is_default=True on disabled profile"
                )

        try:
            updated = await self._store.update_profile(
                profile_id,
                name=normalized_name,
                credential_id=credential_id,
                default_model=normalized_model,
                enabled=enabled,
                is_default=is_default,
            )
        except ProviderConfigStoreError:
            raise

        credential = await self._safe_get_credential(updated.credential_id)
        return self._project(updated, credential)

    async def delete_profile(self, profile_id: str) -> None:
        """Delete profile. Relies on Store's in-use protection.

        Raises:
            ProviderProfileNotFoundError: profile does not exist.
            ProviderProfileInUseError: a Session binding references it.
        """
        await self._store.delete_profile(profile_id)

    async def list_profiles(self) -> tuple[ProviderProfileView, ...]:
        """List all profiles with their current Credential projection.

        Reads all CredentialView once (batch), then projects in memory.
        """
        profiles = await self._store.list_profiles()
        # Batch-load credentials—avoid N+1 queries
        cred_views = await self._cred.list(limit=500)
        cred_by_id: dict[str, CredentialView] = {c.id: c for c in cred_views}
        return tuple(
            self._project(p, cred_by_id.get(p.credential_id))
            for p in profiles
        )

    async def get_profile(self, profile_id: str) -> ProviderProfileView:
        """Return projected Profile view (used internally + by list_models)."""
        profile = await self._store.get_profile(profile_id)
        credential = await self._safe_get_credential(profile.credential_id)
        return self._project(profile, credential)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    async def list_models(
        self,
        profile_id: str,
    ) -> tuple[ModelOption, ...]:
        """Return static model suggestions for the Profile's provider.

        Does NOT:
            - check Credential existence / status
            - check Profile.enabled
            - read Secret / call any remote endpoint

        Raises:
            ProviderProfileNotFoundError: profile does not exist.
        """
        profile = await self._store.get_profile(profile_id)
        return get_static_model_options(profile.provider_id)

    # ------------------------------------------------------------------
    # Session binding
    # ------------------------------------------------------------------

    async def initialize_new_session_binding(
        self,
        *,
        session_id: str,
    ) -> SessionModelBinding | None:
        """Initialize default binding for a freshly-created Session.

        Called by Session create handler AFTER session row commits, BEFORE HTTP
        response returns. Returns the new binding, or None if no default Profile.

        Semantics (E2-3A audit §5.5):
            1. ``store.get_default_profile()``—none → return None (Session
               created without binding—valid outcome)
            2. Default Profile exists → ``upsert_binding(session_id,
               profile.id, profile.default_model, source="default")``
            3. Return binding

        Does NOT (per E2-3A):
            - call ``session_exists`` (caller just created the session)
            - read Credential or check Profile status
            - check Profile.enabled (default Profile is always enabled per
              E2-1 partial unique index + E2-2 enabled/is_default cross-constraint)

        Failure modes (handled by caller's compensation logic):
            - Profile deleted between query and binding insert (FK fails)
              → ``ProviderProfileNotFoundError`` propagated; caller compensates
                by deleting Session.
            - Other Store errors → propagate; caller compensates.

        Notes:
            - Profile query + binding insert are NOT in a single transaction
              (different connection from Session Store, so cross-store atomic
              is impossible per SQLite standard).
            - Snapshot semantics: the Profile's ``default_model`` at this
              moment is captured. Later changes to Profile.default_model do
              not affect this Binding.
        """
        profile = await self._store.get_default_profile()
        if profile is None:
            return None
        return await self._store.upsert_binding(
            session_id=session_id,
            profile_id=profile.id,
            model_id=profile.default_model,
            source="default",
        )

    async def get_session_binding(
        self,
        session_id: str,
    ) -> SessionModelBinding | None:
        """Return binding for session_id, or None.

        Raises:
            SessionNotFoundError: session does not exist (verified via
                injected ``session_exists`` callback).
        """
        await self._require_session(session_id)
        return await self._store.get_binding(session_id)

    async def set_session_binding(
        self,
        *,
        session_id: str,
        profile_id: str,
        model_id: str,
    ) -> SessionModelBinding:
        """Bind session_id to profile_id + model_id.

        Validation order:
            1. Session must exist (``session_exists`` callback)
            2. Profile must exist (Store lookup)
            3. Profile.enabled must be True
            4. model_id must normalize

        Source is fixed to ``"explicit"`` (this method is the user-driven path).
        ``initialize_new_session_binding`` (source="default") belongs to E2-3.

        Does NOT require:
            - Credential ``storage_status = ready``——user can save selection
              even if session-only Key is currently missing; E3 will reject at
              execution time.

        Raises:
            SessionNotFoundError
            ProviderProfileNotFoundError
            ProviderProfileDisabledError
            InvalidModelIdError
        """
        await self._require_session(session_id)

        # Profile existence + enabled check
        try:
            profile = await self._store.get_profile(profile_id)
        except ProviderProfileNotFoundError:
            raise
        if not profile.enabled:
            raise ProviderProfileDisabledError(
                f"profile is disabled (profile_id={profile_id})"
            )

        normalized_model = normalize_model_id(model_id)

        return await self._store.upsert_binding(
            session_id=session_id,
            profile_id=profile_id,
            model_id=normalized_model,
            source="explicit",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _require_session(self, session_id: str) -> None:
        """Verify session existence via injected callback.

        Callbacks are expected to be cheap (e.g., SessionStore.contains). We
        do NOT cache—binding creation must verify against current state.
        """
        try:
            exists = await self._session_exists(session_id)
        except Exception as e:
            raise ProviderConfigServiceError(
                f"session_exists callback raised: {type(e).__name__}"
            ) from e
        if not exists:
            raise SessionNotFoundError(
                f"session not found (session_id={session_id})"
            )

    async def _safe_get_credential(
        self,
        credential_id: str,
    ) -> CredentialView | None:
        """Return CredentialView or None if not found.

        Uses CredentialService.get (safe API). CredentialNotFoundError is
        treated as None—Profile status will derive to ``needs_credential``.
        """
        try:
            return await self._cred.get(credential_id)
        except CredentialNotFoundError:
            return None

    def _project(
        self,
        profile: ProviderProfile,
        credential: CredentialView | None,
    ) -> ProviderProfileView:
        """Build a ProviderProfileView from a Profile + optional Credential."""
        provider_def = self._registry.get(profile.provider_id)
        display_name = (
            provider_def.display_name
            if isinstance(provider_def, ProviderDefinition)
            else profile.provider_id
        )
        masked_value = (
            credential.masked_value if credential is not None else None
        )
        status = derive_profile_status(profile, credential)
        return ProviderProfileView(
            id=profile.id,
            name=profile.name,
            provider_id=profile.provider_id,
            provider_display_name=display_name,
            credential_id=profile.credential_id,
            credential_masked_value=masked_value,
            default_model=profile.default_model,
            enabled=profile.enabled,
            is_default=profile.is_default,
            status=status,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


__all__ = [
    # Types
    "ProfileStatus",
    "SessionExistsCallback",
    # Dataclass
    "ProviderProfileView",
    # Errors
    "ProviderConfigServiceError",
    "UnknownProviderError",
    "CredentialNotFoundForProfileError",
    "SessionNotFoundError",
    "ProviderProfileDisabledError",
    "InvalidProfileNameError",
    # Functions
    "derive_profile_status",
    "normalize_profile_name",
    # Service
    "ProviderConfigService",
    # Re-exports for convenience
    "InvalidModelIdError",
]
