"""Credential API request DTOs（P1-E1-4B1）.

严格 Pydantic 模型——所有 DTO `extra="forbid"`，secret_value 用 `SecretStr`.

**关键不变量**：

1. `secret_value` 用 `pydantic.SecretStr`——`repr(dto)` 不输出明文（默认 mask）
2. byte-length validator 只测 UTF-8 字节数，**不**把值放入异常 message / ctx / 日志
3. OpenAPI 标记 `secret_value` 为 `format=password` + `writeOnly=True`
4. 不提供 example / default——防御 OpenAPI 文档泄漏
5. `extra="forbid"`——任何额外字段直接 422

P1-E1-5B / LOW-1：新增 SafeValidation* 响应模型，覆盖 OpenAPI 默认 422 schema
（默认 HTTPValidationError 含 input / ctx / url 字段——即使运行时不返回，OpenAPI
文档仍声明，会让客户端按错误 shape 生成代码）。
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

__all__ = [
    "MAX_SECRET_BYTES",
    "MAX_LABEL_CHARS",
    "PROVIDER_ID_PATTERN",
    "CREDENTIAL_ID_PATTERN",
    "ProviderHintRequest",
    "CredentialCreateRequest",
    "CredentialLabelUpdateRequest",
    "CredentialRotateRequest",
    "CredentialValidateRequest",
    # 422 response models（P1-E1-5B / LOW-1）
    "SafeValidationField",
    "SafeValidationErrorDetail",
    "SafeValidationErrorResponse",
]


# ============================================================================
# Length / format limits
# ============================================================================


MAX_SECRET_BYTES = 8192
MAX_LABEL_CHARS = 128
# Provider ID——内置 ID（anthropic/glm）+ future user profiles.
# 限定 ASCII lower-case / digits / "-" / "_"——避免任意字符串进日志或 SQL
PROVIDER_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# Credential ID——由 secrets.token_urlsafe() 生成（mixed-case）+ "cred-" prefix.
# 允许 ASCII letters（both cases）/ digits / "-" / "_"——但限定长度避免日志膨胀
CREDENTIAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"

_PROVIDER_ID_RE = re.compile(PROVIDER_ID_PATTERN)
_CREDENTIAL_ID_RE = re.compile(CREDENTIAL_ID_PATTERN)


# ============================================================================
# Shared validators
# ============================================================================


def _validate_secret_bytes(v: SecretStr | None) -> SecretStr | None:
    """Validate secret_value UTF-8 byte length. None passes through.

    Never raises with the secret value in the message——only byte count.
    """
    if v is None:
        return None
    raw = v.get_secret_value()
    if not raw:
        raise ValueError("secret_value must be non-empty")
    byte_len = len(raw.encode("utf-8"))
    if byte_len > MAX_SECRET_BYTES:
        raise ValueError(
            f"secret_value exceeds maximum allowed length "
            f"({MAX_SECRET_BYTES} UTF-8 bytes)"
        )
    return v


def _validate_label(v: str) -> str:
    """Validate label: trim + 1..MAX_LABEL_CHARS Unicode chars."""
    if not isinstance(v, str):
        raise ValueError("label must be a string")
    trimmed = v.strip()
    if not trimmed:
        raise ValueError("label must be non-empty after trim")
    if len(trimmed) > MAX_LABEL_CHARS:
        raise ValueError(
            f"label exceeds maximum allowed length ({MAX_LABEL_CHARS} chars)"
        )
    return trimmed


def _validate_provider_id(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("provider_id must be a string")
    if not _PROVIDER_ID_RE.match(v):
        raise ValueError(
            "provider_id must match ^[a-z0-9][a-z0-9_-]{0,63}$"
        )
    return v


def _validate_credential_id(v: str) -> str:
    if not isinstance(v, str):
        raise ValueError("credential_id must be a string")
    if not _CREDENTIAL_ID_RE.match(v):
        raise ValueError(
            "credential_id must match ^[a-z0-9][a-z0-9_-]{0,127}$"
        )
    return v


def _validate_env_var_name(v: str | None) -> str | None:
    if v is None:
        return None
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", v):
        raise ValueError(
            "env_var_name must match ^[A-Za-z_][A-Za-z0-9_]*$"
        )
    return v


# ============================================================================
# Request DTOs
# ============================================================================


class ProviderHintRequest(BaseModel):
    """POST /api/provider-hints request body.

    `secret_value` 用于本地 hint 探测——绝不持久化 / 网络 / 写日志.
    """

    model_config = ConfigDict(extra="forbid")

    secret_value: SecretStr = Field(
        ...,
        json_schema_extra={
            "format": "password",
            "writeOnly": True,
        },
    )

    @field_validator("secret_value")
    @classmethod
    def _check_secret(cls, v: SecretStr) -> SecretStr:
        return _validate_secret_bytes(v)


class CredentialCreateRequest(BaseModel):
    """POST /api/credentials request body.

    Mutually exclusive:
        - storage_mode in ("keyring", "session_only") → secret_value required
        - storage_mode == "env" → env_var_name required, secret_value forbidden
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=MAX_LABEL_CHARS)
    storage_mode: Literal["keyring", "session_only", "env"]
    secret_value: SecretStr | None = Field(
        default=None,
        json_schema_extra={
            "format": "password",
            "writeOnly": True,
        },
    )
    env_var_name: str | None = None

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str) -> str:
        return _validate_label(v)

    @field_validator("secret_value")
    @classmethod
    def _check_secret(cls, v: SecretStr | None) -> SecretStr | None:
        return _validate_secret_bytes(v)

    @field_validator("env_var_name")
    @classmethod
    def _check_env(cls, v: str | None) -> str | None:
        return _validate_env_var_name(v)

    def normalize(self) -> None:
        """Post-init cross-field validation. Called explicitly by router."""
        if self.storage_mode == "env":
            if self.secret_value is not None:
                raise ValueError(
                    "storage_mode='env' forbids secret_value"
                )
            if self.env_var_name is None or not self.env_var_name.strip():
                raise ValueError(
                    "storage_mode='env' requires env_var_name"
                )
        else:
            if self.secret_value is None:
                raise ValueError(
                    f"storage_mode={self.storage_mode!r} requires secret_value"
                )
            if self.env_var_name is not None:
                raise ValueError(
                    f"storage_mode={self.storage_mode!r} forbids env_var_name"
                )


