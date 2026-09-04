"""Persistent, network-free file queue supervisor for the OCI Worker.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol, cast

from . import __version__
from .config import WorkerRoutingConfig, load_routing_config
from .errors import WorkerRuntimeError
from .protocol import (
    ARTIFACT_NAME,
    CANCEL_NAME,
    DESTROY_NAME,
    RECEIPT_NAME,
    REQUEST_NAME,
    SOURCE_NAME,
    STATUS_NAME,
    atomic_write_json,
    canonical_json_bytes,
    load_queue_request,
    read_json_object,
    require_job_directory,
    require_safe_directory,
)

PROBE_NAME = "probe.json"
_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "destroyed"}
_WINDOWS_REPARSE_POINT = 0x400
_STATUS_READ_ATTEMPTS = 5
_STATUS_RETRY_SECONDS = 0.002


class RuntimeController(Protocol):
    def start(self) -> dict[str, object]: ...

    def run(self, provider_job_id: str) -> None: ...

    def alive(self) -> bool: ...

    def terminate(self) -> None: ...


def _readline_with_timeout(stream: IO[str], timeout_seconds: float) -> str:
    result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(stream.readline())
        except BaseException as error:
            result.put(error)

    threading.Thread(target=read, daemon=True).start()
    try:
        value = result.get(timeout=timeout_seconds)
    except queue.Empty as error:
        raise WorkerRuntimeError("dependency_unavailable") from error
    if isinstance(value, BaseException):
        raise WorkerRuntimeError("dependency_unavailable") from value
    return value


class SubprocessRuntimeController:
    """One reusable runtime child; termination is the hard cancel boundary."""

    def __init__(
        self,
        *,
        queue_root: Path,
        config_path: Path,
        model_root: Path,
        startup_timeout_seconds: float = 1800.0,
    ) -> None:
        self._queue_root = queue_root
        self._config_path = config_path
        self._model_root = model_root
        self._startup_timeout = startup_timeout_seconds
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> dict[str, object]:
        self.terminate()
        environment = {
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "HOME": os.environ.get("HOME", "/opt/mineru-home"),
            "LANG": "C.UTF-8",
            "MINERU_MODEL_SOURCE": "local",
            "PATH": os.environ.get("PATH", "/opt/venv/bin:/usr/bin:/bin"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        python_path = os.environ.get("PYTHONPATH")
        if python_path:
            environment["PYTHONPATH"] = python_path
        for name in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        command = [
            sys.executable,
            "-m",
            "wiki_parser_worker.runtime_child",
            "--queue-root",
            str(self._queue_root),
            "--config",
            str(self._config_path),
            "--model-root",
            str(self._model_root),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=environment,
            )
        except OSError as error:
            raise WorkerRuntimeError("dependency_unavailable") from error
        self._process = process
        if process.stdout is None:
            self.terminate()
            raise WorkerRuntimeError("dependency_unavailable")
        line = _readline_with_timeout(process.stdout, self._startup_timeout)
        try:
            ready = json.loads(line)
        except json.JSONDecodeError as error:
            self.terminate()
            raise WorkerRuntimeError("dependency_unavailable") from error
        if (
            not isinstance(ready, dict)
            or set(ready) != {"config_revision", "config_sha256", "type"}
            or ready["type"] != "ready"
        ):
            self.terminate()
            raise WorkerRuntimeError("dependency_unavailable")
        return cast(dict[str, object], ready)

    def run(self, provider_job_id: str) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise WorkerRuntimeError("dependency_unavailable")
        try:
            process.stdin.write(
                canonical_json_bytes(
                    {"op": "run", "provider_job_id": provider_job_id}
                ).decode("utf-8")
                + "\n"
            )
            process.stdin.flush()
        except OSError as error:
            raise WorkerRuntimeError("dependency_unavailable") from error

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _terminalize_status(
    status: dict[str, object],
    *,
    state: str,
    error_code: str,
    now_ms: int,
) -> dict[str, object]:
    attempts_value = status.get("attempts")
    attempts = [dict(item) for item in attempts_value] if isinstance(attempts_value, list) else []
    if attempts and attempts[-1].get("state") == "running":
        started = attempts[-1].get("started_at_ms")
        attempts[-1].update(
            {
                "duration_ms": now_ms - started if isinstance(started, int) else None,
                "finished_at_ms": now_ms,
                "safe_error_code": error_code,
                "state": "cancelled" if state == "cancelled" else "failed",
            }
        )
    return {
        **status,
        "artifact_sha256": None,
        "artifact_size_bytes": None,
        "attempts": attempts,
        "finished_at_ms": now_ms,
        "observed_at_ms": now_ms,
        "phase": "terminal",
        "safe_error_code": error_code,
        "state": state,
    }


def _control_requested(job_dir: Path, name: str) -> bool:
    path = job_dir / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise WorkerRuntimeError("invalid_source") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)
    ):
        raise WorkerRuntimeError("invalid_source")
    return True


class QueueWorkerService:
    """Single-concurrency durable supervisor over a fixed shared queue root."""

    def __init__(
        self,
        *,
        queue_root: Path,
        config: WorkerRoutingConfig,
        controller: RuntimeController,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._root = require_safe_directory(queue_root, create=True)
        self._jobs = require_safe_directory(self._root / "jobs", create=True)
        self._config = config
        self._controller = controller
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._active_job_id: str | None = None
        self._active_deadline_ms: int | None = None
        self._active_status: dict[str, object] | None = None
        self._started = False

    def _write_probe(self, *, available: bool, error_code: str | None) -> None:
        atomic_write_json(
            self._root / PROBE_NAME,
            {
                "available": available,
                "capabilities": {
                    "gpu_presets_require_cuda": True,
                    "media_types": ["application/pdf"],
                    "network_during_job": False,
                    "output_schemas": ["llm-wiki-parser-artifact/v2"],
                    "parsers": ["mineru"],
                    "pipeline_supports_cpu": True,
                    "presets": [
                        "mineru_pipeline",
                        "mineru_gpu_medium",
                        "mineru_gpu_high",
                    ],
                    "requested_modes": ["pipeline", "gpu-medium", "gpu-high"],
                },
                "contract_version": 2,
                "error_code": error_code,
                "license_mode": "mineru_open_source",
                "observed_at_ms": self._clock_ms(),
                "provider": "mineru",
                "routing_config": {
                    "revision": self._config.revision,
                    "schema_version": self._config.schema_version,
                    "sha256": self._config.sha256,
                },
                "worker_version": __version__,
            },
        )

    def start(self) -> None:
        self._recover_interrupted_jobs()
        self._write_probe(available=False, error_code="provider_unavailable")
        try:
            ready = self._controller.start()
            if (
                ready.get("config_revision") != self._config.revision
                or ready.get("config_sha256") != self._config.sha256
            ):
                raise WorkerRuntimeError("invalid_configuration")
        except WorkerRuntimeError:
            self._write_probe(available=False, error_code="provider_unavailable")
            raise
        self._write_probe(available=True, error_code=None)
        self._started = True

    def _recover_interrupted_jobs(self) -> None:
        for entry in sorted(self._jobs.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or entry.is_symlink():
                continue
            try:
                status = read_json_object(entry / STATUS_NAME)
            except WorkerRuntimeError:
                continue
            if status.get("state") == "running":
                atomic_write_json(
                    entry / STATUS_NAME,
                    _terminalize_status(
                        status,
                        state="failed",
                        error_code="provider_unavailable",
                        now_ms=self._clock_ms(),
                    ),
                )

    def _restart_controller(self) -> None:
        self._controller.terminate()
        self._write_probe(available=False, error_code="provider_unavailable")
        ready = self._controller.start()
        if (
            ready.get("config_revision") != self._config.revision
            or ready.get("config_sha256") != self._config.sha256
        ):
            self._write_probe(available=False, error_code="provider_unavailable")
            raise WorkerRuntimeError("invalid_configuration")
        self._write_probe(available=True, error_code=None)

    def _destroy_job(self, job_dir: Path, status: dict[str, object]) -> None:
        now = self._clock_ms()
        destroyed = {
            **status,
            "artifact_sha256": None,
            "artifact_size_bytes": None,
            "finished_at_ms": status.get("finished_at_ms") or now,
            "observed_at_ms": now,
            "phase": "terminal",
            "safe_error_code": None,
            "state": "destroyed",
        }
        for name in (
            ARTIFACT_NAME,
            CANCEL_NAME,
            DESTROY_NAME,
            RECEIPT_NAME,
            REQUEST_NAME,
            SOURCE_NAME,
        ):
            try:
                path = job_dir / name
                if path.exists() or path.is_symlink():
                    metadata = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                        raise WorkerRuntimeError("invalid_source")
                    path.unlink()
            except OSError as error:
                raise WorkerRuntimeError("invalid_source") from error
        atomic_write_json(job_dir / STATUS_NAME, destroyed)

    def _status_for(self, job_dir: Path) -> dict[str, object]:
        for attempt in range(_STATUS_READ_ATTEMPTS):
            try:
                return read_json_object(job_dir / STATUS_NAME)
            except WorkerRuntimeError:
                if attempt + 1 == _STATUS_READ_ATTEMPTS:
                    raise
                time.sleep(_STATUS_RETRY_SECONDS)
        raise WorkerRuntimeError("invalid_source")  # pragma: no cover

    def _clear_active(self) -> None:
        self._active_job_id = None
        self._active_deadline_ms = None
        self._active_status = None

    def _poll_active(self) -> bool:
        if self._active_job_id is None:
            return False
        job_dir = require_job_directory(self._root, self._active_job_id)
        try:
            status = self._status_for(job_dir)
        except WorkerRuntimeError:
            self._controller.terminate()
            cached = self._active_status
            if cached is not None:
                atomic_write_json(
                    job_dir / STATUS_NAME,
                    _terminalize_status(
                        cached,
                        state="failed",
                        error_code="protocol_error",
                        now_ms=self._clock_ms(),
                    ),
                )
            self._clear_active()
            self._restart_controller()
            return True
        self._active_status = status
        if status.get("state") in _TERMINAL_STATES:
            self._clear_active()
            return True
        now = self._clock_ms()
        try:
            cancel_requested = _control_requested(job_dir, CANCEL_NAME)
        except WorkerRuntimeError:
            self._controller.terminate()
            atomic_write_json(
                job_dir / STATUS_NAME,
                _terminalize_status(
                    status,
                    state="failed",
                    error_code="protocol_error",
                    now_ms=now,
                ),
            )
            self._clear_active()
            self._restart_controller()
            return True
        if cancel_requested:
            self._controller.terminate()
            atomic_write_json(
                job_dir / STATUS_NAME,
                _terminalize_status(
                    status,
                    state="cancelled",
                    error_code="cancelled",
                    now_ms=now,
                ),
            )
            self._clear_active()
            self._restart_controller()
            return True
        if self._active_deadline_ms is not None and now >= self._active_deadline_ms:
            self._controller.terminate()
            atomic_write_json(
                job_dir / STATUS_NAME,
                _terminalize_status(
                    status,
                    state="failed",
                    error_code="request_timeout",
                    now_ms=now,
                ),
            )
            self._clear_active()
            self._restart_controller()
            return True
        if not self._controller.alive():
            atomic_write_json(
                job_dir / STATUS_NAME,
                _terminalize_status(
                    status,
                    state="failed",
                    error_code="provider_unavailable",
                    now_ms=now,
                ),
            )
            self._clear_active()
            self._restart_controller()
            return True
        return False

    def run_once(self) -> bool:
        if not self._started:
            raise WorkerRuntimeError("invalid_configuration")
        if self._active_job_id is not None:
            return self._poll_active()
        if not self._controller.alive():
            self._restart_controller()
            return True
        for entry in sorted(self._jobs.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or entry.is_symlink():
                continue
            try:
                job_dir = require_job_directory(self._root, entry.name)
                status = self._status_for(job_dir)
            except WorkerRuntimeError:
                continue
            state = status.get("state")
            try:
                destroy_requested = _control_requested(job_dir, DESTROY_NAME)
            except WorkerRuntimeError:
                continue
            if destroy_requested and state in _TERMINAL_STATES:
                self._destroy_job(job_dir, status)
                return True
            if state != "queued":
                continue
            try:
                request = load_queue_request(job_dir)
            except WorkerRuntimeError:
                atomic_write_json(
                    job_dir / STATUS_NAME,
                    _terminalize_status(
                        status,
                        state="failed",
                        error_code="protocol_error",
                        now_ms=self._clock_ms(),
                    ),
                )
                return True
            limits = request.spec.get("limits")
            timeout = limits.get("timeout_seconds") if isinstance(limits, dict) else None
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                atomic_write_json(
                    job_dir / STATUS_NAME,
                    _terminalize_status(
                        status,
                        state="failed",
                        error_code="protocol_error",
                        now_ms=self._clock_ms(),
                    ),
                )
                return True
            started = self._clock_ms()
            claimed = {
                **status,
                "observed_at_ms": started,
                "phase": "preflight",
                "started_at_ms": started,
                "state": "running",
            }
            atomic_write_json(job_dir / STATUS_NAME, claimed)
            self._active_job_id = entry.name
            self._active_deadline_ms = started + timeout * 1000
            self._active_status = claimed
            try:
                self._controller.run(entry.name)
            except WorkerRuntimeError:
                atomic_write_json(
                    job_dir / STATUS_NAME,
                    _terminalize_status(
                        claimed,
                        state="failed",
                        error_code="provider_unavailable",
                        now_ms=self._clock_ms(),
                    ),
                )
                self._clear_active()
                self._restart_controller()
            return True
        return False

    def close(self) -> None:
        self._controller.terminate()
        self._started = False


def serve(
    *,
    queue_root: Path,
    config_path: Path,
    model_root: Path,
    poll_interval_seconds: float = 0.05,
) -> None:
    root = require_safe_directory(queue_root, create=True)
    config = load_routing_config(config_path)
    controller = SubprocessRuntimeController(
        queue_root=root,
        config_path=config_path,
        model_root=model_root,
    )
    service = QueueWorkerService(queue_root=root, config=config, controller=controller)
    service.start()
    try:
        while True:
            service.run_once()
            time.sleep(poll_interval_seconds)
    finally:
        service.close()


__all__ = [
    "PROBE_NAME",
    "QueueWorkerService",
    "RuntimeController",
    "SubprocessRuntimeController",
    "serve",
]
