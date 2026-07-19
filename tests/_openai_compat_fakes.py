"""Shared fake SDK objects for OpenAI-compat adapter tests (M1-1).

Duck-typed to match adapter's `getattr(...)` access patterns——avoids building
real `ChatCompletionChunk` pydantic instances (heavy + couples tests to SDK
internals).
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

# ============================================================================
# Fake SDK types
# ============================================================================


class _Usage:
    def __init__(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = (
            total_tokens if total_tokens is not None else prompt_tokens + completion_tokens
        )


class _Function:
    def __init__(self, *, name: str | None = None, arguments: str | None = None) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(
        self,
        *,
        index: int = 0,
        id: str | None = None,
        function: _Function | None = None,
    ) -> None:
        self.index = index
        self.id = id
        self.function = function


class _Delta:
    def __init__(
        self,
        *,
        content: str | None = None,
        tool_calls: list[_ToolCallDelta] | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(
        self,
        *,
        delta: _Delta | None = None,
        finish_reason: str | None = None,
        index: int = 0,
    ) -> None:
        self.delta = delta if delta is not None else _Delta()
        self.finish_reason = finish_reason
        self.index = index


class _Chunk:
    def __init__(
        self,
        *,
        choices: list[_Choice] | None = None,
        usage: _Usage | None = None,
    ) -> None:
        self.choices = choices if choices is not None else []
        self.usage = usage


class _StreamEvent:
    """模拟 ChatCompletionStreamEvent——只暴露 type / chunk."""

    def __init__(
        self,
        *,
        type: str = "chunk",
        chunk: _Chunk | None = None,
    ) -> None:
        self.type = type
        self.chunk = chunk


class _NonChunkEvent:
    """模拟非 chunk 事件（content_part / tool_choice 等）——adapter 应跳过."""

    def __init__(self, *, type: str = "content_part") -> None:
        self.type = type
        # 不含 chunk 属性


# ============================================================================
# Fake stream + manager
# ============================================================================


class _FakeAsyncStream:
    """模拟 AsyncStream——async iterable of _StreamEvent.

    raise_on_iter：第一次迭代时抛该异常（模拟 SDK 错误）.
    """

    def __init__(
        self,
        events: Iterable[Any],
        *,
        raise_on_iter: BaseException | None = None,
    ) -> None:
        self._events = list(events)
        self._raise_on_iter = raise_on_iter
        self.iter_started = False
        self.iter_completed = False
        self.manager: _FakeStreamManager | None = None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            self.iter_started = True
            if self._raise_on_iter is not None:
                raise self._raise_on_iter
            for e in self._events:
                yield e
            self.iter_completed = True

        return gen()


class _FakeStreamManager:
    """模拟 AsyncChatCompletionStreamManager——async with 进入返回 stream."""

    def __init__(self, stream: _FakeAsyncStream) -> None:
        self._stream = stream
        self.entered = False
        self.exited = False
        # back-reference 让测试断言 stream 在 with 退出后才 exited
        stream.manager = self

    async def __aenter__(self) -> _FakeAsyncStream:
        self.entered = True
        return self._stream

    async def __aexit__(self, *args: Any) -> bool:
        self.exited = True
        return False  # 不吞异常


class _FakeCompletionsNamespace:
    """模拟 client.chat.completions——stream(**kwargs) 返回 manager."""

    def __init__(self, stream: _FakeAsyncStream) -> None:
        self._stream = stream
        self.last_kwargs: dict[str, Any] | None = None
        self.stream_call_count: int = 0

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        self.last_kwargs = kwargs
        self.stream_call_count += 1
        return _FakeStreamManager(self._stream)


class _FakeChatNamespace:
    def __init__(self, completions_ns: _FakeCompletionsNamespace) -> None:
        self.completions = completions_ns


class _FakeClient:
    """模拟 AsyncOpenAI——暴露 chat / close()."""

    def __init__(self, stream: _FakeAsyncStream) -> None:
        self._completions_ns = _FakeCompletionsNamespace(stream)
        self.chat = _FakeChatNamespace(self._completions_ns)
        self.closed: bool = False
        self.close_count: int = 0

    @property
    def last_kwargs(self) -> dict[str, Any] | None:
        return self._completions_ns.last_kwargs

    @property
    def stream_call_count(self) -> int:
        return self._completions_ns.stream_call_count

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


class _RaisingClient:
    """模拟 AsyncOpenAI，但 chat.completions.stream() 抛异常（构造期错误）."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.closed = False
        self.close_count = 0
        # 仍暴露 chat 结构以便 getattr 不会失败
        # placeholder namespace——不会被调用（stream 会立刻抛异常）
        _ph_stream = _FakeAsyncStream([])
        _ph_ns = _FakeCompletionsNamespace(_ph_stream)
        self.chat = _FakeChatNamespace(_ph_ns)

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


