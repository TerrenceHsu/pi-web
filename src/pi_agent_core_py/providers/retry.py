"""Retry policy for failures before a provider stream has emitted output."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from .errors import ProviderRateLimitError, ProviderStreamError


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Bounded exponential backoff.

    ``max_retries`` counts retries after the initial attempt.  Retrying is
    deliberately limited to request-establishment failures; once any stream
    event was exposed to the caller, replay could duplicate visible output.
    """

    max_retries: int = 2
    initial_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.initial_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("retry delays must be non-negative")

    def delay_for_retry(self, retry_index: int, exc: BaseException) -> float:
        """Return clamped delay for one-based ``retry_index``."""

        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            return min(max(0.0, retry_after), self.max_delay_s)
        exponential = self.initial_delay_s * (2.0 ** max(0, retry_index - 1))
        return min(exponential, self.max_delay_s)


def is_retryable_provider_error(exc: BaseException) -> bool:
    if isinstance(exc, (ProviderRateLimitError, ProviderStreamError)):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (
        status in {408, 409, 429} or status >= 500
    )


def _retry_after_seconds(exc: BaseException) -> float | None:
    direct = getattr(exc, "retry_after", None)
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    response = getattr(exc, "response", None)
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


async def wait_for_retry(delay_s: float, signal: Any | None) -> bool:
    """Wait for backoff; return False when aborted before the delay elapsed."""

    if signal is not None:
        try:
            if signal.is_set():
                return False
        except Exception:
            pass
    if delay_s <= 0:
        return True

    wait = getattr(signal, "wait", None) if signal is not None else None
    if wait is not None and inspect.iscoroutinefunction(wait):
        try:
            maybe_awaitable = wait()
            try:
                await asyncio.wait_for(maybe_awaitable, timeout=delay_s)
                return False
            except TimeoutError:
                return True
        except Exception:
            pass
    # threading.Event/custom synchronous signals must never block the event
    # loop. Poll at a short interval so abort remains responsive.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay_s
    while loop.time() < deadline:
        await asyncio.sleep(min(0.1, deadline - loop.time()))
        if signal is not None:
            try:
                if signal.is_set():
                    return False
            except Exception:
                pass
    return True


__all__ = [
    "ProviderRetryPolicy",
    "is_retryable_provider_error",
    "wait_for_retry",
]
