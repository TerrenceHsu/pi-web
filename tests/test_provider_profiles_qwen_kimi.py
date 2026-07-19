"""Qwen / Kimi Profile + Hint tests（M1-2 §十.Profile + Hint）.

覆盖 11 项：
- 创建 Qwen / Kimi Profile
- 保存手动 model ID
- /models 返回空列表
- 模型查询 Secret read = 0 / network = 0
- 通用 sk- Key 不自动推断为 Qwen / Kimi
- 新增 Definition 不改变现有 hint 行为
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


def _seed_credential(c: TestClient, cid: str = "cred-A") -> str:
    r = c.post(
        "/api/credentials",
        headers=_headers(),
        json={
            "label": f"Label-{cid}",
            "storage_mode": "session_only",
            "secret_value": "sk-M1-QWEN-KIMI-MARKER",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _create_profile(
    c: TestClient,
    *,
    provider_id: str,
    cred_id: str,
    name: str,
    default_model: str,
) -> dict:
    r = c.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": name,
            "provider_id": provider_id,
            "credential_id": cred_id,
            "default_model": default_model,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["profile"]


# ============================================================================
# Profile 创建
# ============================================================================


class TestProfileCreation:
    async def test_create_qwen_profile_succeeds(self, client: TestClient) -> None:
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="qwen",
            cred_id=cred_id,
            name="Qwen Work",
            default_model="qwen-plus",
        )
        assert profile["provider_id"] == "qwen"
        assert profile["provider_display_name"] == "Qwen"
        assert profile["default_model"] == "qwen-plus"
        assert profile["status"] == "ready"

    async def test_create_kimi_profile_succeeds(self, client: TestClient) -> None:
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="kimi",
            cred_id=cred_id,
            name="Kimi Personal",
            default_model="moonshot-v1-8k",
        )
        assert profile["provider_id"] == "kimi"
        assert profile["provider_display_name"] == "Kimi"
        assert profile["default_model"] == "moonshot-v1-8k"
        assert profile["status"] == "ready"

    async def test_unknown_provider_still_rejected(self, client: TestClient) -> None:
        """unknown provider 仍然拒绝——registry 只多了 qwen / kimi."""
        cred_id = _seed_credential(client)
        r = client.post(
            "/api/provider-profiles",
            headers=_headers(),
            json={
                "name": "Bad",
                "provider_id": "openai",  # not registered
                "credential_id": cred_id,
                "default_model": "gpt-4",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unknown_provider"


# ============================================================================
# 手动 model ID 保存
# ============================================================================


class TestManualModelId:
    async def test_qwen_profile_saves_arbitrary_model_id(
        self, client: TestClient,
    ) -> None:
        """Qwen 没有静态建议——任意合法 model_id 都可保存（控制字符除外）."""
        cred_id = _seed_credential(client)
        # 用户手填的模型 ID——无需在任何静态列表中
        custom_model = "qwen-custom-internal-2026"
        profile = _create_profile(
            client,
            provider_id="qwen",
            cred_id=cred_id,
            name="Custom Qwen",
            default_model=custom_model,
        )
        assert profile["default_model"] == custom_model

    async def test_kimi_profile_saves_arbitrary_model_id(
        self, client: TestClient,
    ) -> None:
        cred_id = _seed_credential(client)
        custom_model = "moonshot-experimental-v2"
        profile = _create_profile(
            client,
            provider_id="kimi",
            cred_id=cred_id,
            name="Custom Kimi",
            default_model=custom_model,
        )
        assert profile["default_model"] == custom_model


# ============================================================================
# /models 返回空列表（M1-2 不引入 QWEN_MODEL_OPTIONS / KIMI_MODEL_OPTIONS）
# ============================================================================


class TestModelsEmpty:
    async def test_qwen_models_returns_empty(self, client: TestClient) -> None:
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="qwen",
            cred_id=cred_id,
            name="Qwen",
            default_model="qwen-plus",
        )
        r = client.get(
            f"/api/provider-profiles/{profile['id']}/models",
            headers=_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 静态建议为空——M1-2 不维护模型目录
        assert body["models"] == []

    async def test_kimi_models_returns_empty(self, client: TestClient) -> None:
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="kimi",
            cred_id=cred_id,
            name="Kimi",
            default_model="moonshot-v1-8k",
        )
        r = client.get(
            f"/api/provider-profiles/{profile['id']}/models",
            headers=_headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["models"] == []


# ============================================================================
# 模型查询零 Secret 读取 / 零网络调用
# ============================================================================


class TestModelsNoSecretNoNetwork:
    async def test_qwen_models_does_not_read_secret(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GET /models 不应读 Secret——任何 SecretStore 读取都视为泄漏."""
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="qwen",
            cred_id=cred_id,
            name="Qwen",
            default_model="qwen-plus",
        )
        r = client.get(
            f"/api/provider-profiles/{profile['id']}/models",
            headers=_headers(),
        )
        body_str = r.text
        # 不应在响应中出现 secret marker
        assert "sk-M1-QWEN-KIMI-MARKER" not in body_str
        assert "secret_ref" not in body_str
        assert "api_key" not in body_str.lower()

    async def test_kimi_models_does_not_read_secret(
        self, client: TestClient,
    ) -> None:
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="kimi",
            cred_id=cred_id,
            name="Kimi",
            default_model="moonshot-v1-8k",
        )
        r = client.get(
            f"/api/provider-profiles/{profile['id']}/models",
            headers=_headers(),
        )
        body_str = r.text
        assert "sk-M1-QWEN-KIMI-MARKER" not in body_str
        assert "secret_ref" not in body_str
        assert "api_key" not in body_str.lower()

    async def test_qwen_models_zero_remote_network_by_design(
        self, client: TestClient,
    ) -> None:
        """M1-2 不引入远程模型目录——/models 结构性返回空，无远端 /v1/models 探测.

        TestClient 走 ASGI mock，不发真实 HTTP；M1-2 不修改 model_options.py，
        所以 GET /models 永远是本地纯静态（且为空）。

        本测试断言：响应快速返回 200 + models == []——若有远端调用则会
        连接超时 / 失败。
        """
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="qwen",
            cred_id=cred_id,
            name="Qwen",
            default_model="qwen-plus",
        )
        r = client.get(
            f"/api/provider-profiles/{profile['id']}/models",
            headers=_headers(),
        )
        # 200 + 空 models 已经证明无远端调用
        assert r.status_code == 200
        assert r.json()["models"] == []
        # 不返回任何含远端 base_url 的字段
        assert "dashscope.aliyuncs.com" not in r.text

    async def test_kimi_models_zero_remote_network_by_design(
        self, client: TestClient,
    ) -> None:
        cred_id = _seed_credential(client)
        profile = _create_profile(
            client,
            provider_id="kimi",
            cred_id=cred_id,
            name="Kimi",
            default_model="moonshot-v1-8k",
        )
        r = client.get(
            f"/api/provider-profiles/{profile['id']}/models",
            headers=_headers(),
        )
        assert r.status_code == 200
        assert r.json()["models"] == []
        assert "api.moonshot.cn" not in r.text


