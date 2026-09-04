"""MCP 传输层（Step 16 新增）。

四类实现：
- `MCPTransport`：抽象基类（connect / send / receive / close）
- `StdioMCPTransport`：JSON Lines over asyncio subprocess；Step 16 MVP
- `StreamableHttpMCPTransport`：MCP Streamable HTTP（一问一答 JSON/SSE）
- `FakeMCPTransport`：测试用，无需起 subprocess

JSON-RPC 2.0 协议：
- 每条消息一行（JSON object + `\n`）
- send 写一行；receive 读一行
- 子进程退出 / EOF / JSON 解析失败 → MCPTransportClosedError / MCPProtocolError
"""
from __future__ import annotations

import abc
import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any, Union

import httpx

from .errors import (
    MCPConnectionError,
    MCPProtocolError,
    MCPTransportClosedError,
)

# ============================================================================
# 抽象基类
# ============================================================================


class MCPTransport(abc.ABC):
    """MCP 传输抽象。

    生命周期：
        connect()  →  send()/receive() 反复往返  →  close()

    所有方法都是 async。close 幂等。
    """

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def send(self, message: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    async def receive(self) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


# ============================================================================
# Stdio transport：JSON Lines over asyncio subprocess
# ============================================================================


class StdioMCPTransport(MCPTransport):
    """stdio JSON Lines 传输。

    启动一个子进程（command + args），通过 stdin 写 JSON 行、stdout 读 JSON 行。
    stderr 不参与协议，留给上层 observability。

    close 行为：
    1. 关闭 stdin
    2. 调 `terminate()`（SIGTERM）
    3. 等最多 2s
    4. 还没退出就 `kill()`（SIGKILL）

    close 幂等；多次调用不报错。
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._closed: bool = False

    async def connect(self) -> None:
        if self._proc is not None:
            return
        if self._closed:
            raise MCPTransportClosedError("StdioMCPTransport: already closed")

        # MCP stdio is a UTF-8 JSON protocol.  On Windows, a piped Python child
        # otherwise inherits the active ANSI code page (for example cp936), so
        # non-ASCII tool results are encoded as GBK and then irreversibly become
        # replacement characters when the parent reads them as UTF-8.
        env = dict(os.environ)
        if self._env:
            env.update(self._env)
        # Protocol encoding is not user-configurable.  Override conflicting values
        # from either the parent or MCP config rather than silently corrupting data.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command, *self._args,
                cwd=self._cwd, env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # stderr 直接丢弃：本 transport 不读 stderr，PIPE 缓冲区满
                # 会让子进程阻塞；DEVNULL 简单稳定。需要日志的话后续 step
                # 可以换成"后台 task drain stderr"。
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise MCPConnectionError(
                f"StdioMCPTransport: command not found: {self._command}"
            ) from e
        except OSError as e:
            raise MCPConnectionError(
                f"StdioMCPTransport: failed to spawn '{self._command}': {e}"
            ) from e

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise MCPTransportClosedError("StdioMCPTransport: not connected or closed")
        line = json.dumps(message) + "\n"
        data = line.encode("utf-8")
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise MCPTransportClosedError(
                f"StdioMCPTransport: stdin closed: {e}"
            ) from e

    async def receive(self) -> dict[str, Any]:
        if self._closed or self._proc is None or self._proc.stdout is None:
            raise MCPTransportClosedError("StdioMCPTransport: not connected or closed")
        try:
            line_b = await self._proc.stdout.readline()
        except Exception as e:
            raise MCPTransportClosedError(
                f"StdioMCPTransport: stdout read failed: {e}"
            ) from e

        if not line_b:
            raise MCPTransportClosedError("StdioMCPTransport: stdout EOF (server exited)")

        try:
            line = line_b.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as e:
            # Never pass U+FFFD replacement characters into ToolResult persistence.
            # Invalid protocol bytes are a safe tool error and can be retried after
            # fixing the server encoding; corrupted text cannot be recovered later.
            raise MCPProtocolError(
                "StdioMCPTransport: response is not valid UTF-8"
            ) from e
        if not line:
            raise MCPProtocolError("StdioMCPTransport: empty line from server")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            raise MCPProtocolError(
                f"StdioMCPTransport: invalid JSON: {e}; line={line!r}"
            ) from e
        if not isinstance(msg, dict):
            raise MCPProtocolError(
                f"StdioMCPTransport: JSON line is not an object: {type(msg).__name__}"
            )
        return msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return

        # 1. 关 stdin
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass

        # 2. terminate（SIGTERM）
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            pass

        # 3. 等最多 2s
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except TimeoutError:
            pass
        except Exception:
            return

        # 4. kill（SIGKILL）
        try:
            proc.kill()
        except Exception:
            pass
        # kill 后必须 await wait()，否则在 POSIX 下会留下 zombie 进程
        try:
            await proc.wait()
        except Exception:
            pass


# ============================================================================
# Streamable HTTP transport
# ============================================================================


class StreamableHttpMCPTransport(MCPTransport):
    """MCP Streamable HTTP transport。

    当前 MCPClient 是严格的一问一答客户端，因此每次 ``send`` 发起一个 POST，
    并把 JSON 或 SSE 中与该请求对应的 JSON-RPC response 暂存给 ``receive``。
    transport 会保存初始化响应中的 ``Mcp-Session-Id``，后续请求自动携带；关闭
    时尽力发送 DELETE 释放服务端 Session。HTTP redirect 默认禁止，避免认证头
    被转发到另一个 origin。
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        protocol_version: str = "2025-06-18",
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._protocol_version = protocol_version
        self._timeout_s = timeout_s
        self._client = client
        self._owns_client = client is None
        self._connected = False
        self._closed = False
        self._session_id: str | None = None
        self._next_response: dict[str, Any] | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def set_protocol_version(self, version: str) -> None:
        """Use the version negotiated by the initialize response."""
        if version:
            self._protocol_version = version

    async def connect(self) -> None:
        if self._closed:
            raise MCPTransportClosedError("StreamableHttpMCPTransport: already closed")
        if self._connected:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s,
                follow_redirects=False,
            )
        self._connected = True

    def _request_headers(self, *, initialize: bool = False) -> dict[str, str]:
        headers = dict(self._headers)
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if not initialize:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or not self._connected or self._client is None:
            raise MCPTransportClosedError(
                "StreamableHttpMCPTransport: not connected or closed"
            )
        if self._next_response is not None:
            raise MCPProtocolError(
                "StreamableHttpMCPTransport: receive previous response before send"
            )
        initialize = message.get("method") == "initialize"
        try:
            async with self._client.stream(
                "POST",
                self._url,
                headers=self._request_headers(initialize=initialize),
                json=message,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise MCPConnectionError(
                        "StreamableHttpMCPTransport: server returned HTTP "
                        f"{response.status_code}"
                    )
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id

                # JSON-RPC notifications have no response body. Streamable HTTP
                # servers normally acknowledge them with 202 Accepted.
                if "id" not in message:
                    await response.aread()
                    return

                content_type = response.headers.get("content-type", "").lower()
                if "application/json" in content_type:
                    await response.aread()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise MCPProtocolError(
                            "StreamableHttpMCPTransport: JSON response must be an object"
                        )
                    self._next_response = payload
                    return
                if "text/event-stream" in content_type:
                    self._next_response = await self._read_sse_response(response)
                    return
                raise MCPProtocolError(
                    "StreamableHttpMCPTransport: response content-type must be "
                    "application/json or text/event-stream"
                )
        except httpx.HTTPError as e:
            raise MCPConnectionError(
                f"StreamableHttpMCPTransport: POST failed: {e}"
            ) from e
        except (ValueError, UnicodeError) as e:
            raise MCPProtocolError(
                f"StreamableHttpMCPTransport: invalid response body: {e}"
            ) from e

    @classmethod
    async def _read_sse_response(
        cls, response: httpx.Response
    ) -> dict[str, Any]:
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if data_lines:
                    candidate = cls._decode_sse_candidate("\n".join(data_lines))
                    if candidate is not None:
                        return candidate
                    data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            candidate = cls._decode_sse_candidate("\n".join(data_lines))
            if candidate is not None:
                return candidate
        raise MCPProtocolError(
            "StreamableHttpMCPTransport: SSE did not contain a JSON-RPC response"
        )

    @staticmethod
    def _decode_sse_candidate(candidate: str) -> dict[str, Any] | None:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) and "id" in value else None

    async def receive(self) -> dict[str, Any]:
        if self._closed or not self._connected:
            raise MCPTransportClosedError(
                "StreamableHttpMCPTransport: not connected or closed"
            )
        if self._next_response is None:
            raise MCPProtocolError("StreamableHttpMCPTransport: no response available")
        response = self._next_response
        self._next_response = None
        return response

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        client = self._client
        if client is not None and self._connected and self._session_id:
            try:
                await client.delete(
                    self._url,
                    headers=self._request_headers(),
                )
            except httpx.HTTPError:
                pass
        self._connected = False
        if client is not None and self._owns_client:
            await client.aclose()
        self._client = None


