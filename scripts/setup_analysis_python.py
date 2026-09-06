"""Create the project's opt-in, dependency-isolated lightweight Python runtime.

Run this setup script with the project's pipy Python. No application credentials
or project package are installed into the runtime. Never run during a tool call.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import venv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check only; do not install anything")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = root / ".venv-analysis"
    executable = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not args.check:
        if target.exists() and not (target / "pyvenv.cfg").is_file():
            raise SystemExit("Refusing to modify an existing non-venv directory")
        if not executable.is_file():
            venv.EnvBuilder(with_pip=False, clear=False).create(target)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "uv",
                "pip",
                "install",
                "--python",
                str(executable),
                "pydantic>=2.7,<3",
                "pandas>=2.2,<4",
                "numpy>=2,<3",
                "matplotlib>=3.9,<4",
                "openpyxl>=3.1,<4",
                "pyarrow>=15",
            ],
            check=True,
        )
    if not executable.is_file():
        raise SystemExit("Analysis environment is not installed")
    subprocess.run(
        [
            str(executable),
            "-I",
            "-B",
            str(root / "src/coding_agent_app/data_analysis/python_bootstrap.py"),
            "--probe",
        ],
        check=True,
    )
    print(f"Analysis Python ready: {executable}")


if __name__ == "__main__":
    main()
