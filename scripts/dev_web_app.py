"""Dev-mode Web app 启动脚本。

与 tests/e2e/start_test_web_app.py 的差异：
- credential_extra_ui_origins 默认包含 vite dev origin（http://localhost:5173 /
  http://127.0.0.1:5173），让浏览器从 vite dev server 发起的 mutating 请求
  通过 require_allowed_origin_dep 校验
- HOST/PORT/EXTRA_UI_ORIGINS 环境变量可调

不会影响 E2E 测试基础设施——E2E 仍用 start_test_web_app.py。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _build_harness():
    import asyncio
    import random

    from pi_agent_core_py.agent import Agent
    from pi_agent_core_py.harness import AgentHarness
    from pi_agent_core_py.model_client import (
        DoneEvent,
        FakeClient,
        TextDeltaEvent,
    )

    deltas = ["Hello", " from", " delayed", " fake", " backend"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))

    class _DelayedFakeClient(FakeClient):
        async def stream(self, **kwargs):
            async for ev in super().stream(**kwargs):
                if not isinstance(ev, DoneEvent):
                    await asyncio.sleep(random.uniform(0.075, 0.150))
                yield ev

    scripts = [list(script) for _ in range(200)]
    fake = _DelayedFakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


def main() -> None:
    import uvicorn

    from pi_agent_core_py.web.app import create_app

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")

    tmp_root = Path(tempfile.mkdtemp(prefix="pi-dev-"))
    db_path = tmp_root / "dev.sqlite"
    uploads_dir = tmp_root / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    default_origins = "http://localhost:5173 http://127.0.0.1:5173"
    extra_origins_env = os.environ.get("EXTRA_UI_ORIGINS", default_origins)
    extra_ui_origins = tuple(o.strip() for o in extra_origins_env.split() if o.strip())

    print(
        f"[dev] starting on http://{host}:{port} "
        f"(db={db_path}, uploads={uploads_dir})\n"
        f"[dev] allowed_ui_origins={extra_ui_origins}",
        flush=True,
    )

    harness = _build_harness()
    app = create_app(
        harness,
        db_path=str(db_path),
        uploads_dir=str(uploads_dir),
        allow_prompt_preview=True,
        enable_trusted_host=True,
        credential_extra_ui_origins=extra_ui_origins,
    )

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
