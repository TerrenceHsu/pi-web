"""Authenticated, public-IP-only HTTP proxy; CONNECT pins the validated DNS answer.

The browser cannot turn a redirect, subresource or DNS rebinding into a request
to the Web application's authenticated localhost endpoints. No TLS interception.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import secrets
import socket
from urllib.parse import urlsplit, urlunsplit

CONNECTION_IDLE_SECONDS = 180.0
CONNECTION_LIFETIME_SECONDS = 6 * 60 * 60.0
WRITE_TIMEOUT_SECONDS = 10.0


class BrowserNetworkDenied(ValueError):
    pass


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
    return address.is_global and not address.is_multicast


def destination(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
            or port != (443 if parsed.scheme == "https" else 80)
            or any(ord(char) <= 32 for char in url)
            or "\\" in url
        ):
            raise BrowserNetworkDenied("browser_public_http_only")
        if host.rstrip(".").endswith((".localhost", ".local", ".internal")):
            raise BrowserNetworkDenied("browser_public_http_only")
        try:
            if not public_address(host):
                raise BrowserNetworkDenied("browser_public_http_only")
        except ValueError as exc:
            if isinstance(exc, BrowserNetworkDenied) or "." not in host:
                raise BrowserNetworkDenied("browser_public_http_only") from None
        return host, port
    except (ValueError, UnicodeError):
        raise BrowserNetworkDenied("browser_public_http_only") from None


async def resolve_public(host: str, port: int) -> list[tuple[socket.AddressFamily, str]]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(
        dict.fromkeys((family, str(sockaddr[0])) for family, _, _, _, sockaddr in answers)
    )
    if not addresses or any(not public_address(address) for _, address in addresses):
        raise BrowserNetworkDenied("browser_public_http_only")
    return addresses


async def connect_public(
    host: str,
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    addresses = await resolve_public(host, port)
    # All answers were validated before connecting; retry numeric addresses only.
    # Bound both per-address delay and total time in the caller's 10s timeout.
    for family, address in addresses[:4]:
        try:
            async with asyncio.timeout(2):
                return await asyncio.open_connection(address, port, family=family)
        except (OSError, TimeoutError):
            continue
    raise OSError("browser_connect_failed")


class PublicBrowserProxy:
    def __init__(self) -> None:
        self.username = "pi-browser"
        self.password = secrets.token_urlsafe(32)
        self.server: asyncio.Server | None = None
        self.connections: set[asyncio.Task[None]] = set()

    async def start(self) -> str:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=16_384)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        if len(self.connections) >= 64:
            writer.close()
            return
        self.connections.add(task)
        upstream: asyncio.StreamWriter | None = None

        async def drain(sink: asyncio.StreamWriter) -> None:
            async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                await sink.drain()

        try:
            async with asyncio.timeout(CONNECTION_LIFETIME_SECONDS):
                async with asyncio.timeout(10):
                    raw = await reader.readuntil(b"\r\n\r\n")
                    lines = raw.decode("iso-8859-1").split("\r\n")
                    method, target, version = lines[0].split(" ")
                    if version not in {"HTTP/1.0", "HTTP/1.1"}:
                        raise BrowserNetworkDenied()
                    headers = [line.split(":", 1) for line in lines[1:] if line]
                    if any(len(item) != 2 for item in headers):
                        raise BrowserNetworkDenied()
                    auth = next(
                        (v.strip() for k, v in headers if k.lower() == "proxy-authorization"), ""
                    )
                    expected = (
                        "Basic "
                        + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
                    )
                    if not hmac.compare_digest(auth, expected):
                        writer.write(
                            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                            b'Proxy-Authenticate: Basic realm="browser"\r\n'
                            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                        )
                        await drain(writer)
                        return
                    host, port = destination(
                        f"https://{target}/" if method == "CONNECT" else target
                    )
                    if method == "CONNECT" and port != 443:
                        raise BrowserNetworkDenied()
                    remote, upstream = await connect_public(host, port)
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await drain(writer)
                else:
                    parsed = urlsplit(target)
                    request_path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                    forwarded = [
                        (k, v)
                        for k, v in headers
                        if k.lower()
                        not in {
                            "proxy-authorization",
                            "proxy-connection",
                            "connection",
                            "host",
                        }
                    ]
                    head = (
                        f"{method} {request_path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
                    )
                    head += "".join(f"{k}:{v}\r\n" for k, v in forwarded) + "\r\n"
                    upstream.write(head.encode("iso-8859-1"))
                    await drain(upstream)

                # Video can keep receiving without sending application data.
                # Both directions renew one idle deadline only after a successful
                # write; neither activity nor backpressure extends the hard lifetime.
                async with asyncio.timeout(CONNECTION_IDLE_SECONDS) as idle:
                    loop = asyncio.get_running_loop()

                    async def pump(
                        source: asyncio.StreamReader, sink: asyncio.StreamWriter,
                    ) -> None:
                        while data := await source.read(65_536):
                            sink.write(data)
                            await drain(sink)
                            if not idle.expired():
                                idle.reschedule(loop.time() + CONNECTION_IDLE_SECONDS)

                    pipes = [
                        asyncio.create_task(pump(reader, upstream)),
                        asyncio.create_task(pump(remote, writer)),
                    ]
                    try:
                        await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for pipe in pipes:
                            pipe.cancel()
                        await asyncio.gather(*pipes, return_exceptions=True)
        except (
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            # No destination URLs, headers, input text or credentials are logged.
            pass
        finally:
            if upstream:
                upstream.close()
            writer.close()
            self.connections.discard(task)
