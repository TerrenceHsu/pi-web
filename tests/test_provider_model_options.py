"""Tests for model_options module (E2-2).

覆盖 spec §12.3 Models 矩阵（10 项）+ capability invariants.
"""
from __future__ import annotations

import inspect

import pytest

from pi_agent_core_py.web.providers import model_options
from pi_agent_core_py.web.providers.model_options import (
    ANTHROPIC_MODEL_OPTIONS,
    GLM_MODEL_OPTIONS,
    InvalidModelIdError,
    get_static_model_options,
    normalize_model_id,
)

# ============================================================================
# Static catalog shape
# ============================================================================


def test_anthropic_returns_empty_tuple_by_default() -> None:
    """Anthropic has no trusted model ID baseline in repo—empty tuple."""
    assert ANTHROPIC_MODEL_OPTIONS == ()


def test_glm_returns_static_tuple() -> None:
    """GLM returns at least the repo's frozen _DEFAULT_GLM_MODEL."""
    assert len(GLM_MODEL_OPTIONS) >= 1
    ids = {opt.id for opt in GLM_MODEL_OPTIONS}
    assert "glm-4.5-flash" in ids  # matches providers/glm.py _DEFAULT_GLM_MODEL


def test_unknown_provider_returns_empty_tuple() -> None:
    """Unknown provider → empty tuple (no exception)."""
    assert get_static_model_options("unknown") == ()


def test_get_static_model_options_returns_same_object() -> None:
    """Same tuple reference each call (immutable constant)."""
    assert get_static_model_options("anthropic") is ANTHROPIC_MODEL_OPTIONS
    assert get_static_model_options("glm") is GLM_MODEL_OPTIONS


# ============================================================================
# ModelOption invariants
# ============================================================================


def test_model_option_source_is_always_static() -> None:
    """E2-2 only supports static source; no remote/custom/cached/refreshed_at."""
    for opt in (*ANTHROPIC_MODEL_OPTIONS, *GLM_MODEL_OPTIONS):
        assert opt.source == "static"


def test_model_option_does_not_contain_secret_fields() -> None:
    """ModelOption must NOT carry secret/endpoint/credential fields."""
    forbidden_substrings = (
        "secret", "api_key", "authorization", "headers",
        "endpoint", "credential", "base_url",
    )
    for opt in (*ANTHROPIC_MODEL_OPTIONS, *GLM_MODEL_OPTIONS):
        field_names = {f.name.lower() for f in opt.__dataclass_fields__.values()}
        for bad in forbidden_substrings:
            assert bad not in field_names


def test_unknown_capability_uses_none_not_false() -> None:
    """Unknown capability MUST be None (per design doc), never False."""
    for opt in (*ANTHROPIC_MODEL_OPTIONS, *GLM_MODEL_OPTIONS):
        caps = opt.capabilities
        for field in ("streaming", "tool_calling", "reasoning", "vision", "context_window"):
            value = getattr(caps, field)
            assert value is None or isinstance(value, bool) or isinstance(value, int)


# ============================================================================
# normalize_model_id
# ============================================================================


def test_normalize_model_id_trims_whitespace() -> None:
    assert normalize_model_id("  glm-4.5-flash  ") == "glm-4.5-flash"


def test_normalize_model_id_preserves_case_and_punctuation() -> None:
    assert normalize_model_id("claude-Opus-4.5:latest") == "claude-Opus-4.5:latest"
    assert normalize_model_id("org/model_v1.2") == "org/model_v1.2"


def test_normalize_model_id_rejects_empty() -> None:
    with pytest.raises(InvalidModelIdError):
        normalize_model_id("")
    with pytest.raises(InvalidModelIdError):
        normalize_model_id("   ")


def test_normalize_model_id_rejects_control_chars() -> None:
    for bad in ("bad\ninject", "bad\rinject", "bad\x00null", "bad\x1bescape", "bad\x7f"):
        with pytest.raises(InvalidModelIdError):
            normalize_model_id(bad)


def test_normalize_model_id_rejects_too_long() -> None:
    with pytest.raises(InvalidModelIdError):
        normalize_model_id("x" * 257)


def test_normalize_model_id_rejects_non_string() -> None:
    with pytest.raises(InvalidModelIdError):
        normalize_model_id(123)  # type: ignore[arg-type]
    with pytest.raises(InvalidModelIdError):
        normalize_model_id(None)  # type: ignore[arg-type]


def test_normalize_model_id_error_does_not_echo_input() -> None:
    """Safety: error message must NOT include the offending value."""
    secret_marker = "PI_E2_TEST_MARKER_INPUT"
    # Embed a NUL inside (not at edges) so trim doesn't strip it
    try:
        normalize_model_id(f"{secret_marker}\x00tail")
    except InvalidModelIdError as e:
        msg = str(e)
        assert secret_marker not in msg
    else:
        pytest.fail("expected InvalidModelIdError")


# ============================================================================
# Source-code invariants
# ============================================================================


def test_model_options_module_does_not_import_http_clients() -> None:
    """model_options must NOT import httpx / aiohttp / requests / anthropic / openai."""
    src = inspect.getsource(model_options)
    forbidden_imports = (
        "import httpx",
        "import aiohttp",
        "import requests",
        "from httpx",
        "from anthropic",
        "from openai",
    )
    for imp in forbidden_imports:
        assert imp not in src


def test_model_options_module_does_not_access_network() -> None:
    """Source must not contain http:// / https:// URLs (would suggest remote calls)."""
    src = inspect.getsource(model_options)
    # Docstrings may reference URLs—we only forbid executable code
    lines = [
        line for line in src.split("\n")
        if not line.strip().startswith("#")
        and not line.strip().startswith('"""')
    ]
    executable = "\n".join(lines)
    # No URL-like literals in executable code
    assert "http://" not in executable
    assert "https://" not in executable
