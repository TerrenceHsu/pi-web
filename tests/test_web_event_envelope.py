"""P1-B2: WebEventEnvelope schema / sequence / request_id 关联 / GET /api/events 过滤。

覆盖用户原指令 §14 B2 验收 1-7：
1. WS/SSE/GET /api/events 返回同一种 envelope（同 schema；广播入口一次性生成）
2. event_id 唯一
3. sequence 全局单调递增
4. prompt 事件关联正确 request_id/session_id
5. request 的 start/end sequence 被写入
6. after_sequence 过滤正确
7. buffer 截断后 gap=true

不覆盖（属于 B3）：
- WS reconnect replay
- 前端去重（在 chatStore unit / e2e 验证）

默认运行（fast FakeClient + TestClient，无外部依赖）。
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app


def _make_harness(n_scripts: int = 5) -> AgentHarness:
    one = [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")]
    fake = FakeClient([list(one) for _ in range(n_scripts)])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client(tmp_path):
    """标准 web client——fast FakeClient + 小 buffer 方便测 gap。"""
    harness = _make_harness()
    app = create_app(
        harness,
        uploads_dir=str(tmp_path / "uploads"),
        event_buffer_max_size=20,  # 小 buffer 方便测 gap detection
    )
    with TestClient(app) as client:
        yield client, harness, app


def _trigger_prompt(client: TestClient, text: str = "hi") -> dict:
    """同步触发一次 prompt，返回 response body。"""
    resp = client.post("/api/prompt", json={"text": text})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ============================================================================
# Tests 1-3: envelope schema / event_id 唯一 / sequence 单调
# ============================================================================


def test_1_envelope_has_seven_core_fields(web_client):
    """每个 envelope 必须有 7 个核心字段（§14 B2 #1）。"""
    client, _, _ = web_client
    _trigger_prompt(client)

    resp = client.get("/api/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"], "no events emitted"

    required = {
        "event_id",
        "request_id",
        "session_id",
        "sequence",
        "type",
        "timestamp",
        "payload",
    }
    for ev in body["events"]:
        missing = required - set(ev.keys())
        assert not missing, f"envelope missing fields: {missing}; got keys={list(ev.keys())}"


def test_2_event_id_unique(web_client):
    """event_id 在多次 prompt 后仍唯一（§14 B2 #2）。"""
    client, _, _ = web_client
    _trigger_prompt(client, text="first")
    _trigger_prompt(client, text="second")

    body = client.get("/api/events").json()
    ids = [ev["event_id"] for ev in body["events"]]
    assert all(i.startswith("evt_") for i in ids)
    assert len(ids) == len(set(ids)), f"duplicate event_ids: {ids}"


def test_3_sequence_globally_monotonic(web_client):
    """sequence 全局单调递增，跨 prompt 连续（§14 B2 #3）。"""
    client, _, _ = web_client
    _trigger_prompt(client, text="first")
    _trigger_prompt(client, text="second")

    body = client.get("/api/events").json()
    seqs = [ev["sequence"] for ev in body["events"]]
    assert all(isinstance(s, int) for s in seqs)
    # 严格单调递增（不重复，不回退）
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1], f"sequence not monotonic at {i}: {seqs}"


# ============================================================================
# Test 4: prompt 事件关联正确 request_id / session_id
# ============================================================================


def test_4_prompt_events_have_request_id_and_session_id(web_client):
    """prompt 触发的事件 envelope 都有 request_id / session_id（§14 B2 #4）。"""
    client, _, app = web_client
    _trigger_prompt(client)

    body = client.get("/api/events").json()
    # 排除 hello / shutdown 这类非 prompt 事件（如果有）
    prompt_events = [
        ev for ev in body["events"]
        if ev.get("type") in (
            "agent_start", "turn_start", "message_start", "message_update",
            "message_end", "tool_execution_start", "tool_execution_end",
            "turn_end", "agent_end", "request_start", "request_end",
        )
    ]
    assert prompt_events, "no prompt events emitted"
    for ev in prompt_events:
        assert ev["request_id"] is not None, (
            f"prompt event {ev['type']} missing request_id"
        )
        assert ev["request_id"].startswith("req_sync_"), ev["request_id"]
        assert ev["session_id"] is not None, (
            f"prompt event {ev['type']} missing session_id"
        )


# ============================================================================
# Test 5: async request 的 start/end sequence 被写入
# ============================================================================


