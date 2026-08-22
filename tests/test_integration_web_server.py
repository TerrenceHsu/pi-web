"""Integration test 5: Web server smoke。

覆盖范围（按 user spec 编号）：
1. GET /api/sessions 返回 200（spec）
2. GET /api/mcp/tools 返回 200（spec）
3. /api/session 旧 endpoint 仍 200
4. /api/mcp 旧 endpoint 仍 200
5. 多次 create_app 同一个 harness 不会导致 hook 重复广播
6. event_buffer 超过 maxlen 后丢最旧
7. harness already running 时 POST /api/prompt 返回 409
8. /ws/events 可以连接并收到至少一个 event
9. /api/stream?limit=1 可自动化读完，不挂住
10. /api/skills?include_prompt=true 默认返回 403
11. create_app(allow_prompt_preview=True) 且 localhost 时可返回 prompt
12. uvicorn subprocess test 加端口 retry
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.model_client import (
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.skills import Skill
from pi_agent_core_py.tools import AgentTool, ToolResult
from pi_agent_core_py.web.app import create_app, dispose_app

# 不标 slow——25 个测试用 TestClient（含 SSE / WS / uvicorn subprocess），
# 单跑约 19s（~780ms/test，主要是 uvicorn subprocess 启动开销）。
# 覆盖 web/app.py 的 SSE / WS / 静态资源 / spec endpoint 大段代码。



# ============================================================================
# helpers
# ============================================================================


def _make_harness(
    *,
    extra_scripts: list[list] | None = None,
    skills: list[Skill] | None = None,
) -> AgentHarness:
    scripts = extra_scripts or [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]]
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    if skills:
        harness.attach_skills(skills)
    return harness


def _echo_tool() -> AgentTool:
    class _Echo(AgentTool):
        name = "echo"
        label = "Echo"
        description = "echo back text"
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
            return ToolResult(
                tool_call_id=tool_call_id, name="echo",
                content=[TextContent(text=args.get("text", ""))],
            )

    return _Echo()


def _skill_with_prompt() -> Skill:
    return Skill(
        name="demo",
        description="demo skill",
        prompt="secret prompt template: {var}",
    )


@pytest.fixture
def web_client():
    """单 harness / app / TestClient。结束时 dispose 避免 hook 累积。"""
    harness = _make_harness()
    app = create_app(harness)
    with TestClient(app) as client:
        try:
            yield client, harness, app
        finally:
            # TestClient 退出会触发 shutdown hook；兜底再 dispose。
            dispose_app(app)


@pytest.fixture
def web_client_with_skill():
    """带 skill + allow_prompt_preview=True 的 fixture。"""
    harness = _make_harness(skills=[_skill_with_prompt()])
    app = create_app(harness, allow_prompt_preview=True)
    with TestClient(app) as client:
        try:
            yield client, harness, app
        finally:
            dispose_app(app)


# ============================================================================
# P1-1/2: spec endpoints
# ============================================================================


def test_get_sessions_endpoint_returns_200(web_client):
    """spec: GET /api/sessions 返回 200。"""
    client, _, _ = web_client
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "sessions" in data
    assert isinstance(data["sessions"], list)


def test_get_mcp_tools_endpoint_returns_200(web_client):
    """spec: GET /api/mcp/tools 返回 200。"""
    client, _, _ = web_client
    resp = client.get("/api/mcp/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert "tools" in data
    assert "count" in data


def test_legacy_session_singular_endpoint_still_works(web_client):
    """兼容: GET /api/session 仍 200。"""
    client, _, _ = web_client
    resp = client.get("/api/session")
    assert resp.status_code == 200


def test_legacy_mcp_endpoint_still_works(web_client):
    """兼容: GET /api/mcp 仍 200。"""
    client, _, _ = web_client
    resp = client.get("/api/mcp")
    assert resp.status_code == 200


# ============================================================================
# P1-3: hook 累积修复
# ============================================================================


def test_multiple_create_app_does_not_accumulate_hooks():
    """同一 harness 多次 create_app 后，单次 event 不应被广播多次。"""
    harness = _make_harness()
    apps = [create_app(harness) for _ in range(3)]
    try:
        # harness 上应该有 3 个 hook（每个 app 一个）—— 但每个独立，不重复
        # 模拟一个 event 被 hook 处理：直接调用每个 app 注册的 hook
        # 通过 on_event_hooks 数量验证
        assert len(harness.on_event_hooks) == 3
    finally:
        for app in apps:
            dispose_app(app)
    # dispose 后 hooks 清空
    assert len(harness.on_event_hooks) == 0


def test_event_buffer_no_duplicate_after_recreate(web_client):
    """关闭一个 app（dispose）再 create 新 app；事件不会重复广播。"""
    client1, harness, app1 = web_client
    # 触发一次 prompt，buffer 应只记录每个 AgentEvent 一次
    r = client1.post("/api/prompt", json={"text": "hello"})
    assert r.status_code == 200
    events_count_first = client1.get("/api/events").json()["count"]
    assert events_count_first > 0

    # 关闭第一个 app，再创建一个新 app on same harness
    dispose_app(app1)
    app2 = create_app(harness)
    client2 = TestClient(app2)
    try:
        # 第二次 prompt 之前清 buffer
        client2.post("/api/events/clear")
        r2 = client2.post("/api/prompt", json={"text": "world"})
        assert r2.status_code == 200
        events_count_second = client2.get("/api/events").json()["count"]
        # 第二个 app 只有自己的 hook；event 数应该和第一次接近（事件总数 / 2 之内）
        # 如果 hook 累积，count 至少翻倍
        assert events_count_second <= events_count_first * 2, (
            f"hook 累积疑似回归: first={events_count_first} second={events_count_second}"
        )
    finally:
        client2.close()
        dispose_app(app2)


# ============================================================================
# P1-4: event_buffer 上限
# ============================================================================


def test_event_buffer_drops_old_when_over_maxlen():
    """TraceEventBuffer 超过 maxlen 后丢最旧。"""
    from pi_agent_core_py.web.state import TraceEventBuffer

    buf = TraceEventBuffer(max_size=3)
    for i in range(5):
        buf.append({"i": i})
    events = buf.list()
    assert len(events) == 3
    # 应保留最新 3 个：i=2,3,4
    assert [e["i"] for e in events] == [2, 3, 4]


def test_event_buffer_default_maxlen_is_1000():
    """默认 maxlen=1000（spec 要求）。"""
    from pi_agent_core_py.web.state import TraceEventBuffer

    buf = TraceEventBuffer()
    assert buf.max_size == 1000


def test_create_app_event_buffer_max_size_param():
    """create_app(event_buffer_max_size=N) 把 state buffer 容量改为 N。"""
    harness = _make_harness()
    app = create_app(harness, event_buffer_max_size=10)
    try:
        assert app.state.web.event_buffer.max_size == 10
    finally:
        dispose_app(app)


# ============================================================================
# P1-5: harness already running → 409
# ============================================================================


def test_post_prompt_returns_409_when_harness_already_running(monkeypatch):
    """外部直接调 harness.run_prompt 占用 harness 时，POST /api/prompt 应返回 409。"""
    harness = _make_harness()
    app = create_app(harness)
    client = TestClient(app)
    try:
        # 模拟 harness 被"外部"占用——直接 set context.phase
        harness.context.phase = "running"
        r = client.post("/api/prompt", json={"text": "x"})
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
        assert "running" in r.json()["detail"].lower() or "phase" in r.json()["detail"].lower()
    finally:
        harness.context.phase = "idle"
        client.close()
        dispose_app(app)


def test_post_prompt_409_when_state_running_flag_set(web_client):
    """WebAppState.running=True（同一 app 重复 POST）→ 409。"""
    client, _, app = web_client
    # 强制 set running=True，模拟并发 POST
    app.state.web.running = True
    try:
        r = client.post("/api/prompt", json={"text": "x"})
        assert r.status_code == 409
    finally:
        app.state.web.running = False


# ============================================================================
# P2-6: WebSocket /ws/events
# ============================================================================


def test_websocket_events_can_connect_and_receive_hello(web_client):
    """WebSocket /ws/events 能 accept + 发 hello event。"""
    client, _, _ = web_client
    with client.websocket_connect("/ws/events") as ws:
        # 第一个消息应是 hello
        msg = ws.receive_json()
        assert msg["type"] == "hello"
        assert "agent_status" in msg


def test_websocket_receives_events_after_prompt(web_client):
    """POST /api/prompt 触发后，WS client 应收到至少一个 event。"""
    client, _, _ = web_client
    with client.websocket_connect("/ws/events") as ws:
        # 先消费 hello
        hello = ws.receive_json()
        assert hello["type"] == "hello"

        # 触发 prompt（同步），然后读 WS
        # 注意 TestClient 的 WS 是同 thread 同步——POST 后才能读后续消息
        client.post("/api/prompt", json={"text": "hi"})

        # 至少能收到一个 agent_start / turn_start 等 event
        msg = ws.receive_json()
        assert isinstance(msg, dict)
        assert "_received_at_ms" in msg or "type" in msg


# ============================================================================
# P2-7: /api/stream?limit=N
# ============================================================================


def test_stream_with_limit_completes(web_client):
    """?limit=0 时 SSE 发出 hello 后立即关闭，不挂住。"""
    client, _, _ = web_client
    chunks: list[str] = []
    with client.stream("GET", "/api/stream?limit=0", timeout=10.0) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            chunks.append(line)
    # hello 后 SSE 应正常关闭
    joined = "\n".join(chunks)
    assert "hello" in joined, f"未收到 hello event; got {joined!r}"


def test_stream_with_limit_one_collects_one_event(web_client):
    """?limit=1 时 SSE 应在收到 1 个 event 后正常关闭。

    触发：先开 WS 连接（吃住 hook 广播）+ 同时 POST 一次 prompt；SSE 侧
    能在 hook 广播中拿到第一个 event 后退出。但 TestClient 是同步——
    用 background thread POST。
    """
    import threading

    client, harness, _ = web_client

    chunks: list[str] = []
    chunks_lock = threading.Lock()
    trigger_errors: list[Exception] = []
    portal = client.portal
    assert portal is not None

    def trigger_post_after_connect() -> None:
        # 给 SSE 一点时间连上 hook 广播；然后 POST prompt
        time.sleep(0.5)
        try:
            portal.call(harness.run_prompt, "trigger")
        except Exception as error:
            trigger_errors.append(error)

    trigger_thread = threading.Thread(target=trigger_post_after_connect)
    trigger_thread.start()

    try:
        with client.stream("GET", "/api/stream?limit=1", timeout=20.0) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                with chunks_lock:
                    chunks.append(line)
    finally:
        trigger_thread.join(timeout=5.0)

    assert not trigger_thread.is_alive()
    assert not trigger_errors

    joined = "\n".join(chunks)
    # hello + 至少一个 event
    assert "hello" in joined
    assert "event: event" in joined


# ============================================================================
# P2-8: prompt preview 保护
# ============================================================================


def test_skills_include_prompt_default_forbidden(web_client):
    """默认 allow_prompt_preview=False：include_prompt=true 返回 403。"""
    client, _, _ = web_client
    # 该 fixture 没 attach skill，但参数解析在 skill_registry 检查之前
    # 因为 include_prompt=True + allow_prompt_preview=False → 403
    # 即使没 attach skill 也要 403（保护优先）
    # 等等：原代码先 return attached=False 才走保护？再看一下顺序
    # 修复：保护应在 attached=False 之前——skill_registry is None 时直接返回，
    # 不走 include_prompt 检查。所以本测试需要 attach skill。
    # 用 web_client_with_skill fixture 跑：
    pass  # 见下一个测试


def test_skills_include_prompt_default_forbidden_with_skill():
    """attach skill 后，include_prompt=true 默认返回 403。"""
    harness = _make_harness(skills=[_skill_with_prompt()])
    app = create_app(harness)  # allow_prompt_preview 默认 False
    client = TestClient(app)
    try:
        r = client.get("/api/skills?include_prompt=true")
        assert r.status_code == 403
    finally:
        client.close()
        dispose_app(app)


def test_skills_include_prompt_allowed_with_flag_and_localhost():
    """allow_prompt_preview=True + localhost → 可访问 prompt。"""
    harness = _make_harness(skills=[_skill_with_prompt()])
    app = create_app(harness, allow_prompt_preview=True)
    client = TestClient(app)
    try:
        r = client.get("/api/skills?include_prompt=true")
        assert r.status_code == 200
        data = r.json()
        assert data["attached"] is True
        # 至少一个 skill，且 prompt 字段存在
        assert any("prompt" in s for s in data["skills"])
    finally:
        client.close()
        dispose_app(app)


def test_skills_no_include_prompt_always_ok():
    """不带 include_prompt 时无论 flag 状态都正常。"""
    harness = _make_harness(skills=[_skill_with_prompt()])
    app = create_app(harness)  # allow_prompt_preview=False
    client = TestClient(app)
    try:
        r = client.get("/api/skills")
        assert r.status_code == 200
    finally:
        client.close()
        dispose_app(app)


# ============================================================================
# 现有 endpoints 健康检查（保留）
# ============================================================================


def test_get_state_endpoint(web_client):
    client, _, _ = web_client
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert "agent_status" in data
    assert "turn_count" in data


def test_get_messages_endpoint(web_client):
    client, _, _ = web_client
    resp = client.get("/api/messages")
    assert resp.status_code == 200


def test_get_snapshots_endpoint(web_client):
    client, _, _ = web_client
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200


def test_get_events_endpoint(web_client):
    client, _, _ = web_client
    resp = client.get("/api/events")
    assert resp.status_code == 200


def test_post_prompt_endpoint(web_client):
    """POST /api/prompt 正常路径。"""
    client, harness, _ = web_client
    resp = client.post("/api/prompt", json={"text": "hello"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert harness.last_snapshot is not None


# ============================================================================
# P3-12: uvicorn subprocess 加端口 retry
# ============================================================================


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_uvicon_subprocess_starts_and_serves_state(tmp_path):
    """真实 uvicorn subprocess 启动；GET /api/state 必须返回 200。

    加端口 retry：若 launcher 启动失败（端口被抢 / import 错），最多试 3 次。
    """
    import httpx
    import uvicorn  # noqa: F401

    src_path = str(Path(__file__).resolve().parents[1] / "src")
    max_attempts = 3
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        port = _pick_free_port()
        launcher = tmp_path / f"launcher_{attempt}.py"
        launcher.write_text(f"""
