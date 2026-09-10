"""Offline endpoint routing, SDK redirect policy and compatible schema upgrades."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest

from pi_agent_core_py.ai.providers.anthropic_compat import AnthropicCompatAdapter
from pi_agent_core_py.ai.providers.endpoints import capability_provider_id
from pi_agent_core_py.ai.providers.factory import create_provider
from pi_agent_core_py.ai.providers.openai_compat import OpenAICompatibleProvider
from pi_agent_core_py.ai.providers.registry import (
    ProviderRegistry,
    get_provider_definition,
    list_provider_definitions,
)
from pi_agent_core_py.web.providers.config_service import (
    ProviderProfileView,
    derive_profile_status,
)
from pi_agent_core_py.web.providers.config_store import (
    ProviderConfigSchemaValidationError,
    ProviderProfile,
    SQLiteProviderConfigStore,
)
from pi_agent_core_py.web.providers.runtime import RequestProviderRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id,style,adapter_type", [
    ("openai_compatible", "openai_compatible", OpenAICompatibleProvider),
    ("anthropic_compatible", "anthropic_compatible", AnthropicCompatAdapter),
    ("glm", "openai_compatible", OpenAICompatibleProvider),
    ("qwen", "anthropic_compatible", AnthropicCompatAdapter),
])
async def test_factory_uses_effective_protocol_and_disables_redirects(
    provider_id: str, style: Any, adapter_type: type,
) -> None:
    definition = get_provider_definition(provider_id)
    assert definition is not None
    adapter = create_provider(
        provider_definition=replace(definition, api_style=style, default_base_url="https://model.test/v1"),
        api_key="synthetic-not-a-real-key", model_id="test-model",
    )
    try:
        assert isinstance(adapter, adapter_type)
        assert adapter.provider_id == provider_id
        assert str(adapter._client.base_url) == "https://model.test/v1/"
        assert adapter._client._client.follow_redirects is False
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_request_snapshot_keeps_original_protocol_endpoint_and_credential() -> None:
    profile = ProviderProfileView(
        id="p", name="custom", provider_id="openai_compatible", provider_display_name="API",
        credential_id="original-credential", credential_masked_value=None, default_model="model",
        enabled=True, is_default=False, status="ready", created_at=1, updated_at=1,
        api_style="openai_compatible", base_url="https://original.test/v1",
    )
    seen: dict[str, Any] = {}

    async def get_binding(session_id: str) -> Any:
        return SimpleNamespace(profile_id="p", model_id="bound-model", source="explicit")

    async def get_profile(profile_id: str) -> ProviderProfileView:
        return profile

    async def resolve_secret(credential_id: str) -> str:
        seen["credential_id"] = credential_id
        return "synthetic"

    def factory(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace()

    runtime = RequestProviderRuntime(
        provider_config_service=SimpleNamespace(
            get_session_binding=get_binding, get_profile=get_profile,
        ),
        credential_service=SimpleNamespace(resolve_secret_for_request=resolve_secret),
        provider_registry=ProviderRegistry(list_provider_definitions()), provider_factory=factory,
    )
    selection = await runtime.resolve_selection("session")
    assert selection is not None
    profile = replace(
        profile, credential_id="new-credential", api_style="anthropic_compatible",
        base_url="https://changed.test",
    )
    adapter = await runtime.build_adapter(selection)
    assert seen["credential_id"] == "original-credential"
    assert seen["model_id"] == "bound-model"
    assert seen["provider_definition"].api_style == "openai_compatible"
    assert seen["provider_definition"].default_base_url == "https://original.test/v1"
    assert adapter.capability_provider_id == capability_provider_id(
        "openai_compatible", "openai_compatible", "https://original.test/v1",
    )
    assert "original.test" not in repr(selection)


def test_official_validation_does_not_block_custom_endpoint() -> None:
    profile = ProviderProfile(
        id="p", name="custom", provider_id="anthropic", credential_id="credential",
        default_model="model", enabled=True, is_default=False, created_at=1, updated_at=1,
    )
    credential = SimpleNamespace(
        storage_status="ready", validation_status="invalid", last_validated_provider_id="anthropic",
    )
    assert derive_profile_status(profile, credential) == "credential_invalid"
    assert derive_profile_status(
        replace(profile, base_url="https://gateway.test"), credential,
    ) == "ready"


async def _legacy_database(path: Path) -> tuple[ProviderProfile, Any]:
    store = await SQLiteProviderConfigStore.open(path)
    try:
        profile = await store.create_profile(
            profile_id="p", name="legacy", provider_id="qwen", credential_id="credential",
            default_model="model", is_default=True,
        )
        binding = await store.upsert_binding(
            session_id="session", profile_id="p", model_id="bound", source="explicit",
        )
    finally:
        await store.close()
    async with aiosqlite.connect(path) as db:
        await db.execute("ALTER TABLE web_provider_profiles DROP COLUMN api_style")
        await db.execute("ALTER TABLE web_provider_profiles DROP COLUMN base_url")
        await db.execute("UPDATE web_provider_config_schema_meta SET value='1' WHERE key='version'")
        await db.commit()
    return profile, binding


@pytest.mark.asyncio
async def test_v1_upgrade_preserves_profile_binding_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    profile, binding = await _legacy_database(path)
    store = await SQLiteProviderConfigStore.open(path)
    try:
        assert await store.get_schema_version() == 2
        assert await store.get_profile("p") == profile
        assert await store.get_binding("session") == binding
        assert await store.get_default_profile() == profile
    finally:
        await store.close()
    reopened = await SQLiteProviderConfigStore.open(path)
    try:
        assert await reopened.get_profile("p") == profile
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_v1_upgrade_rejects_drift_without_partial_changes(tmp_path: Path) -> None:
    path = tmp_path / "drift.db"
    await _legacy_database(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("ALTER TABLE web_provider_profiles ADD COLUMN secret TEXT")
        await db.commit()
    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(path)
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT value FROM web_provider_config_schema_meta") as cursor:
            assert await cursor.fetchone() == ("1",)
        async with db.execute("PRAGMA table_info(web_provider_profiles)") as cursor:
            assert "base_url" not in {row[1] for row in await cursor.fetchall()}
