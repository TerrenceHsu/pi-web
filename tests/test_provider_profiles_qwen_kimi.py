"""Minimal public API smoke for the two OpenAI-compatible providers."""

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

_HEADERS = {"X-PI-Agent-UI": "1"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    model = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    app = create_app(
        AgentHarness(Agent(system_prompt="base", client=model)),
        db_path=str(tmp_path / "api.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


def _create_profile(client: TestClient, provider_id: str, model_id: str) -> dict[str, object]:
    credential_response = client.post(
        "/api/credentials",
        headers=_HEADERS,
        json={
            "label": f"{provider_id}-credential",
            "storage_mode": "session_only",
            "secret_value": "sk-development-smoke",
        },
    )
    assert credential_response.status_code == 201
    credential_id = credential_response.json()["credential"]["credential_id"]
    response = client.post(
        "/api/provider-profiles",
        headers=_HEADERS,
        json={
            "name": f"{provider_id}-profile",
            "provider_id": provider_id,
            "credential_id": credential_id,
            "default_model": model_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["profile"]


def test_create_qwen_profile(client: TestClient) -> None:
    profile = _create_profile(client, "qwen", "qwen-plus")
    assert profile["provider_id"] == "qwen"
    assert profile["default_model"] == "qwen-plus"


def test_create_kimi_profile(client: TestClient) -> None:
    profile = _create_profile(client, "kimi", "moonshot-v1-8k")
    assert profile["provider_id"] == "kimi"
    assert profile["default_model"] == "moonshot-v1-8k"
