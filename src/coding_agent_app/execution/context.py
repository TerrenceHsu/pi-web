"""Immutable server-only request/role binding; never accepted in tool arguments."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .models import ExecutionContext, ExecutionDenied

_current: ContextVar[ExecutionContext | None] = ContextVar("execution_request", default=None)


@contextmanager
def clear_execution_context() -> Iterator[None]:
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def current_execution_context() -> ExecutionContext:
    value = _current.get()
    if value is None:
        raise ExecutionDenied("grant_required")
    return value


@contextmanager
def bind_execution_context(context: ExecutionContext) -> Iterator[None]:
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
