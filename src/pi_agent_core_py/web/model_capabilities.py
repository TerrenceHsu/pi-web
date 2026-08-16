"""User-configured model context limits used by Context Budget.

The table intentionally lives outside Provider Profile schema versioning: a
capability belongs to an exact ``(provider_id, model_id)`` pair and is not a
secret.  The authenticated gateway already gives every user an isolated
workspace database.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import aiosqlite

from .providers.model_options import get_static_model_options, normalize_model_id

CapabilitySource = Literal["user", "static", "unknown"]

MIN_CONTEXT_WINDOW = 1_024
MAX_CONTEXT_WINDOW = 10_000_000
MIN_OUTPUT_TOKENS = 1
MAX_OUTPUT_TOKENS = 1_000_000


@dataclass(frozen=True)
class ResolvedModelCapabilities:
    provider_id: str
    model_id: str
    context_window: int | None
    max_output_tokens: int | None
    source: CapabilitySource
    updated_at: int | None = None


class ModelCapabilityValidationError(ValueError):
    pass


def _normalize_provider_id(value: str) -> str:
    if not isinstance(value, str):
        raise ModelCapabilityValidationError("provider_id must be a string")
    value = value.strip()
    if not value or len(value) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ModelCapabilityValidationError("provider_id is invalid")
    return value


def _validate_limits(
    context_window: int | None,
    max_output_tokens: int | None,
) -> None:
    if context_window is not None and not (
        MIN_CONTEXT_WINDOW <= context_window <= MAX_CONTEXT_WINDOW
    ):
        raise ModelCapabilityValidationError(
            f"context_window must be between {MIN_CONTEXT_WINDOW} and {MAX_CONTEXT_WINDOW}"
        )
    if max_output_tokens is not None and not (
        MIN_OUTPUT_TOKENS <= max_output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ModelCapabilityValidationError(
            f"max_output_tokens must be between {MIN_OUTPUT_TOKENS} and {MAX_OUTPUT_TOKENS}"
        )
    if context_window is None and max_output_tokens is None:
        raise ModelCapabilityValidationError("at least one model limit is required")
    if (
        context_window is not None
        and max_output_tokens is not None
        and max_output_tokens > context_window
    ):
        raise ModelCapabilityValidationError(
            "max_output_tokens must not exceed context_window"
        )


class SQLiteModelCapabilityStore:
    """Small repository sharing the Session Store connection."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._db = connection
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS web_model_capability_overrides (
                provider_id      TEXT NOT NULL,
                model_id         TEXT NOT NULL,
                context_window   INTEGER,
                max_output_tokens INTEGER,
                updated_at       INTEGER NOT NULL,
                PRIMARY KEY(provider_id, model_id),
                CHECK(context_window IS NULL OR context_window BETWEEN 1024 AND 10000000),
                CHECK(max_output_tokens IS NULL OR max_output_tokens BETWEEN 1 AND 1000000)
            )
            """
        )
        await self._db.commit()

    async def get_override(
        self, provider_id: str, model_id: str,
    ) -> ResolvedModelCapabilities | None:
        provider = _normalize_provider_id(provider_id)
        model = normalize_model_id(model_id)
        async with self._db.execute(
            "SELECT context_window, max_output_tokens, updated_at "
            "FROM web_model_capability_overrides "
            "WHERE provider_id = ? AND model_id = ?",
            (provider, model),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return ResolvedModelCapabilities(
            provider_id=provider,
            model_id=model,
            context_window=row["context_window"],
            max_output_tokens=row["max_output_tokens"],
            source="user",
            updated_at=int(row["updated_at"]),
        )

    async def resolve(
        self, provider_id: str, model_id: str,
    ) -> ResolvedModelCapabilities:
        override = await self.get_override(provider_id, model_id)
        if override is not None:
            return override
        provider = _normalize_provider_id(provider_id)
        model = normalize_model_id(model_id)
        static = next(
            (item for item in get_static_model_options(provider) if item.id == model),
            None,
        )
        if static is not None and (
            static.capabilities.context_window is not None
            or static.capabilities.max_output_tokens is not None
        ):
            return ResolvedModelCapabilities(
                provider_id=provider,
                model_id=model,
                context_window=static.capabilities.context_window,
                max_output_tokens=static.capabilities.max_output_tokens,
                source="static",
            )
        return ResolvedModelCapabilities(
            provider_id=provider,
            model_id=model,
            context_window=None,
            max_output_tokens=None,
            source="unknown",
        )

    async def upsert(
        self,
        *,
        provider_id: str,
        model_id: str,
        context_window: int | None,
        max_output_tokens: int | None,
    ) -> ResolvedModelCapabilities:
        provider = _normalize_provider_id(provider_id)
        model = normalize_model_id(model_id)
        _validate_limits(context_window, max_output_tokens)
        updated_at = int(time.time() * 1000)
        async with self._lock:
            await self._db.execute(
                """
                INSERT INTO web_model_capability_overrides (
                    provider_id, model_id, context_window,
                    max_output_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, model_id) DO UPDATE SET
                    context_window = excluded.context_window,
                    max_output_tokens = excluded.max_output_tokens,
                    updated_at = excluded.updated_at
                """,
                (provider, model, context_window, max_output_tokens, updated_at),
            )
            await self._db.commit()
        return ResolvedModelCapabilities(
            provider_id=provider,
            model_id=model,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            source="user",
            updated_at=updated_at,
        )


def serialize_model_capabilities(value: ResolvedModelCapabilities) -> dict[str, object]:
    return {
        "provider_id": value.provider_id,
        "model_id": value.model_id,
        "context_window": value.context_window,
        "max_output_tokens": value.max_output_tokens,
        "source": value.source,
        "updated_at": value.updated_at,
    }


__all__ = [
    "CapabilitySource",
    "MAX_CONTEXT_WINDOW",
    "MAX_OUTPUT_TOKENS",
    "MIN_CONTEXT_WINDOW",
    "MIN_OUTPUT_TOKENS",
    "ModelCapabilityValidationError",
    "ResolvedModelCapabilities",
    "SQLiteModelCapabilityStore",
    "serialize_model_capabilities",
]
