"""Bounded, shell-free Docker CLI transport with a deployment-owned endpoint.

This is an internal control-plane adapter, never an Agent tool. No Docker SDK,
credentials, host environment, implicit context, image pull or shell is used.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .errors import SandboxError

DockerStream = Literal["stdout", "stderr"]
DockerOutput = Callable[[DockerStream, bytes], Awaitable[None]]


@dataclass(frozen=True)
class DockerReply:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    truncated: bool = False


class DockerTransport(Protocol):
    async def call(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_bytes: int = 1024 * 1024,
        on_output: DockerOutput | None = None,
    ) -> DockerReply: ...


def require_plain_path(path: Path, *, directory: bool = False) -> None:
    """Reject links/reparse points in every existing component, not just the leaf."""
    if not path.is_absolute():
        raise SandboxError("invalid_configuration", provider="local_docker")
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SandboxError("invalid_configuration", provider="local_docker")
    leaf = path.stat()
    if not (stat.S_ISDIR(leaf.st_mode) if directory else stat.S_ISREG(leaf.st_mode)):
        raise SandboxError("invalid_configuration", provider="local_docker")


class DockerCLITransport:
    def __init__(self, *, executable: Path, config_directory: Path, endpoint: str) -> None:
        require_plain_path(executable)
        require_plain_path(config_directory, directory=True)
        if any(config_directory.iterdir()):
            raise SandboxError("invalid_configuration", provider="local_docker")
        allowed = (
            {"npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine"}
            if os.name == "nt"
            else {"unix:///var/run/docker.sock"}
        )
        if endpoint not in allowed:
            raise SandboxError("invalid_configuration", provider="local_docker")
        self._prefix = (
            str(executable),
            "--config",
            str(config_directory),
            "--host",
            endpoint,
        )
        self._config_directory = config_directory

    async def call(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_bytes: int = 1024 * 1024,
        on_output: DockerOutput | None = None,
    ) -> DockerReply:
        if not argv or timeout_seconds <= 0 or max_bytes < 1 or any("\0" in arg for arg in argv):
            raise SandboxError("invalid_configuration", provider="local_docker")
        require_plain_path(self._config_directory, directory=True)
        if any(self._config_directory.iterdir()):
            raise SandboxError("invalid_configuration", provider="local_docker")
        # DLL loading on Windows needs SystemRoot; credentials/proxies/context do not.
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR"}
        }
        process: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task[None]] = []
        output = {"stdout": bytearray(), "stderr": bytearray()}
        captured = 0
        truncated = False

        async def drain(reader: asyncio.StreamReader, stream: DockerStream) -> None:
            nonlocal captured, truncated
            while chunk := await reader.read(8192):
                keep = chunk[: max(0, max_bytes - captured)]
                truncated |= len(keep) != len(chunk)
                captured += len(keep)
                output[stream].extend(keep)
                if keep and on_output is not None:
                    await on_output(stream, keep)

        async def feed(writer: asyncio.StreamWriter) -> None:
            try:
                for offset in range(0, len(stdin), 65536):
                    writer.write(stdin[offset : offset + 65536])
                    await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                writer.close()

        try:
            async with asyncio.timeout(timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    *self._prefix,
                    *argv,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdin is not None and process.stdout is not None
                assert process.stderr is not None
                tasks = [
                    asyncio.create_task(feed(process.stdin)),
                    asyncio.create_task(drain(process.stdout, "stdout")),
                    asyncio.create_task(drain(process.stderr, "stderr")),
                ]
                await asyncio.gather(*tasks)
                code = await process.wait()
            return DockerReply(code, bytes(output["stdout"]), bytes(output["stderr"]), truncated)
        except TimeoutError:
            # TimeoutError is an OSError subclass. Preserve the terminal reason
            # so the backend destroys the container and reports timed_out.
            raise
        except OSError:
            raise SandboxError("provider_unavailable", provider="local_docker") from None
        finally:
            # This only cleans up the CLI. The backend must ALSO destroy the container.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
