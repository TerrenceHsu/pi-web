"""One local gate entrypoint; no installs, image pulls or business service changes."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = "src/pi_agent_core_py/web/frontend"


def commands(group: str, python: str, npm: str) -> list[tuple[str, list[str]]]:
    checks: list[tuple[str, list[str]]] = []
    if group in {"offline", "all"}:
        checks.extend([
            ("lock", [python, "-m", "uv", "lock", "--check", "--offline"]),
            ("ruff", [python, "-m", "ruff", "check", "src", "tests", "scripts", "evals"]),
            ("mypy", [python, "-m", "mypy", "src", "evals"]),
            ("worker-ruff", [python, "-m", "ruff", "check", "workers/wiki_parser_worker"]),
            ("worker-mypy", [python, "-m", "mypy", "--config-file",
                              "workers/wiki_parser_worker/pyproject.toml",
                              "workers/wiki_parser_worker/src"]),
            ("analysis-runtime", [python, "scripts/setup_analysis_python.py", "--check"]),
            ("backend", [python, "-m", "pytest", "tests", "--tb=short", "-q", "--maxfail=1",
                         "--basetemp=.test-tmp/local-gate"]),
            ("worker-tests", [python, "-m", "pytest", "workers/wiki_parser_worker/tests",
                              "--no-cov", "--tb=short", "-q",
                              "--basetemp=.test-tmp/local-worker"]),
            ("evals", [python, "-m", "evals", "--gate", "--artifacts-dir",
                       f".eval/local-gate/{uuid4().hex}"]),
            ("frontend-lint", [npm, "--prefix", FRONTEND, "run", "lint"]),
            ("frontend-types", [npm, "--prefix", FRONTEND, "run", "typecheck"]),
            ("frontend-unit", [npm, "--prefix", FRONTEND, "test"]),
            ("frontend-build", [npm, "--prefix", FRONTEND, "run", "build"]),
        ])
    if group in {"browser", "all"}:
        checks.append(("chromium", [npm, "--prefix", "tests/e2e", "run", "test:e2e",
                                     "--", "--retries=0"]))
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["all", "offline", "browser", "docker"], default="all")
    parser.add_argument("--port", type=int, default=8121)
    parser.add_argument("--docker-executable", type=Path)
    parser.add_argument("--image-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("invalid test port")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    checks = commands(args.group, sys.executable, npm)
    environment = dict(os.environ)
    environment.update({
        "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "CI": "1",
        "E2E_PORT": str(args.port), "E2E_PYTHON": sys.executable.replace("\\", "/"),
        "UV_CACHE_DIR": str(ROOT / ".test-tmp" / "uv-cache"),
    })
    # Inherited live-service opt-ins never expand an offline run.
    for name in ("PI_RUN_INTEGRATION", "PI_TEST_DOCKER_IMAGE", "PI_TEST_DOCKER_EXE",
                 "E2E_BASE_URL", "PI_E2E_FAST"):
        environment.pop(name, None)
    if args.group == "docker":
        if (
            args.docker_executable is None or not args.docker_executable.is_absolute()
            or not args.image_id or not args.image_id.startswith("sha256:")
            or len(args.image_id) != 71
        ):
            parser.error("docker requires an absolute --docker-executable and fixed --image-id")
        environment.update({"PI_TEST_DOCKER_IMAGE": args.image_id,
                            "PI_TEST_DOCKER_EXE": str(args.docker_executable)})
        checks = [
            ("docker-smoke", [sys.executable, "scripts/check_bash_docker.py",
                               "--docker-executable", str(args.docker_executable),
                               "--image-id", args.image_id, "--verify"]),
            ("docker-web", [sys.executable, "-m", "pytest", "tests/test_execution_maintenance.py",
                             "tests/test_web_execution.py", "tests/test_web_bash.py",
                             "tests/test_web_task_bash.py", "-m", "docker", "--no-cov",
                             "--tb=short", "-q", "--basetemp=.test-tmp/local-docker"]),
        ]
    if args.dry_run:
        print(json.dumps(checks, indent=2))
        return
    from pi_agent_core_py.maintenance import MaintenanceError, installation_lock

    lock = installation_lock(ROOT / ".test-tmp" / "gate-control")
    try:
        lock.__enter__()
    except MaintenanceError:
        raise SystemExit("Another local gate is running; do not share test state") from None
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / ".test-tmp" / f"gate-{args.group}-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    browser_started = False

    def run(name: str, command: list[str]) -> int:
        started = time.monotonic()
        print(f"[gate] {name} started; log: {output / (name + '.log')}", flush=True)
        with (output / (name + ".log")).open("wb") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
            try:
                code = process.wait(timeout=7200)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=log, stderr=log, check=False)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                code = 124
        results.append({
            "name": name, "exit_code": code, "seconds": round(time.monotonic() - started, 2),
        })
        (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"[gate] {name}: {'PASS' if code == 0 else 'FAIL'}", flush=True)
        return code

    try:
        for name, command in checks:
            browser_started |= name == "chromium"
            if run(name, command):
                break
    finally:
        try:
            if browser_started:
                # npm posttest is skipped on failure. Always remove the test build.
                run("restore-production", [npm, "--prefix", FRONTEND, "run", "build"])
        finally:
            lock.__exit__(None, None, None)
    raise SystemExit(0 if all(item["exit_code"] == 0 for item in results) else 1)


if __name__ == "__main__":
    main()
