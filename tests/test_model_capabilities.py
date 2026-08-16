from __future__ import annotations

import aiosqlite
import pytest

from pi_agent_core_py.web.model_capabilities import (
    ModelCapabilityValidationError,
    SQLiteModelCapabilityStore,
)


@pytest.mark.asyncio
async def test_model_capability_override_round_trip_and_resolution() -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    try:
        store = SQLiteModelCapabilityStore(db)
        await store.init()
        unknown = await store.resolve("glm", "custom-model")
        assert unknown.source == "unknown"
        assert unknown.context_window is None

        saved = await store.upsert(
            provider_id="glm",
            model_id="custom-model",
            context_window=128_000,
            max_output_tokens=8_192,
        )
        assert saved.source == "user"

        restored = await store.resolve("glm", "custom-model")
        assert restored.context_window == 128_000
        assert restored.max_output_tokens == 8_192
        assert restored.source == "user"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_model_capabilities_are_isolated_by_provider_and_model() -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    try:
        store = SQLiteModelCapabilityStore(db)
        await store.init()
        await store.upsert(
            provider_id="glm",
            model_id="same",
            context_window=32_000,
            max_output_tokens=None,
        )
        assert (await store.resolve("glm", "same")).context_window == 32_000
        assert (await store.resolve("qwen", "same")).context_window is None
        assert (await store.resolve("glm", "other")).context_window is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_model_capability_limits_are_validated() -> None:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    try:
        store = SQLiteModelCapabilityStore(db)
        await store.init()
        with pytest.raises(ModelCapabilityValidationError):
            await store.upsert(
                provider_id="glm",
                model_id="x",
                context_window=1_000,
                max_output_tokens=2_000,
            )
        with pytest.raises(ModelCapabilityValidationError):
            await store.upsert(
                provider_id="glm",
                model_id="x",
                context_window=4_096,
                max_output_tokens=8_192,
            )
    finally:
        await db.close()
