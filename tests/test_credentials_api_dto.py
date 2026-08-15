"""Credential API DTO tests（P1-E1-4B1）.

覆盖 spec §10 DTO：
- secret_value 用 SecretStr——repr 不泄漏
- byte-length validator（按 UTF-8 bytes，不是 Python 字符）
- label length / trim 校验
- provider_id / credential_id format 校验
- extra="forbid" 拒绝额外字段
- normalize() 跨字段校验（env vs keyring）
- OpenAPI schema——secret_value 标记 writeOnly + format=password + 无 example
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from pi_agent_core_py.web.credentials.dto import (
    MAX_LABEL_CHARS,
    MAX_SECRET_BYTES,
    CredentialCreateRequest,
    CredentialLabelUpdateRequest,
    CredentialRotateRequest,
    CredentialValidateRequest,
    ProviderHintRequest,
)

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# SecretStr repr safety
# ============================================================================


class TestSecretStrReprSafety:
    def test_secretstr_repr_does_not_leak_marker(self) -> None:
        """Pydantic SecretStr 默认 repr = SecretStr('**********')."""
        s = SecretStr(SECRET_MARKER)
        assert SECRET_MARKER not in repr(s)
        assert SECRET_MARKER not in str(s)

    def test_dto_repr_does_not_leak_marker(self) -> None:
        dto = ProviderHintRequest(secret_value=SecretStr(SECRET_MARKER))
        text = repr(dto)
        assert SECRET_MARKER not in text

    def test_dto_normalize_does_not_leak_in_error(self) -> None:
        """Cross-field validation error message 不含 marker."""
        # Create env+secret_value——normalize() should raise
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="env",
            secret_value=SecretStr(SECRET_MARKER),
            env_var_name="MY_KEY",
        )
        with pytest.raises(ValueError) as ei:
            dto.normalize()
        msg = str(ei.value)
        assert SECRET_MARKER not in msg


# ============================================================================
# Byte length validation
# ============================================================================


class TestByteLength:
    def test_secret_at_max_bytes_accepted(self) -> None:
        """8192 bytes UTF-8——刚好上限，允许."""
        # ASCII 1 byte per char——8192 chars
        raw = "a" * MAX_SECRET_BYTES
        dto = ProviderHintRequest(secret_value=SecretStr(raw))
        assert dto.secret_value.get_secret_value() == raw

    def test_secret_over_max_bytes_rejected(self) -> None:
        """8193 bytes——超 1 byte，拒绝."""
        raw = "a" * (MAX_SECRET_BYTES + 1)
        with pytest.raises(ValidationError):
            ProviderHintRequest(secret_value=SecretStr(raw))

    def test_secret_multibyte_counted_by_bytes_not_chars(self) -> None:
        """中文字符 3 bytes/char——按字节计数，不是 Python 字符数."""
        # 2731 中文字符 = 8193 bytes > 8192
        raw = "中" * 2731
        assert len(raw.encode("utf-8")) > MAX_SECRET_BYTES
        with pytest.raises(ValidationError):
            ProviderHintRequest(secret_value=SecretStr(raw))

    def test_secret_exactly_2730_chinese_chars_accepted(self) -> None:
        """2730 × 3 = 8190 bytes ≤ 8192——允许."""
        raw = "中" * 2730
        assert len(raw.encode("utf-8")) <= MAX_SECRET_BYTES
        dto = ProviderHintRequest(secret_value=SecretStr(raw))
        assert dto.secret_value.get_secret_value() == raw

    def test_secret_error_message_does_not_leak_value(self) -> None:
        raw = SECRET_MARKER + "a" * MAX_SECRET_BYTES
        with pytest.raises(ValidationError) as ei:
            ProviderHintRequest(secret_value=SecretStr(raw))
        # ValidationError repr 不应含 marker
        assert SECRET_MARKER not in str(ei.value)
        assert SECRET_MARKER not in repr(ei.value)


# ============================================================================
# Label validation
# ============================================================================


class TestLabelValidation:
    def test_label_trim_then_nonempty(self) -> None:
        """trim 后必须非空."""
        with pytest.raises(ValidationError):
            CredentialLabelUpdateRequest(label="   ")

    def test_label_max_chars_accepted(self) -> None:
        label = "a" * MAX_LABEL_CHARS
        dto = CredentialLabelUpdateRequest(label=label)
        # _validate_label returns trimmed——"aaa...a" trim unchanged
        assert dto.label == label

    def test_label_over_max_rejected(self) -> None:
        label = "a" * (MAX_LABEL_CHARS + 1)
        with pytest.raises(ValidationError):
            CredentialLabelUpdateRequest(label=label)

    def test_label_trim_applied(self) -> None:
        """前后空白被 strip."""
        dto = CredentialLabelUpdateRequest(label="  hello  ")
        assert dto.label == "hello"


# ============================================================================
# provider_id / credential_id format
# ============================================================================


class TestIdFormat:
    def test_provider_id_lowercase_alpha_accepted(self) -> None:
        dto = CredentialValidateRequest(provider_id="anthropic")
        assert dto.provider_id == "anthropic"

    def test_provider_id_uppercase_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CredentialValidateRequest(provider_id="Anthropic")

    def test_provider_id_with_dash_underscore_accepted(self) -> None:
        dto = CredentialValidateRequest(provider_id="my-provider_1")
        assert dto.provider_id == "my-provider_1"

    def test_provider_id_too_long_rejected(self) -> None:
        raw = "a" * 65
        with pytest.raises(ValidationError):
            CredentialValidateRequest(provider_id=raw)

    def test_provider_id_with_dot_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CredentialValidateRequest(provider_id="evil.com")

    def test_provider_id_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CredentialValidateRequest(provider_id="")


# ============================================================================
# extra="forbid"
# ============================================================================


class TestExtraForbidden:
    def test_create_with_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CredentialCreateRequest.model_validate(
                {
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234",
                    "fingerprint": "abc",  # extra field
                }
            )

    def test_create_with_id_field_rejected(self) -> None:
        """Caller must not be able to inject credential_id."""
        with pytest.raises(ValidationError):
            CredentialCreateRequest.model_validate(
                {
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": "sk-test-1234",
                    "id": "cred-evil",
                }
            )

    def test_validate_with_base_url_rejected(self) -> None:
        """Custom Base URL is forbidden——must use built-in endpoint."""
        with pytest.raises(ValidationError):
            CredentialValidateRequest.model_validate(
                {"provider_id": "anthropic", "base_url": "https://evil.com"}
            )

    def test_rotate_with_storage_mode_rejected(self) -> None:
        """Cannot change storage_mode via rotate."""
        with pytest.raises(ValidationError):
            CredentialRotateRequest.model_validate(
                {
                    "secret_value": "sk-test-1234",
                    "storage_mode": "env",  # forbidden
                }
            )


# ============================================================================
# Cross-field validation (normalize)
# ============================================================================


class TestCrossFieldValidation:
    def test_env_with_secret_value_rejected(self) -> None:
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="env",
            secret_value=SecretStr("sk-test"),
            env_var_name="MY_KEY",
        )
        with pytest.raises(ValueError, match="forbids secret_value"):
            dto.normalize()

    def test_env_without_env_var_name_rejected(self) -> None:
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="env",
        )
        with pytest.raises(ValueError, match="requires env_var_name"):
            dto.normalize()

    def test_keyring_with_env_var_name_rejected(self) -> None:
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="keyring",
            secret_value=SecretStr("sk-test"),
            env_var_name="MY_KEY",
        )
        with pytest.raises(ValueError, match="forbids env_var_name"):
            dto.normalize()

    def test_keyring_without_secret_value_rejected(self) -> None:
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="keyring",
        )
        with pytest.raises(ValueError, match="requires secret_value"):
            dto.normalize()

    def test_rotate_requires_one_field(self) -> None:
        dto = CredentialRotateRequest()
        with pytest.raises(ValueError, match="requires"):
            dto.normalize()

    def test_rotate_forbids_both_fields(self) -> None:
        dto = CredentialRotateRequest(
            secret_value=SecretStr("sk-test"),
            env_var_name="MY_KEY",
        )
        with pytest.raises(ValueError, match="only one"):
            dto.normalize()

    def test_session_only_with_secret_accepted(self) -> None:
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="session_only",
            secret_value=SecretStr("sk-test-1234567890"),
        )
        dto.normalize()  # does not raise

    def test_env_with_only_env_var_name_accepted(self) -> None:
        dto = CredentialCreateRequest(
            label="X",
            storage_mode="env",
            env_var_name="MY_KEY",
        )
        dto.normalize()  # does not raise


# ============================================================================
# OpenAPI schema——secret_value writeOnly + format=password + no example
# ============================================================================


class TestOpenApiSchema:
    def test_provider_hint_secret_field_is_password_format(self) -> None:
        schema = ProviderHintRequest.model_json_schema()
        prop = schema["properties"]["secret_value"]
        assert prop.get("format") == "password"
        assert prop.get("writeOnly") is True

    def test_create_secret_field_is_password_format(self) -> None:
        schema = CredentialCreateRequest.model_json_schema()
        prop = schema["properties"]["secret_value"]
        assert prop.get("format") == "password"
        assert prop.get("writeOnly") is True

    def test_rotate_secret_field_is_password_format(self) -> None:
        schema = CredentialRotateRequest.model_json_schema()
        prop = schema["properties"]["secret_value"]
        assert prop.get("format") == "password"
        assert prop.get("writeOnly") is True

    def test_no_default_secret_value(self) -> None:
        """secret_value 不应有 default（除了 Optional 字段）."""
        schema = ProviderHintRequest.model_json_schema()
        prop = schema["properties"]["secret_value"]
        assert "default" not in prop

    def test_openapi_schema_does_not_contain_marker(self) -> None:
        """Schema 不应含任何 marker 字面量."""
        for cls in (
            ProviderHintRequest,
            CredentialCreateRequest,
            CredentialRotateRequest,
            CredentialValidateRequest,
            CredentialLabelUpdateRequest,
        ):
            schema_json = cls.model_json_schema()
            text = str(schema_json)
            assert SECRET_MARKER not in text
