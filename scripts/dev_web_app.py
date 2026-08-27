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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from pi_agent_core_py.harness import AgentHarness

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


async def _require_persistent_keyring(secret_backend: str) -> None:
    """Fail fast unless the selected persistent Keyring is really writable."""
    if secret_backend == "memory":
        return
    if secret_backend not in {"auto", "keyring"}:
        raise RuntimeError(
            "PI_AGENT_SECRET_BACKEND must be 'keyring', 'auto', or 'memory'."
        )

    from pi_agent_core_py.secrets import OSKeyringSecretStore

    try:
        store = OSKeyringSecretStore()
        available = await store.probe_write_access()
    except Exception:
        available = False
    if not available:
        raise RuntimeError(
            "Persistent Keyring preflight failed. Start the backend from the "
            "interactive Windows login session so Credential Manager is "
            "available, or explicitly set PI_AGENT_SECRET_BACKEND=memory to "
            "run without persistent API keys."
        )


def _build_harness() -> AgentHarness:
    import asyncio
    import random

    from pi_agent_core_py.agent import Agent
    from pi_agent_core_py.harness import AgentHarness
    from pi_agent_core_py.model_client import (
        DoneEvent,
        FakeClient,
        StreamEvent,
        TextDeltaEvent,
    )
    from pi_agent_core_py.policy import DefaultToolPermissionPolicy

    deltas = ["Hello", " from", " delayed", " fake", " backend"]
    script: list[StreamEvent] = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))

    class _DelayedFakeClient(FakeClient):
        async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            async for ev in super().stream(**kwargs):
                if not isinstance(ev, DoneEvent):
                    await asyncio.sleep(random.uniform(0.075, 0.150))
                yield ev

    scripts = [list(script) for _ in range(200)]
    fake = _DelayedFakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent, permission_policy=DefaultToolPermissionPolicy())
    harness.attach_skills([])
    return harness


def main() -> None:
    import asyncio

    import uvicorn

    from pi_agent_core_py.web.app import create_app
    from pi_agent_core_py.web.auth import AuthUser, create_authenticated_app

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    secret_backend = os.environ.get("PI_AGENT_SECRET_BACKEND", "keyring").strip().lower()

    try:
        asyncio.run(_require_persistent_keyring(secret_backend))
    except RuntimeError as exc:
        raise SystemExit(f"[dev] startup refused: {exc}") from None

    data_root = Path(
        os.environ.get("PI_AGENT_DATA_DIR", str(REPO_ROOT / ".pi-agent-data"))
    ).resolve()
    auth_db_path = data_root / "auth.sqlite"
    user_data_root = data_root / "users"

    default_origins = "http://localhost:5173 http://127.0.0.1:5173"
    extra_origins_env = os.environ.get("EXTRA_UI_ORIGINS", default_origins)
    extra_ui_origins = tuple(o.strip() for o in extra_origins_env.split() if o.strip())

    print(
        f"[dev] starting on http://{host}:{port} "
        f"(auth_db={auth_db_path}, user_data={user_data_root})\n"
        f"[dev] allowed_ui_origins={extra_ui_origins}\n"
        f"[dev] credential_backend={secret_backend} (write probe passed)",
        flush=True,
    )

    def _workspace_app(user: AuthUser, workspace_root: Path) -> FastAPI:
        del user  # The stable user id is already encoded in workspace_root.
        return create_app(
            _build_harness(),
            db_path=str(workspace_root / "workspace.sqlite"),
            uploads_dir=str(workspace_root / "uploads"),
            wiki_root=str(workspace_root / "knowledge"),
            wiki_parser_worker_source_root=REPO_ROOT
            / "workers"
            / "wiki_parser_worker",
            allow_prompt_preview=True,
            enable_trusted_host=True,
            credential_extra_ui_origins=extra_ui_origins,
            credential_secret_backend=secret_backend,
            enable_builtin_ddgs=True,
        )

    app = create_authenticated_app(
        _workspace_app,
        auth_db_path=auth_db_path,
        user_data_root=user_data_root,
        extra_ui_origins=extra_ui_origins,
    )

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