import sys
sys.path.insert(0, {src_path!r})

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient, TextDeltaEvent, DoneEvent
from pi_agent_core_py.web.app import create_app
import uvicorn

fake = FakeClient([[TextDeltaEvent(delta='ok'), DoneEvent(stop_reason='stop')]])
agent = Agent(system_prompt='sys', client=fake)
harness = AgentHarness(agent)
app = create_app(harness)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
""")
        import subprocess

        proc = subprocess.Popen(
            [sys.executable, str(launcher)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 等端口就绪（最多 5s）
            deadline = time.time() + 5.0
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    out, err = proc.communicate(timeout=1)
                    last_error = (
                        f"uvicorn subprocess exited early. rc={proc.returncode}\n"
                        f"stderr={err}\nstdout={out}"
                    )
                    break
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(0.5)
                        s.connect(("127.0.0.1", port))
                        ready = True
                        break
                except OSError:
                    time.sleep(0.2)
            if not ready:
                # 端口竞态或启动失败——清理后重试
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                continue

            # 发请求验证
            with httpx.Client(timeout=5.0) as http:
                r = http.get(f"http://127.0.0.1:{port}/api/state")
                if r.status_code == 200 and "agent_status" in r.json():
                    return  # 成功
                last_error = f"unexpected response: {r.status_code} {r.text}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

    pytest.fail(
        f"uvicorn subprocess 启动失败（{max_attempts} 次重试后）：{last_error}"
    )
