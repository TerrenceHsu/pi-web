"""Fixed worker entrypoint. JSON requests are written by the host, not executed."""

from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path

from .limits import apply_worker_limits


def main() -> None:
    try:
        apply_worker_limits()
    except Exception:
        print('{"error_code":"worker_limits_unavailable"}')
        return
    from agent_workspace.analysis import MAX_RESULT_BYTES, AnalysisError, AnalysisRequest

    from .engine import calculate

    try:
        request_path = Path(sys.argv[1])
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        result = calculate(
            Path(payload["source_path"]),
            AnalysisRequest.model_validate(
                payload["request"],
            ),
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
    except MemoryError:
        print('{"error_code":"data_limit_exceeded"}')
    except Exception:
        print('{"error_code":"invalid_or_unsupported_data"}')


if __name__ == "__main__":
    main()