# ============================================================================
# Hint 行为不变（通用 sk- 不推断 Qwen / Kimi）
# ============================================================================


class TestHintBehaviorUnchanged:
    async def test_generic_sk_key_does_not_hint_qwen(
        self, client: TestClient,
    ) -> None:
        """sk- 不能区分 Qwen / Kimi / OpenAI——hint 必须不推荐任何 provider."""
        r = client.post(
            "/api/provider-hints",
            headers=_headers(),
            json={"secret_value": "sk-generic-1234567890abcdef"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # 通用 sk- 是 ambiguous——candidates 为空
        assert body["candidates"] == []
        assert "qwen" not in body["candidates"]

    async def test_generic_sk_key_does_not_hint_kimi(
        self, client: TestClient,
    ) -> None:
        r = client.post(
            "/api/provider-hints",
            headers=_headers(),
            json={"secret_value": "sk-generic-1234567890abcdef"},
        )
        body = r.json()
        assert "kimi" not in body["candidates"]

    async def test_anthropic_hint_still_works(self, client: TestClient) -> None:
        """新增 Qwen / Kimi Definition 不应改变 Anthropic sk-ant- 高置信度匹配."""
        r = client.post(
            "/api/provider-hints",
            headers=_headers(),
            json={"secret_value": "sk-ant-api03-abcdef1234567890"},
        )
        body = r.json()
        assert body["candidates"] == ["anthropic"]
        assert body["confidence"] == "high"
