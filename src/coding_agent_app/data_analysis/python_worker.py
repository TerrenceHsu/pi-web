"""Run explicitly approved code in a disposable dependency-isolated interpreter.

This is NOT a filesystem/network sandbox. The Web service must obtain consent
before launching this entrypoint. Resource limits are defense against accidents.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import traceback
from importlib.metadata import version
from pathlib import Path
from typing import Any

from agent_workspace.analysis import (
    MAX_RESULT_BYTES,
    AnalysisError,
    AnalysisRequest,
    PythonAnalysisRequest,
)

from .limits import apply_worker_limits


class BoundedLog(io.TextIOBase):
    def __init__(self) -> None:
        self.data = bytearray()
        self.truncated = False

    def write(self, text: str) -> int:
        encoded = text.encode("utf-8", errors="replace")
        remaining = max(0, 16_384 - len(self.data))
        self.data.extend(encoded[:remaining])
        self.truncated |= len(encoded) > remaining
        return len(text)

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace") + (
            "\n[output truncated]" if self.truncated else ""
        )


def calculate_python(
    path: Path,
    request: PythonAnalysisRequest,
    directory: Path,
) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams.update({"text.usetex": False, "text.parse_math": False})
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd

    from .engine import _check_shape, _headers, _load, _scalar

    frame, source, warnings = _load(
        path,
        AnalysisRequest(
            file_id=request.file_id,
            action="inspect",
            sheet=request.sheet,
            encoding=request.encoding,
            header_row=request.header_row,
        ),
    )
    if source.get("missing_formula_columns"):
        raise AnalysisError("formula_cache_missing")
    source_rows = len(frame)
    output = BoundedLog()
    namespace: dict[str, Any] = {"df": frame, "pd": pd, "np": np, "plt": plt}
    error = None
    result_frame = pd.DataFrame()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exec(compile(request.code, "<analysis>", "exec"), namespace)  # noqa: S102
            value = namespace.get("result")
            if isinstance(value, pd.Series):
                result_frame = value.to_frame().reset_index()
            elif isinstance(value, pd.DataFrame):
                result_frame = value
            elif value is not None:
                raise TypeError("Assign a pandas DataFrame or Series to result")
    except BaseException as exc:
        # Include actionable code line numbers, never a host traceback/path.
        lines = [
            str(item.lineno)
            for item in traceback.extract_tb(exc.__traceback__)
            if item.filename == "<analysis>"
        ]
        error = f"{type(exc).__name__}: {str(exc)[:1500]}"
        if lines:
            error += " (analysis line " + ", ".join(lines[-5:]) + ")"
        result_frame = pd.DataFrame()
    _check_shape(len(result_frame), len(result_frame.columns))
    columns = _headers(list(result_frame.columns)) if len(result_frame.columns) else []
    total = len(result_frame)
    rows = [
        [_scalar(v) for v in row]
        for row in result_frame.head(request.limit).itertuples(index=False, name=None)
    ]
    if any(isinstance(v, str) and len(v) > 65_536 for row in rows for v in row):
        raise AnalysisError("result_limit_exceeded")
    if len(rows) < total:
        warnings.append(f"Result limited to {len(rows)} of {total} rows; not a full export.")
    has_chart = False
    try:
        if not error and plt.get_fignums():
            figure = plt.figure(plt.get_fignums()[-1])
            # Bound normal chart dimensions; this does not constrain arbitrary code's I/O.
            figure.set_size_inches(8, 4)
            figure.savefig(directory / "chart.png", format="png", dpi=120)
            has_chart = True
            if len(plt.get_fignums()) > 1:
                warnings.append("Only the last pyplot figure is exported.")
    finally:
        plt.close("all")
    with (directory / "result.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)

        def safe(value: Any) -> Any:
            if isinstance(value, str) and value.lstrip()[:1] in {"=", "+", "-", "@"}:
                return "'" + value
            return value

        writer.writerow([safe(c) for c in columns])
        writer.writerows([[safe(v) for v in row] for row in rows])
    return {
        "source": source,
        "source_rows": source_rows,
        "analyzed_rows": source_rows,
        "schema": [],
        "columns": columns,
        "rows": rows[:50],
        "result_rows": total,
        "exported_rows": len(rows),
        "preview_rows": min(50, len(rows)),
        "limited": len(rows) < total,
        "preview_limited": len(rows) > 50,
        "warnings": warnings,
        "has_chart": has_chart,
        "stdout": output.text(),
        "python_error": error,
        "null_policy": "Input empty cells are null; later handling follows approved code.",
        "numeric_policy": "CSV preserves strings; conversions/statistics follow approved code.",
        "execution_policy": "One-time user-approved local Python; not a security sandbox.",
    }


def main() -> None:
    try:
        apply_worker_limits()
    except Exception:
        print('{"error_code":"worker_limits_unavailable"}')
        return
    try:
        request_path = Path(sys.argv[1])
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        result = calculate_python(
            Path(payload["source_path"]),
            PythonAnalysisRequest.model_validate(payload["request"]),
            request_path.parent,
        )
        result["engine_versions"] = {
            "python": sys.version.split()[0],
            **{
                name: version(name)
                for name in ("pandas", "numpy", "matplotlib", "openpyxl", "pyarrow")
            },
        }
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise AnalysisError("result_limit_exceeded")
        print(encoded)
    except AnalysisError as exc:
        print(json.dumps({"error_code": exc.code}))
    except Exception:
        print('{"error_code":"python_worker_failed"}')


if __name__ == "__main__":
    main()
