"""Bounded proxy streaming with no public network or real browser required."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from unittest.mock import AsyncMock

import pytest

from pi_agent_core_py.web.browser import network


class RecordingWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.delivered: asyncio.Queue[bytes] = asyncio.Queue()
        self.block_drain: asyncio.Event | None = None
        self.active_drains = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.active_drains += 1
        try:
            if self.block_drain is not None:
                await self.block_drain.wait()
            self.delivered.put_nowait(self.writes[-1])
        finally:
            self.active_drains -= 1

    def close(self) -> None:
        self.closed = True


@dataclass
class Connection:
    proxy: network.PublicBrowserProxy
    client: asyncio.StreamReader
    remote: asyncio.StreamReader
    writer: RecordingWriter
    upstream: RecordingWriter
    task: asyncio.Task[None]

    def assert_closed(self) -> None:
        assert self.task.done()
        assert not self.proxy.connections
        assert self.writer.closed and self.upstream.closed
        assert self.writer.active_drains == self.upstream.active_drains == 0


@asynccontextmanager
async def connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    idle: float = 0.2,
    lifetime: float = 2,
    write_timeout: float = 0.2,
) -> AsyncIterator[Connection]:
    monkeypatch.setattr(network, "CONNECTION_IDLE_SECONDS", idle)
    monkeypatch.setattr(network, "CONNECTION_LIFETIME_SECONDS", lifetime)
    monkeypatch.setattr(network, "WRITE_TIMEOUT_SECONDS", write_timeout)
    proxy = network.PublicBrowserProxy()
    client, remote = asyncio.StreamReader(), asyncio.StreamReader()
    writer, upstream = RecordingWriter(), RecordingWriter()
    connect = AsyncMock(return_value=(remote, upstream))
    monkeypatch.setattr(network, "connect_public", connect)
    auth = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
    client.feed_data(
        f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode()
    )
    task = asyncio.create_task(proxy.handle(client, cast(asyncio.StreamWriter, writer)))
    try:
        assert await asyncio.wait_for(writer.delivered.get(), 1) == (
            b"HTTP/1.1 200 Connection Established\r\n\r\n"
        )
        connect.assert_awaited_once_with("example.com", 443)
        yield Connection(proxy, client, remote, writer, upstream, task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("direction", ["upstream", "downstream"])
async def test_one_way_activity_renews_shared_idle_deadline(
    monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    async with connection(monkeypatch, idle=0.15) as active:
        source, sink = (
            (active.client, active.upstream)
            if direction == "upstream"
            else (active.remote, active.writer)
        )
        # The other direction stays entirely silent for more than two idle periods.
        for index in range(12):
            assert not active.task.done()
            data = f"chunk-{index}".encode()
            source.feed_data(data)
            assert await asyncio.wait_for(sink.delivered.get(), 1) == data
            await asyncio.sleep(0.03)
        assert not active.task.done()
        source.feed_eof()
        await asyncio.wait_for(active.task, 1)
        active.assert_closed()


async def test_silent_connection_expires_and_releases_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with connection(monkeypatch, idle=0.04) as quiet:
        await asyncio.wait_for(quiet.task, 0.5)
        quiet.assert_closed()
        assert len(quiet.writer.writes) == 1  # Only the successful CONNECT response.
        assert not quiet.upstream.writes


@pytest.mark.parametrize("idle,write_timeout", [(0.5, 0.04), (0.04, 0.5)])
async def test_blocked_write_cannot_outlive_write_or_idle_deadline(
    monkeypatch: pytest.MonkeyPatch, idle: float, write_timeout: float,
) -> None:
    async with connection(monkeypatch, idle=idle, write_timeout=write_timeout) as blocked:
        blocked.writer.block_drain = asyncio.Event()
        blocked.remote.feed_data(b"undelivered video")
        await asyncio.wait_for(blocked.task, 0.3)
        blocked.assert_closed()
        assert blocked.writer.delivered.empty()
        assert blocked.writer.writes[-1] == b"undelivered video"


async def test_activity_cannot_extend_hard_connection_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with connection(monkeypatch, idle=0.5, lifetime=0.16) as active:
        async def stream() -> None:
            while True:
                active.remote.feed_data(b"live video")
                await asyncio.sleep(0.02)

        producer = asyncio.create_task(stream())
        try:
            await asyncio.wait_for(active.task, 0.4)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        active.assert_closed()
        assert len(active.writer.writes) >= 3


async def test_proxy_close_cancels_streaming_connections_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with connection(monkeypatch) as active:
        assert active.task in active.proxy.connections
        await active.proxy.close()
        active.assert_closed()