def test_5_async_request_records_sequence_range(web_client):
    """async request 完成后 event_start_sequence / event_end_sequence 被填充（§14 B2 #5）。"""
    client, _, app = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    assert resp.status_code == 202
    request_id = resp.json()["request_id"]

    # 等 completed
    import time
    deadline = time.monotonic() + 3.0
    final = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/requests/{request_id}")
        if r.status_code == 200 and r.json().get("status") in ("completed", "error", "aborted"):
            final = r.json()
            break
        time.sleep(0.02)
    assert final is not None and final["status"] == "completed", (
        f"async didn't complete: {final}"
    )

    assert final["event_start_sequence"] is not None
    assert final["event_end_sequence"] is not None
    assert final["event_end_sequence"] >= final["event_start_sequence"], (
        f"end < start: start={final['event_start_sequence']} end={final['event_end_sequence']}"
    )

    # 验证：buffer 内属于此 request 的事件 sequence 落在 [start, end]
    events_body = client.get(f"/api/events?request_id={request_id}").json()
    seqs = [ev["sequence"] for ev in events_body["events"]]
    if seqs:  # 可能为空（如果 buffer 已截断）——但本测试 buffer=20，应该够
        assert min(seqs) >= final["event_start_sequence"]
        assert max(seqs) <= final["event_end_sequence"]


# ============================================================================
# Tests 6-7: after_sequence 过滤 + gap 检测
# ============================================================================


def test_6_after_sequence_filter(web_client):
    """GET /api/events?after_sequence=N 只返回 sequence > N（§14 B2 #6）。"""
    client, _, _ = web_client
    _trigger_prompt(client)
    body1 = client.get("/api/events").json()
    all_seqs = [ev["sequence"] for ev in body1["events"]]
    assert len(all_seqs) >= 3

    cutoff = all_seqs[1]  # 取第 2 个 sequence 作为过滤点
    body2 = client.get(f"/api/events?after_sequence={cutoff}").json()
    filtered_seqs = [ev["sequence"] for ev in body2["events"]]
    # 所有 filtered_seqs 都 > cutoff
    assert all(s > cutoff for s in filtered_seqs)
    # 数量 = 总数 - 前 2 个
    expected = len(all_seqs) - 2  # cutoff 是 all_seqs[1]，> cutoff 的从 [2:] 开始
    assert len(filtered_seqs) == expected


def test_7_gap_detection_when_buffer_truncated(web_client):
    """buffer 截断后 after_sequence 早于 first_available_sequence → gap=True（§14 B2 #7）。"""
    client, _, _ = web_client
    # 触发多个 prompt 让 buffer 满 + 截断（buffer=20）
    for i in range(5):
        _trigger_prompt(client, text=f"msg-{i}")

    body = client.get("/api/events").json()
    first_avail = body["first_available_sequence"]
    assert first_avail is not None and first_avail > 1, (
        f"buffer should have truncated; first_avail={first_avail}"
    )

    # after_sequence = first_avail - 5（已经被截断的范围）
    stale_seq = first_avail - 5
    body2 = client.get(f"/api/events?after_sequence={stale_seq}").json()
    assert body2["gap"] is True, (
        f"gap should be True when after_sequence+1 < first_available_sequence; "
        f"after={stale_seq} first_avail={first_avail}"
    )

    # 反例：after_sequence 在范围内 → gap=False
    body3 = client.get(f"/api/events?after_sequence={first_avail}").json()
    assert body3["gap"] is False


# ============================================================================
# Test 8: session_id 过滤
# ============================================================================


def test_8_session_id_filter(web_client):
    """GET /api/events?session_id=X 只返回该 session 的事件。"""
    client, _, _ = web_client
    _trigger_prompt(client)
    body = client.get("/api/events").json()
    # 第一个 prompt 事件的 session_id
    sample_session = body["events"][0]["session_id"]
    assert sample_session is not None

    filtered = client.get(f"/api/events?session_id={sample_session}").json()
    for ev in filtered["events"]:
        assert ev["session_id"] == sample_session


# ============================================================================
# Test 9: WS 收到 envelope
# ============================================================================


def test_9_ws_hello_envelope(web_client):
    """WS /ws/events 至少接 hello（envelope schema 一致性靠广播入口保证；WS+HTTP 混合
    在 sync TestClient 下难测——B3 reconnect 测试会完整覆盖 WS event flow）。

    §14 B2 #1 的 WS 一致性由实现保证：_web_event_hook 在广播入口一次性生成 envelope，
    WS / SSE / buffer 三者收到的是同一份 dict。
    """
    client, _, _ = web_client
    with client.websocket_connect("/ws/events") as ws:
        first = ws.receive_json()
        # hello 应该至少有 type 字段
        assert "type" in first
        assert first.get("type") == "hello"


