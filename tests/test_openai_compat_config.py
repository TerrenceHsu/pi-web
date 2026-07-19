"""OpenAICompatConfig 校验测试（M1-1 §十五.Config）.

覆盖：
- 合法 config
- SecretStr repr masking
- 空 api_key
- 空 model
- 非法 base URL（scheme / userinfo / 缺 host）
- timeout_s 非正
- max_tokens 非正
- temperature NaN / inf
- extra field 拒绝
"""
from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from pi_agent_core_py.providers.openai_compat import OpenAICompatConfig


def _make(**overrides: Any) -> OpenAICompatConfig:
    base: dict[str, Any] = {
        "api_key": "sk-test-key",
        "base_url": "https://example.test",
        "model": "qwen-plus",
    }
    base.update(overrides)
    return OpenAICompatConfig(**base)


# ============================================================================
# 合法 config
# ============================================================================


def test_valid_config_minimal() -> None:
    cfg = _make()
    assert cfg.api_key.get_secret_value() == "sk-test-key"
    assert cfg.base_url == "https://example.test"
    assert cfg.model == "qwen-plus"
    assert cfg.timeout_s == 60.0
    assert cfg.max_tokens == 4096
    assert cfg.temperature is None


def test_valid_config_http_base_url() -> None:
    cfg = _make(base_url="http://localhost:8080")
    assert cfg.base_url == "http://localhost:8080"


def test_valid_config_max_tokens_none() -> None:
    cfg = _make(max_tokens=None)
    assert cfg.max_tokens is None


def test_valid_config_temperature_value() -> None:
    cfg = _make(temperature=0.7)
    assert cfg.temperature == 0.7


def test_valid_config_temperature_zero() -> None:
    cfg = _make(temperature=0.0)
    assert cfg.temperature == 0.0


# ============================================================================
# SecretStr repr masking
# ============================================================================


def test_secret_str_repr_does_not_leak_key() -> None:
    cfg = _make(api_key="sk-M1-OPENAI-COMPAT-SECRET-MARKER")
    r = repr(cfg)
    assert "sk-M1-OPENAI-COMPAT-SECRET-MARKER" not in r
    assert "api_key" not in r or "SecretStr" in r or "=" not in r.split("api_key")[-1].split(",")[0]


def test_secret_str_str_does_not_leak_key() -> None:
    cfg = _make(api_key="sk-M1-OPENAI-COMPAT-SECRET-MARKER")
    assert "sk-M1-OPENAI-COMPAT-SECRET-MARKER" not in str(cfg)


def test_secret_str_is_secret_str_type() -> None:
    cfg = _make()
    assert isinstance(cfg.api_key, SecretStr)


# ============================================================================
# api_key 校验
# ============================================================================


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(api_key="")


def test_whitespace_only_api_key_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(api_key="   ")


# ============================================================================
# model 校验
# ============================================================================


def test_empty_model_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(model="")


def test_whitespace_only_model_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(model="   ")


def test_model_with_control_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(model="bad\nmodel")
    with pytest.raises(ValidationError):
        _make(model="bad\x00model")
    with pytest.raises(ValidationError):
        _make(model="bad\rmodel")
    with pytest.raises(ValidationError):
        _make(model="bad\x7fmodel")


# ============================================================================
# base_url 校验
# ============================================================================


def test_base_url_must_have_scheme() -> None:
    with pytest.raises(ValidationError):
        _make(base_url="example.test")


def test_base_url_rejects_ftp_scheme() -> None:
    with pytest.raises(ValidationError):
        _make(base_url="ftp://example.test")


def test_base_url_rejects_file_scheme() -> None:
    with pytest.raises(ValidationError):
        _make(base_url="file:///etc/passwd")


def test_base_url_rejects_userinfo() -> None:
    with pytest.raises(ValidationError):
        _make(base_url="https://user:pass@example.test")


def test_base_url_rejects_lone_userinfo_at() -> None:
    with pytest.raises(ValidationError):
        _make(base_url="https://leaked@example.test")


def test_base_url_rejects_missing_host() -> None:
    with pytest.raises(ValidationError):
        _make(base_url="https://")


# ============================================================================
# timeout_s 校验
# ============================================================================


def test_timeout_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(timeout_s=0)


def test_timeout_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(timeout_s=-1.0)


# ============================================================================
# max_tokens 校验
# ============================================================================


def test_max_tokens_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(max_tokens=0)


def test_max_tokens_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(max_tokens=-100)


def test_max_tokens_positive_accepted() -> None:
    cfg = _make(max_tokens=8192)
    assert cfg.max_tokens == 8192


# ============================================================================
# temperature 校验
# ============================================================================


def test_temperature_nan_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(temperature=math.nan)


def test_temperature_inf_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(temperature=math.inf)
    with pytest.raises(ValidationError):
        _make(temperature=-math.inf)


# ============================================================================
# extra field 拒绝
# ============================================================================


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(extra_headers={"X-Test": "1"})


def test_provider_id_field_rejected() -> None:
    """Config 不保存 provider_id（决策 3）——extra field forbid."""
    with pytest.raises(ValidationError):
        _make(provider_id="qwen")


def test_authorization_field_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(Authorization="Bearer xxx")


def test_organization_field_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(organization="org-xyz")