class _RaisingCompletionsNamespace:
    """模拟 client.chat.completions——stream(**kwargs) 直接抛异常."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.last_kwargs: dict[str, Any] | None = None
        self.stream_call_count: int = 0

    def stream(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        self.stream_call_count += 1
        raise self._exc


class _RaisingStreamClient:
    """模拟 AsyncOpenAI，stream() 调用时抛异常（HTTP 级错误模拟）."""

    def __init__(self, exc: BaseException) -> None:
        self._completions_ns = _RaisingCompletionsNamespace(exc)
        self.chat = _FakeChatNamespace(self._completions_ns)
        self.closed = False
        self.close_count = 0

    @property
    def last_kwargs(self) -> dict[str, Any] | None:
        return self._completions_ns.last_kwargs

    @property
    def stream_call_count(self) -> int:
        return self._completions_ns.stream_call_count

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


# ============================================================================
# Chunk builders——便利工厂
# ============================================================================


def make_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
) -> _Usage:
    return _Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def text_delta_chunk(
    text: str,
    *,
    finish_reason: str | None = None,
) -> _Chunk:
    return _Chunk(
        choices=[_Choice(delta=_Delta(content=text), finish_reason=finish_reason)],
    )


def empty_delta_chunk(
    *,
    finish_reason: str | None = None,
    usage: _Usage | None = None,
) -> _Chunk:
    return _Chunk(
        choices=[_Choice(delta=_Delta(), finish_reason=finish_reason)],
        usage=usage,
    )


def usage_only_chunk(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int | None = None,
) -> _Chunk:
    return _Chunk(
        choices=[],
        usage=make_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


def tool_call_delta_chunk(
    index: int,
    *,
    id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
    finish_reason: str | None = None,
) -> _Chunk:
    fn = _Function(name=name, arguments=arguments) if (name or arguments) else None
    return _Chunk(
        choices=[
            _Choice(
                delta=_Delta(
                    tool_calls=[_ToolCallDelta(index=index, id=id, function=fn)],
                ),
                finish_reason=finish_reason,
            ),
        ],
    )


def reasoning_chunk(
    text: str,
    *,
    finish_reason: str | None = None,
) -> _Chunk:
    """Delta 只含 reasoning_content——adapter 应忽略."""
    return _Chunk(
        choices=[_Choice(delta=_Delta(reasoning_content=text), finish_reason=finish_reason)],
    )


def mixed_delta_chunk(
    *,
    content: str | None = None,
    tool_calls: list[_ToolCallDelta] | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    usage: _Usage | None = None,
) -> _Chunk:
    """一个 chunk 同时含多种 delta（content + tool_call + reasoning）."""
    return _Chunk(
        choices=[
            _Choice(
                delta=_Delta(
                    content=content,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning_content,
                ),
                finish_reason=finish_reason,
            ),
        ],
        usage=usage,
    )


def make_event(chunk: _Chunk | None = None) -> _StreamEvent:
    return _StreamEvent(type="chunk", chunk=chunk)


def make_non_chunk_event(event_type: str = "content_part") -> _NonChunkEvent:
    """非 chunk 事件——adapter 应 continue."""
    return _NonChunkEvent(type=event_type)


__all__ = [
    # fakes
    "_FakeAsyncStream",
    "_FakeClient",
    "_RaisingStreamClient",
    # builders
    "make_usage",
    "make_event",
    "make_non_chunk_event",
    "text_delta_chunk",
    "empty_delta_chunk",
    "usage_only_chunk",
    "tool_call_delta_chunk",
    "reasoning_chunk",
    "mixed_delta_chunk",
]
