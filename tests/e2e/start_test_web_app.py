"""Playwright E2E 专用 Web app 启动脚本。

目标：
- 用 FakeClient + AgentHarness 启动真实 FastAPI app
- 监听 127.0.0.1:8000（PORT 环境变量可覆盖）
- 临时 sqlite + uploads_dir（process 退出即丢）
- 静态托管 src/pi_agent_core_py/web/static/（必须先 npm run build）
- 不依赖真实 GLM / 真实 MCP server

用法（被 playwright.config.ts webServer 调用）：

    python tests/e2e/start_test_web_app.py

注意：必须在 conda 环境 `pipy` 下运行；脚本不强校验环境。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 让脚本可以从仓库根目录直接运行——把 src/ 加进 sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _build_test_harness():
    """构造 FakeClient harness——多次 prompt 都返回确定性文本。

    scripts 用 list[list[StreamEvent]]——FakeClient 每次 stream() 调用消费一个 script；
    P0-4 e2e 中多次发 prompt 时按序消费；超过则 FakeClient 抛错（不希望发生）。

    P1-B3: 如果环境变量 PI_E2E_DELAYED=1，构造 delayed FakeClient——每个 delta
    sleep 75-150ms，至少 4 个 delta，让 async prompt 测试能观察 draft 增长。
    """
    import os

    from pi_agent_core_py.agent import Agent
    from pi_agent_core_py.harness import AgentHarness
    from pi_agent_core_py.messages import TextContent, ToolCall
    from pi_agent_core_py.model_client import (
        DoneEvent,
        FakeClient,
        TextDeltaEvent,
        ToolCallEvent,
    )
    from pi_agent_core_py.policy import DefaultToolPermissionPolicy
    from pi_agent_core_py.tools import AgentTool, ToolResult

    class _Utf8DdgsTool(AgentTool):
        """Deterministic DDGS-shaped tool for UTF-8 browser regression tests."""

        name = "mcp__ddgs__search_text"
        label = "DDGS UTF-8 fixture"
        description = "Return a deterministic Chinese search result."
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        async def execute(self, tool_call_id, args, **kwargs):
            del kwargs
            query = str(args.get("query", ""))
            turn = "第二轮" if "第二轮" in query else "第一轮"
            text = f"中文搜索结果：{turn}北京天气晴朗，编码保持完整。"
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(text=text)],
                details={"query": query, "fixture": "utf8-ddgs"},
            )

    # P1-B3-4: 默认 delayed（让 async prompt 测试能观察 draft 增长）；
    # 设置 PI_E2E_FAST=1 切回 fast FakeClient
    delayed = os.environ.get("PI_E2E_FAST") != "1"

    if delayed:
        # delayed script——5 个 delta + DoneEvent
        deltas = ["Hello", " from", " delayed", " fake", " backend"]
        script: list = [TextDeltaEvent(delta=d) for d in deltas]
        script.append(DoneEvent(stop_reason="stop"))
        # 用 dict 当作 FakeClient 的 script 元数据，但 FakeClient 不支持 delay——
        # 用 subclass 包装（见 _DelayedFakeClient）
        from pi_agent_core_py.model_client import FakeClient as _BaseFakeClient

        class _DelayedFakeClient(_BaseFakeClient):
            """FakeClient subclass——stream() 内每个 delta 前 sleep 75-150ms。"""

            async def stream(self, **kwargs):
                import asyncio
                import random

                messages = kwargs.get("messages", [])
                messages_repr = repr(messages)
                latest_user_repr = next(
                    (
                        repr(message)
                        for message in reversed(messages)
                        if getattr(message, "role", None) == "user"
                    ),
                    "",
                )
                if "U_FFFD_HISTORY_TEST" in latest_user_repr:
                    yield TextDeltaEvent(delta="历史内容\ufffd疑似损坏")
                    yield DoneEvent(stop_reason="stop")
                    return
                utf8_turn_count = sum(
                    getattr(message, "role", None) == "user"
                    and "UTF8_DDGS_TURN_" in repr(message)
                    for message in messages
                )
                if utf8_turn_count:
                    call_id = f"call_utf8_ddgs_{utf8_turn_count}"
                    has_current_result = any(
                        getattr(message, "role", None) == "toolResult"
                        and getattr(message, "tool_call_id", None) == call_id
                        for message in messages
                    )
                    if has_current_result:
                        yield TextDeltaEvent(
                            delta=f"DDGS 第{utf8_turn_count}轮处理完成"
                        )
                        yield DoneEvent(stop_reason="stop")
                    else:
                        turn_label = "第二轮" if utf8_turn_count == 2 else "第一轮"
                        yield ToolCallEvent(
                            tool_call=ToolCall(
                                id=call_id,
                                name="mcp__ddgs__search_text",
                                arguments={"query": f"{turn_label} 北京天气"},
                            )
                        )
                        yield DoneEvent(stop_reason="tool_use")
                    return
                approval_marker = None
                if "P2B_APPROVAL_APPROVE" in messages_repr:
                    approval_marker = "approved-e2e.md"
                elif "P2B_APPROVAL_DENY" in messages_repr:
                    approval_marker = "denied-e2e.md"
                if approval_marker is not None:
                    has_tool_result = any(
                        getattr(message, "role", None) == "toolResult"
                        for message in messages
                    )
                    if has_tool_result:
                        await asyncio.sleep(0.4)
                        yield TextDeltaEvent(delta="Approval flow finished")
                        yield DoneEvent(stop_reason="stop")
                    else:
                        yield ToolCallEvent(tool_call=ToolCall(
                            id=f"call_{approval_marker}",
                            name="write_file",
                            arguments={
                                "filename": approval_marker,
                                "content": "created after human approval",
                            },
                        ))
                        yield DoneEvent(stop_reason="tool_use")
                    return

                slow_refresh = "P2A_SLOW_REFRESH" in messages_repr
                # 调用父类 stream 拿到原 events，但插入 delay
                # FakeClient.stream 是 async generator——委托
                async for ev in super().stream(**kwargs):
                    # 每个 event 前 delay（除 DoneEvent）
                    if not isinstance(ev, DoneEvent):
                        delay = 0.4 if slow_refresh else random.uniform(0.075, 0.150)
                        await asyncio.sleep(delay)
                    yield ev

        # D2-8.0: 20 scripts 不够支撑 37 个 E2E（含 regenerate 多次 prompt）
        # 提升到 200 避免脚本耗尽导致后续测试无响应
        scripts = [list(script) for _ in range(200)]
        fake = _DelayedFakeClient(scripts)
    else:
        class _FastFakeClient(FakeClient):
            """Fast fixture with the same integrity-test override as delayed mode."""

            async def stream(self, **kwargs):
                messages = kwargs.get("messages", [])
                latest_user_repr = next(
                    (
                        repr(message)
                        for message in reversed(messages)
                        if getattr(message, "role", None) == "user"
                    ),
                    "",
                )
                if "U_FFFD_HISTORY_TEST" in latest_user_repr:
                    yield TextDeltaEvent(delta="历史内容\ufffd疑似损坏")
                    yield DoneEvent(stop_reason="stop")
                    return
                async for event in super().stream(**kwargs):
                    yield event

        # 默认 fast FakeClient
        one = [
            TextDeltaEvent(delta="hello from fake backend"),
            DoneEvent(stop_reason="stop"),
        ]
        # D2-8.0: 同步提升 fast FakeClient 脚本数
        scripts = [list(one) for _ in range(200)]
        fake = _FastFakeClient(scripts)

    agent = Agent(system_prompt="", client=fake)
    agent.tools.register(_Utf8DdgsTool())
    harness = AgentHarness(agent, permission_policy=DefaultToolPermissionPolicy())
    # attach 一个空 SkillRegistry——让 /api/skills/upload 走 register 路径而非 422
    harness.attach_skills([])
    return harness


def main() -> None:
    import asyncio

    import uvicorn

    from pi_agent_core_py.web.app import create_app
    from pi_agent_core_py.web.auth.gateway import create_authenticated_app
    from pi_agent_core_py.web.auth.passwords import hash_password
    from pi_agent_core_py.web.auth.service import AuthService
    from pi_agent_core_py.web.auth.store import AuthStore

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")

    # P1-C5: 支持 E2E_DB_PATH 固定数据库——用于 persistence E2E 预置测试数据
    fixed_db = os.environ.get("E2E_DB_PATH")
    if fixed_db:
        db_path = Path(fixed_db)
        # 固定 DB 模式：清理旧文件 + -wal + -shm
        for suffix in ("", "-wal", "-shm"):
            old = db_path.with_suffix(db_path.suffix + suffix) if suffix else db_path
            if old.exists():
                old.unlink()
        tmp_root = db_path.parent
    else:
        # 临时 sqlite + uploads_dir——脚本退出时随临时目录清理
        tmp_root = Path(tempfile.mkdtemp(prefix="pi-e2e-"))
        db_path = tmp_root / "e2e.sqlite"
    print(
        f"[e2e] starting on http://{host}:{port} "
        f"(workspace_root={tmp_root})",
        flush=True,
    )

    async def seed_accounts() -> None:
        store = await AuthStore.open(str(tmp_root / "auth.sqlite"))
        try:
            service = AuthService(store)
            await service.ensure_initial_admin()
            if await store.get_user_by_name("alice") is None:
                await store.create_user("alice", hash_password("alice-pass"))
        finally:
            await store.close()

    asyncio.run(seed_accounts())

    # P1-B3-4: E2E 可通过环境变量调整 buffer size——Test 6 buffer gap fallback 用
    # E2E_EVENT_BUFFER_MAX_SIZE=2 让 buffer 快速满，触发 gap=true
    buffer_max_size = int(os.environ.get("E2E_EVENT_BUFFER_MAX_SIZE", "1000"))
    # P1-E M2-4: 启用 TrustedHost——让 Credential / Provider Profile /
    # Session Binding API 自动启用（resolver 的 conservative auto 要求
    # runtime + api + TrustedHost + file DB 四个前置条件）。
    # 不开则 /api/sessions/{sid}/model-binding 返回 404，前端
    # bindingLoadState=error，ChatInput.providerReady 永远 false，
    # 依赖 send-button 的 E2E 都会 timeout。
    def workspace_factory(user, workspace_root):
        """Build the real per-user app behind the authentication gateway."""
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_db = (
            db_path
            if fixed_db and user.name == "admin"
            else workspace_root / "workspace.sqlite"
        )
        uploads_dir = workspace_root / "uploads"
        knowledge_root = workspace_root / "knowledge"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        knowledge_root.mkdir(parents=True, exist_ok=True)
        return create_app(
            _build_test_harness(),
            db_path=str(workspace_db),
            uploads_dir=str(uploads_dir),
            knowledge_root=str(knowledge_root),
            allow_prompt_preview=True,
            event_buffer_max_size=buffer_max_size,
            enable_trusted_host=True,
            enable_builtin_ddgs=False,
        )

    app = create_authenticated_app(
        workspace_factory,
        auth_db_path=tmp_root / "auth.sqlite",
        user_data_root=tmp_root / "users",
        extra_hosts=(host, "localhost", "testserver"),
        extra_ui_origins=(f"http://{host}:{port}",),
    )

    # uvicorn 日志降到 warning——避免淹没 Playwright webServer 输出
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
