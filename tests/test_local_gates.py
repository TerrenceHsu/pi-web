"""Gate command composition: offline by default, no hidden installation or live calls."""

from pathlib import Path

import yaml

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


def test_local_gate_typechecks_both_platforms_and_uses_module_pytest() -> None:
    checks = dict(commands("offline", "python", "npm"))
    for platform in ("linux", "win32"):
        assert checks[f"mypy-{platform}"] == [
            "python", "-m", "mypy", "--platform", platform, "src", "evals",
        ]
    for name in ("backend", "worker-tests"):
        # Module invocation adds the repo root for tests/scripts namespace imports.
        assert checks[name][:3] == ["python", "-m", "pytest"]


def test_ci_preserves_repo_imports_and_cross_platform_strict_checks() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    runs = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "run" in step
    ]
    pytest_commands = [run for run in runs if "pytest" in run]
    assert len(pytest_commands) == 3
    assert all(run.startswith("uv run --no-sync python -m pytest ") for run in pytest_commands)
    installs = [run for run in runs if "uv sync" in run]
    assert installs and all("--extra sandbox-e2b" in run for run in installs)
    for platform in ("linux", "win32"):
        assert any(f"mypy --platform {platform} src evals" in run for run in runs)
