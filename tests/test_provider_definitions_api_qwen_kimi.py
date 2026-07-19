"""Provider Definitions API regression tests for Qwen / Kimi（M1-2 §十.API）.

覆盖 9 项：
- definitions API 返回 Qwen / Kimi
- 不返回 base_url / validation endpoint / Adapter 类信息
- safe fields 与旧 Provider（GLM / Anthropic）一致
- 旧 Provider API 响应不变

若 API serializer 当前已经满足，不改生产代码，只加回归测试.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.web.app import create_app  # noqa: E402

pytestmark = pytest.mark.asyncio


# ============================================================================
# Fixtures
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "api.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


def _get_definitions(c: TestClient) -> list[dict]:
    r = c.get("/api/provider-definitions", headers=_headers())
    assert r.status_code == 200, r.text
    return r.json()


def _find(defs: list[dict], provider_id: str) -> dict:
    for d in defs:
        if d["id"] == provider_id:
            return d
    raise AssertionError(f"{provider_id} not in definitions: {[d['id'] for d in defs]}")


# ============================================================================
# 返回 Qwen / Kimi
# ============================================================================


class TestDefinitionsReturned:
    async def test_definitions_includes_qwen(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        ids = {d["id"] for d in defs}
        assert "qwen" in ids

    async def test_definitions_includes_kimi(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        ids = {d["id"] for d in defs}
        assert "kimi" in ids

    async def test_definitions_includes_all_four(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        ids = {d["id"] for d in defs}
        assert ids >= {"glm", "anthropic", "qwen", "kimi"}


# ============================================================================
# Safe fields 形状
# ============================================================================


# 当前 serializer 暴露的字段全集——任何字段变化需显式更新本常量
_SAFE_FIELDS = frozenset({
    "id",
    "display_name",
    "api_style",
    "validation_supported",
    "supports_model_listing",
})

# 严禁返回的字段——含 base_url / endpoint / Authorization / Adapter 类等
_FORBIDDEN_FIELDS = frozenset({
    "default_base_url",
    "credential_validation_endpoint",
    "credential_validation_strategy",
    "key_prefix_hints",
    "authorization",
    "adapter_class",
    "sdk_type",
    "extra_headers",
})


class TestSafeFieldsShape:
    async def test_qwen_safe_fields(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        qwen = _find(defs, "qwen")
        # 字段集精确（不多不少）
        assert set(qwen.keys()) == _SAFE_FIELDS

    async def test_kimi_safe_fields(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        kimi = _find(defs, "kimi")
        assert set(kimi.keys()) == _SAFE_FIELDS

    async def test_qwen_safe_field_values(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        qwen = _find(defs, "qwen")
        assert qwen == {
            "id": "qwen",
            "display_name": "Qwen",
            "api_style": "openai_compatible",
            "validation_supported": False,
            "supports_model_listing": False,
        }

    async def test_kimi_safe_field_values(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        kimi = _find(defs, "kimi")
        assert kimi == {
            "id": "kimi",
            "display_name": "Kimi",
            "api_style": "openai_compatible",
            "validation_supported": False,
            "supports_model_listing": False,
        }


# ============================================================================
# 不返回敏感字段
# ============================================================================


class TestNoSensitiveFields:
    async def test_qwen_does_not_return_base_url(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        qwen = _find(defs, "qwen")
        assert "default_base_url" not in qwen
        # base_url 字面也不应在任何字段值中
        for v in qwen.values():
            assert "dashscope.aliyuncs.com" not in str(v)

    async def test_kimi_does_not_return_base_url(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        kimi = _find(defs, "kimi")
        assert "default_base_url" not in kimi
        for v in kimi.values():
            assert "api.moonshot.cn" not in str(v)

    async def test_qwen_does_not_return_validation_endpoint(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        qwen = _find(defs, "qwen")
        assert "credential_validation_endpoint" not in qwen

    async def test_kimi_does_not_return_validation_endpoint(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        kimi = _find(defs, "kimi")
        assert "credential_validation_endpoint" not in kimi

    async def test_qwen_does_not_return_adapter_class_info(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        qwen = _find(defs, "qwen")
        # 不应含内部 Adapter / SDK 类型信息
        for forbidden in _FORBIDDEN_FIELDS:
            assert forbidden not in qwen, f"forbidden field {forbidden!r} in qwen"

    async def test_kimi_does_not_return_adapter_class_info(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        kimi = _find(defs, "kimi")
        for forbidden in _FORBIDDEN_FIELDS:
            assert forbidden not in kimi, f"forbidden field {forbidden!r} in kimi"


# ============================================================================
# Safe fields 与旧 Provider 一致
# ============================================================================


class TestLegacyProvidersSafeFieldsConsistent:
    async def test_glm_safe_fields_unchanged(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        glm = _find(defs, "glm")
        assert set(glm.keys()) == _SAFE_FIELDS
        assert glm == {
            "id": "glm",
            "display_name": "Zhipu GLM (Anthropic-compatible)",
            "api_style": "anthropic_compatible",
            "validation_supported": False,
            "supports_model_listing": False,
        }

    async def test_anthropic_safe_fields_unchanged(self, client: TestClient) -> None:
        defs = _get_definitions(client)
        anthropic = _find(defs, "anthropic")
        assert set(anthropic.keys()) == _SAFE_FIELDS
        assert anthropic == {
            "id": "anthropic",
            "display_name": "Anthropic",
            "api_style": "anthropic_compatible",
            "validation_supported": True,
            "supports_model_listing": True,
        }

    async def test_legacy_providers_do_not_leak_base_url(self, client: TestClient) -> None:
        """旧 Provider 也不返回 base_url（serializer 已统一）."""
        defs = _get_definitions(client)
        for pid in ("glm", "anthropic"):
            d = _find(defs, pid)
            assert "default_base_url" not in d
            assert "credential_validation_endpoint" not in d


# ============================================================================
# API 响应顺序——保留 Registry 顺序（GLM / Anthropic / Qwen / Kimi）
# ============================================================================


class TestApiResponseOrder:
    async def test_definitions_preserve_registry_order(self, client: TestClient) -> None:
        """API 投影顺序应与 list_provider_definitions() 一致——前端不在后端排序."""
        defs = _get_definitions(client)
        ids = [d["id"] for d in defs]
        # 前 4 项精确顺序——追加新 Provider 时需同步更新
        assert ids[:4] == ["glm", "anthropic", "qwen", "kimi"]
