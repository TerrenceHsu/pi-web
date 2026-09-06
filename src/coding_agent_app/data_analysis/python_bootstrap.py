"""Isolated-interpreter bootstrap: load compute modules without the Web/LLM SDKs."""

from __future__ import annotations

import io
import json
import runpy
import sys
import types
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    source = Path(__file__).resolve().parents[2]
    # These package shells are confined to this disposable interpreter. Importing
    # the product package initializers would unnecessarily load Provider SDKs.
    for name in ("agent_workspace", "coding_agent_app", "coding_agent_app.data_analysis"):
        module = types.ModuleType(name)
        module.__path__ = [str(source.joinpath(*name.split(".")))]
        sys.modules[name] = module
    if sys.argv[1:] == ["--probe"]:
        from coding_agent_app.data_analysis.limits import apply_worker_limits

        apply_worker_limits()
        import matplotlib
        import numpy
        import openpyxl  # type: ignore[import-untyped]
        import pandas
        import pyarrow
        import pydantic

        assert all((matplotlib, numpy, openpyxl, pandas, pyarrow, pydantic))
        print(
            json.dumps(
                {
                    "prefix": sys.prefix,
                    "python": sys.version.split()[0],
                    "versions": {
                        name: version(name)
                        for name in (
                            "pandas",
                            "numpy",
                            "matplotlib",
                            "openpyxl",
                            "pyarrow",
                            "pydantic",
                        )
                    },
                }
            )
        )
        return
    runpy.run_module("coding_agent_app.data_analysis.python_worker", run_name="__main__")


if __name__ == "__main__":
    main()