# ============================================================================
# Fake transport：测试用
# ============================================================================


#: handler 类型：sync 或 async callable，输入 send 的 message，返回对应 response dict。
FakeHandler = Union[  # noqa: UP007
    Callable[[dict[str, Any]], dict[str, Any]],
    Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
]


class FakeMCPTransport(MCPTransport):
    """测试用 transport，无需起 subprocess。

    工作模式：
    - `send(message)`：把 message 追加到 `self.sent`；如果设置了 `handler`，
      用 handler 计算 response 并 stash；handler 抛异常时也 stash 异常。
    - `receive()`：返回 stash 的 response；若 stash 是异常则抛出。

    可选注入点（构造参数）：
    - `handler`：按 request 计算 response 的 callable
    - `raise_on_connect`：connect 时抛的异常（模拟连接失败）
    - `raise_on_receive`：receive 时抛的异常 callable（模拟 EOF / 协议错）

    记录：
    - `self.sent`：所有 send 过的 message（list[dict]）
    - `self.received_count`：receive 被调用次数
    """

    def __init__(
        self,
        handler: FakeHandler | None = None,
        *,
        raise_on_connect: Exception | None = None,
        raise_on_receive: Callable[[], Exception] | None = None,
    ) -> None:
        self._handler = handler
        self._raise_on_connect = raise_on_connect
        self._raise_on_receive = raise_on_receive
        self._closed: bool = False
        self._connected: bool = False
        self._next_response: dict[str, Any] | None = None
        self._next_exc: Exception | None = None
        self.sent: list[dict[str, Any]] = []
        self.received_count: int = 0

    async def connect(self) -> None:
        if self._closed:
            raise MCPTransportClosedError("FakeMCPTransport: closed")
        if self._raise_on_connect is not None:
            raise self._raise_on_connect
        self._connected = True

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or not self._connected:
            raise MCPTransportClosedError("FakeMCPTransport: not connected")
        self.sent.append(message)
        if self._handler is not None:
            try:
                resp = self._handler(message)
                if inspect.isawaitable(resp):
                    resp = await resp
                self._next_response = resp
                self._next_exc = None
            except Exception as e:
                self._next_response = None
                self._next_exc = e

    async def receive(self) -> dict[str, Any]:
        if self._closed or not self._connected:
            raise MCPTransportClosedError("FakeMCPTransport: not connected")
        if self._raise_on_receive is not None:
            raise self._raise_on_receive()
        if self._next_exc is not None:
            exc = self._next_exc
            self._next_exc = None
            raise exc
        if self._next_response is None:
            raise MCPProtocolError(
                "FakeMCPTransport: no response stashed (handler 未设置或未返回)"
            )
        resp = self._next_response
        self._next_response = None
        self.received_count += 1
        return resp

    async def close(self) -> None:
        self._closed = True
        self._connected = False


__all__ = [
    "MCPTransport",
    "StdioMCPTransport",
    "StreamableHttpMCPTransport",
    "FakeMCPTransport",
    "FakeHandler",
]