class CredentialLabelUpdateRequest(BaseModel):
    """PATCH /api/credentials/{credential_id} request body. Only label."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=MAX_LABEL_CHARS)

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str) -> str:
        return _validate_label(v)


class CredentialRotateRequest(BaseModel):
    """PUT /api/credentials/{credential_id}/secret request body.

    Existing credential's storage_mode determines which field is required:
        - keyring / session_only → secret_value required
        - env → env_var_name required

    Cross-field check happens at Service layer——DTO only enforces
    not-both / not-neither at API level (at least one must be present).
    """

    model_config = ConfigDict(extra="forbid")

    secret_value: SecretStr | None = Field(
        default=None,
        json_schema_extra={
            "format": "password",
            "writeOnly": True,
        },
    )
    env_var_name: str | None = None

    @field_validator("secret_value")
    @classmethod
    def _check_secret(cls, v: SecretStr | None) -> SecretStr | None:
        return _validate_secret_bytes(v)

    @field_validator("env_var_name")
    @classmethod
    def _check_env(cls, v: str | None) -> str | None:
        return _validate_env_var_name(v)

    def normalize(self) -> None:
        """At least one of secret_value / env_var_name must be present."""
        if self.secret_value is None and self.env_var_name is None:
            raise ValueError(
                "rotate requires secret_value or env_var_name"
            )
        if self.secret_value is not None and self.env_var_name is not None:
            raise ValueError(
                "rotate accepts only one of secret_value / env_var_name"
            )


class CredentialValidateRequest(BaseModel):
    """POST /api/credentials/{credential_id}/validate request body."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., min_length=1, max_length=64)

    @field_validator("provider_id")
    @classmethod
    def _check_provider_id(cls, v: str) -> str:
        return _validate_provider_id(v)


# ============================================================================
# Safe 422 response models（P1-E1-5B / LOW-1）
# ============================================================================


class SafeValidationField(BaseModel):
    """Single field projection in safe 422 response——path + code only.

    Never contains input / ctx / msg / url（Pydantic default carries these
    in `ValidationError`；we project only safe fields）.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    code: str


class SafeValidationErrorDetail(BaseModel):
    """Error body shape returned by `safe_validation_response`."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["request_validation_failed"]
    message: str
    fields: list[SafeValidationField]


class SafeValidationErrorResponse(BaseModel):
    """Wrapper——top-level response model for Credential 422 responses.

    Declared via `APIRouter(responses={422: {"model": SafeValidationErrorResponse}})`
    to override FastAPI's default `HTTPValidationError` schema.
    """

    model_config = ConfigDict(extra="forbid")

    error: SafeValidationErrorDetail
