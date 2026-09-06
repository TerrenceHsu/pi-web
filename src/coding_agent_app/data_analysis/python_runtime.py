"""Admin-configured local interpreter; never fall back to the Web interpreter."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def worker_environment(directory: Path) -> dict[str, str]:
    environment = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
    environment.update(
        {
            "TEMP": str(directory),
            "TMP": str(directory),
            "TMPDIR": str(directory),
            "MPLCONFIGDIR": str(directory / "mpl-cache"),
            "MPLBACKEND": "Agg",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    return environment


async def stop_worker(process: asyncio.subprocess.Process) -> None:
    # POSIX workers start a new process group. Windows worker limits disallow
    # child processes and terminate the job when its owning process exits.
    if os.name == "posix":
        try:
            kill_group = getattr(os, "killpg", None)
            if kill_group is not None:
                kill_group(process.pid, getattr(signal, "SIGKILL", 9))
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


class PythonRuntime:
    def __init__(self, executable: Path | None = None) -> None:
        configured = os.environ.get("PI_AGENT_ANALYSIS_PYTHON")
        project_root = Path(__file__).resolve().parents[3]
        default_root = project_root if (project_root / "pyproject.toml").is_file() else Path.cwd()
        default = (
            default_root
            / ".venv-analysis"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.executable = (executable or (Path(configured) if configured else default)).absolute()
        self.bootstrap = Path(__file__).with_name("python_bootstrap.py")
        self._checked_at = 0.0
        self._capability: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def capability(self) -> dict[str, Any]:
        async with self._lock:
            if time.monotonic() - self._checked_at < 15:
                return dict(self._capability)
            reason = None
            versions: dict[str, str] = {}
            process = None
            try:
                if not self.executable.is_file():
                    raise ValueError("missing")
                async with asyncio.timeout(10):
                    process = await asyncio.create_subprocess_exec(
                        str(self.executable),
                        "-I",
                        "-B",
                        str(self.bootstrap),
                        "--probe",
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=worker_environment(self.executable.parent.parent),
                        start_new_session=os.name == "posix",
                    )
                    assert process.stdout is not None
                    data = await process.stdout.read(8193)
                    if len(data) > 8192 or await process.wait() != 0:
                        raise ValueError("probe failed")
                    info = json.loads(data)
                    same_prefix = await asyncio.to_thread(
                        lambda: Path(info["prefix"]).resolve() == Path(sys.prefix).resolve(),
                    )
                    if same_prefix:
                        raise ValueError("not independent")
                    versions = info["versions"]
            except (OSError, ValueError, KeyError, TimeoutError):
                reason = (
                    "Dedicated Python runtime unavailable. Run scripts/setup_analysis_python.py "
                    "with the project Python, or set PI_AGENT_ANALYSIS_PYTHON "
                    "to a separate environment."
                )
            finally:
                if process is not None:
                    await stop_worker(process)
            self._capability = {"available": reason is None, "reason": reason, "versions": versions}
            self._checked_at = time.monotonic()
            return dict(self._capability)
