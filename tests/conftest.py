"""Global safety gates and fixtures for live integration tests."""

from __future__ import annotations

import pytest
from _live_integration import (
    GLMLiveConfig,
    integration_enabled,
    resolve_glm_live_config,
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Block real services unless both marker selection and env opt-in exist."""

    if integration_enabled():
        return

    disabled = pytest.mark.skip(
        reason="真实集成测试需要 PI_RUN_INTEGRATION=1（请使用固定重跑脚本）"
    )
    for item in items:
        if item.get_closest_marker("integration") is not None:
            item.add_marker(disabled)


@pytest.fixture
async def glm_live_config() -> GLMLiveConfig:
    """Resolve one secret-safe GLM configuration for all real GLM tests."""

    config = await resolve_glm_live_config()
    if config is None:
        pytest.fail(
            "未找到可用 GLM 配置：请配置 Web 默认 GLM Profile/Keyring，"
            "或设置 PI_AGENT_TEST_GLM_API_KEY；不会回退到可能过期的凭证"
        )
    return config