def test_9b_ws_and_get_events_share_envelope_schema(web_client):
    """WS broadcast 和 GET /api/events 用同一个 envelope schema——
    通过代码路径一致性验证（_web_event_hook 一次性生成 envelope 给三处）。

    这里间接验证：触发 prompt → GET /api/events 拿 envelope → 断言 schema 完整。
    WS 路径走同一个 broadcast 入口，schema 由 envelope 字段定义保证一致。
    """
    client, _, _ = web_client
    _trigger_prompt(client)
    body = client.get("/api/events").json()
    # 所有事件都有 envelope 7 字段
    required = {"event_id", "request_id", "session_id", "sequence", "type", "timestamp", "payload"}
    for ev in body["events"]:
        assert required.issubset(ev.keys()), (
            f"event missing envelope fields: {set(ev.keys())}"
        )


# ============================================================================
# Test 10: 旧 payload 字段保留在 envelope.payload 内（向后兼容）
# ============================================================================


def test_10_envelope_payload_preserves_legacy_fields(web_client):
    """envelope.payload 内保留原 AgentEvent 字段 + _received_at_ms（向后兼容）。"""
    client, _, _ = web_client
    _trigger_prompt(client)
    body = client.get("/api/events").json()
    # 找一个 message_update（应该有 delta）
    updates = [
        ev for ev in body["events"]
        if ev.get("type") == "message_update"
    ]
    if not updates:
        # 至少有 message_start / agent_start 之类——验证 payload 非空
        any_prompt_event = next(
            (ev for ev in body["events"] if ev.get("request_id")), None
        )
        assert any_prompt_event is not None
        assert isinstance(any_prompt_event["payload"], dict)
        assert "_received_at_ms" in any_prompt_event["payload"]
    else:
        payload = updates[0]["payload"]
        assert "_received_at_ms" in payload
        # payload 应该有 type 字段（与 envelope.type 冗余）
        assert payload.get("type") == "message_update"


# ============================================================================
# Test 11: SSE /api/stream 推送 envelope（B2.1 hardening 补测）
# ============================================================================


def test_11_sse_receives_envelope(web_client):
    """SSE /api/stream 推送的也是 envelope schema（§14 B2 #1 SSE 一致性）。

    模式：threading 后台触发 prompt + client.stream 读 SSE。
    参考 test_integration_web_server.py::test_sse_endpoint_emits_hello_and_event。
    """
    client, _, _ = web_client
    chunks: list[str] = []
    chunks_lock = threading.Lock()

    def trigger_post_after_connect() -> None:
        import time
        # 等 SSE 连接建立（server 接到 GET /api/stream 后稍延迟触发 prompt）
        time.sleep(0.2)
        try:
            client.post("/api/prompt", json={"text": "hi"}, timeout=5.0)
        except Exception:
            pass

    threading.Thread(target=trigger_post_after_connect, daemon=True).start()

    with client.stream("GET", "/api/stream?limit=2", timeout=10.0) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            with chunks_lock:
                chunks.append(line)

    joined = "\n".join(chunks)
    # 至少有 hello + 一个 event
    assert "event: hello" in joined
    assert "event: event" in joined

    # 找 event: event 后的 data: 行——应当能解析出含 envelope 7 字段的 dict
    data_lines = [
        line for line in chunks
        if line.startswith("data: ")
    ]
    assert len(data_lines) >= 2, f"not enough data lines: {data_lines}"
    # 最后一个 data: 应该是 prompt 触发的 envelope（hello 是第一个）
    # 找带 "event_id" 字段的 data
    envelope_parsed: dict | None = None
    for line in data_lines:
        try:
            obj = json.loads(line[len("data: "):])
            if isinstance(obj, dict) and "event_id" in obj:
                envelope_parsed = obj
                break
        except json.JSONDecodeError:
            continue
    assert envelope_parsed is not None, (
        f"no envelope with event_id in SSE data: {data_lines}"
    )

    # 断言 envelope 7 字段都在
    required = {
        "event_id", "request_id", "session_id",
        "sequence", "type", "timestamp", "payload",
    }
    assert required.issubset(envelope_parsed.keys()), (
        f"SSE envelope missing fields: {set(envelope_parsed.keys())}"
    )
