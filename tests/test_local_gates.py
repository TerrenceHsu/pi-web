"""Gate command composition: offline by default, no hidden installation or live calls."""

from scripts.run_local_gates import commands


def test_local_full_gate_contains_worker_browser_and_coverage() -> None:
    checks = dict(commands("all", "python", "npm"))
    required = {"lock", "worker-ruff", "worker-mypy", "worker-tests", "backend", "chromium"}
    assert required <= checks.keys()
    assert "--no-cov" not in checks["backend"]
    assert "--retries=0" in checks["chromium"]
    # Evals intentionally refuses overwriting a previous run's evidence.
    assert checks["evals"][-1] != dict(commands("all", "python", "npm"))["evals"][-1]
    assert all("install" not in command and "docker" not in command for command in checks.values())
    assert "chromium" not in dict(commands("offline", "python", "npm"))
    assert list(dict(commands("browser", "python", "npm"))) == ["chromium"]
