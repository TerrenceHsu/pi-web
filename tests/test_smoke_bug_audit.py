"""Smoke test 9: 暴露已知 bug —— max_turns 缺失 / multi-turn tool_use 丢失 /
provider close 缺失 / path sandbox / policy deny。
"""
from __future__ import annotations

import asyncio

import pytest

from pi_agent_core_py.context import convert_to_llm
from pi_agent_core_py.loop import run_event_loop
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent, ToolCallEvent
from pi_agent_core_py.providers import to_anthropic_messages
from pi_agent_core_py.providers.anthropic_compat import AnthropicCompatAdapter
from pi_agent_core_py.tools import AgentTool, ToolRegistry, ToolResult

# ============================================================================
# Bug: run_event_loop 无 max_turns —— 模型持续 yield tool_call 时死循环
# ============================================================================


class EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id, name="echo",
            content=[TextContent(text="ok")],
        )


@pytest.mark.asyncio
async def test_run_event_loop_max_turns_safety_net() -> None:
    """FakeClient 一直返回 tool_call → 应该在 max_turns 截断。

    暴露 bug：run_event_loop 没有 max_turns 保护时，模型持续 yield tool_call
    会无限循环；修复后用户能传 max_turns=N，loop 到达上限后明确收敛为
    error assistant。
    """
    # 模型反复调用 echo —— 100 轮脚本足够触发 max_turns=3
    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(id=f"t{i}", name="echo", arguments={})),
            DoneEvent(stop_reason="tool_use"),
        ]
        for i in range(100)
    ])

    events = []
    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
        tools=ToolRegistry([EchoTool()]),
        max_turns=3,
    ):
        events.append(ev)

    # 最后应有 turn_end + agent_end，且 assistant.stop_reason == 'error'
    turn_ends = [e for e in events if e.type == "turn_end"]
    assert turn_ends
    last_te = turn_ends[-1]
    assert last_te.message.stop_reason == "error"
    assert "max_turns" in (last_te.message.error_message or "")


@pytest.mark.asyncio
async def test_run_event_loop_max_turns_default_is_set() -> None:
    """不传 max_turns 时使用默认值 50（不再无界）。"""
    import inspect
    sig = inspect.signature(run_event_loop)
    assert "max_turns" in sig.parameters
    assert sig.parameters["max_turns"].default == 50


# ============================================================================
# Bug: 多轮 tool 调用上下文丢失（Anthropic）
# ============================================================================


def test_anthropic_multi_turn_preserves_tool_use_in_assistant() -> None:
    """assistant(text + tool_use) → user(tool_result) 必须保留 tool_use block。

    Anthropic 协议：tool_result 必须配对前一条 assistant 含 tool_use block。
    当前 convert_to_llm 把 AssistantMessage 里的 ToolCall 全部过滤，
    导致 to_anthropic_messages 产生的 assistant 缺 tool_use —— 多轮 tool 调用失败。
    """
    tc = ToolCall(id="t1", name="echo", arguments={"q": "hi"})
    msgs = [
        UserMessage(content=[TextContent(text="call echo")]),
        AssistantMessage(
            content=[TextContent(text="ok"), tc],
            api="x", provider="x", model="x",
        ),
        ToolResultMessage(
            tool_call_id="t1", name="echo",
            content=[TextContent(text="hi")],
        ),
    ]
    llm_msgs = convert_to_llm(msgs)
    anthropic_msgs = to_anthropic_messages(llm_msgs)

    # 找到 assistant message —— 必须含 tool_use block
    asst = next(m for m in anthropic_msgs if m["role"] == "assistant")
    block_types = [b["type"] for b in asst["content"]]
    assert "tool_use" in block_types, (
        f"Anthropic assistant 必须含 tool_use block 才能匹配 tool_result；"
        f"实际 block types: {block_types}"
    )


# ============================================================================
# Bug: AnthropicCompatAdapter / GLMClient 无 close 方法 → httpx 泄漏
# ============================================================================


def test_anthropic_compat_adapter_has_close_method() -> None:
    """Adapter 应暴露 aclose()/close() 让上层释放 httpx 连接。"""
    config_cls = AnthropicCompatAdapter
    assert hasattr(config_cls, "close") or hasattr(config_cls, "aclose"), (
        "AnthropicCompatAdapter 应有 close/aclose 方法，避免 httpx AsyncClient 泄漏"
    )


# ============================================================================
# Bug: Provider close integration —— Harness.close 应释放 provider 资源
# ============================================================================


@pytest.mark.asyncio
async def test_anthropic_compat_adapter_close_releases_client() -> None:
    """构造 adapter → close → 内部 httpx.AsyncClient 应被关闭。"""
    from pi_agent_core_py.providers.anthropic_compat import AnthropicCompatConfig

    cfg = AnthropicCompatConfig(
        api_key="test-key-1234567890abcdef1234567890abcdef",
        base_url="https://example.invalid",
        model="test-model",
    )
    adapter = AnthropicCompatAdapter(cfg)
    # 内部 _client 是 AsyncAnthropic —— 它包了 httpx.AsyncClient
    inner_client = adapter._client
    # 应有 close 方法
    if hasattr(adapter, "aclose"):
        await adapter.aclose()
    elif hasattr(adapter, "close"):
        await adapter.close()
    # 验证 inner client 被关闭
    inner_http = getattr(inner_client, "_client", None)
    assert inner_http is not None
    assert inner_http.is_closed, "httpx.AsyncClient 未关闭——资源泄漏"


# ============================================================================
# Bug: run_event_loop 用户传 signal 在 stream 中被 set 后 stop_reason 不一致
# ============================================================================


@pytest.mark.asyncio
async def test_run_event_loop_aborted_assistant_message_present() -> None:
    """signal set 后 loop 应产出 stop_reason='aborted' 的 assistant。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="partial"),
        DoneEvent(stop_reason="stop"),
    ]])

    signal = asyncio.Event()
    signal.set()  # 提前 set

    events = []
    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
        signal=signal,
    ):
        events.append(ev)

    # 应该有 turn_end，并且 message.stop_reason == 'aborted'
    turn_ends = [e for e in events if e.type == "turn_end"]
    assert turn_ends, "缺 turn_end"
    assert turn_ends[-1].message.stop_reason == "aborted"


# ============================================================================
# Policy: deny 应阻断工具执行
# ============================================================================


class WriteFileTool(AgentTool):
    name = "write_file"
    label = "Write"
    description = "writes a file"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id, name="write_file",
            content=[TextContent(text=f"wrote {args.get('path')}")],
        )


@pytest.mark.asyncio
async def test_default_policy_denies_write_to_secret_path() -> None:
    """DefaultToolPermissionPolicy 应拒绝写到 workspace_roots 之外的路径。"""
    from pi_agent_core_py.policy import DefaultToolPermissionPolicy

    policy = DefaultToolPermissionPolicy(
        workspace_roots=["/tmp/workspace"],
        allow_write=True,
    )
    tc = ToolCall(
        id="t1", name="write_file",
        arguments={"path": "/etc/passwd"},
    )
    decision = await policy.check_tool_call(
        tool_call=tc, tool=WriteFileTool(), messages=[],
    )
    assert decision.denied, (
        f"写 /etc/passwd 应被 deny；实际 decision={decision.decision} "
        f"reason={decision.reason}"
    )


@pytest.mark.asyncio
async def test_policy_deny_blocks_tool_execution_in_loop() -> None:
    """policy deny → 工具不执行，ToolResult(is_error=True, error_type=ToolPermissionDenied)。"""
    from pi_agent_core_py.policy import (
        DenyAllToolPermissionPolicy,
        InMemoryToolPermissionAuditLog,
    )

    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="t1", name="write_file",
                arguments={"path": "/tmp/x"},
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop")],
    ])
    audit = InMemoryToolPermissionAuditLog()
    policy = DenyAllToolPermissionPolicy()
    registry = ToolRegistry([WriteFileTool()])

    msgs: list = []
    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="write file",
        client=fake,
        tools=registry,
        permission_policy=policy,
        permission_audit_log=audit,
    ):
        if ev.type == "agent_end":
            msgs.extend(ev.messages)

    # ToolResult 应该是 is_error=True，error_type=ToolPermissionDenied
    tr = next(m for m in msgs if isinstance(m, ToolResultMessage))
    assert tr.is_error
    assert tr.details.get("error_type") == "ToolPermissionDenied"
    # audit 有 1 条 deny 记录
    records = audit.list_records()
    assert any(r.decision == "deny" for r in records)


# ============================================================================
# Path sandbox: ../ 越界
# ============================================================================


def test_path_sandbox_rejects_dotdot_escape(tmp_path) -> None:
    """workspace_roots 之外的 ../ 路径必须被识别为 outside。"""
    from pi_agent_core_py.policy import is_path_within_roots

    root = str(tmp_path)
    # 直接判 ../ 逃逸
    escaping = f"{root}/../evil"
    assert not is_path_within_roots(escaping, [root])


def test_path_sandbox_allows_within_root(tmp_path) -> None:
    from pi_agent_core_py.policy import is_path_within_roots

    root = str(tmp_path)
    inside = f"{root}/subdir/file.txt"
    assert is_path_within_roots(inside, [root])


# ============================================================================
# Policy hook 抛异常不破坏 loop
# ============================================================================


@pytest.mark.asyncio
async def test_policy_hook_exception_does_not_crash_loop() -> None:
    """policy.check_tool_call 抛异常 → loop 包成 error_type=ToolPermissionPolicyError。"""
    from pi_agent_core_py.policy import (
        InMemoryToolPermissionAuditLog,
        ToolPermissionPolicy,
    )

    class BrokenPolicy(ToolPermissionPolicy):
        name = "broken"

        async def check_tool_call(self, *, tool_call, tool, messages):
            raise RuntimeError("policy boom")

    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="t1", name="echo", arguments={},
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop")],
    ])
    audit = InMemoryToolPermissionAuditLog()
    msgs: list = []
    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
        tools=ToolRegistry([EchoTool()]),
        permission_policy=BrokenPolicy(),
        permission_audit_log=audit,
    ):
        if ev.type == "agent_end":
            msgs.extend(ev.messages)

    tr = next(m for m in msgs if isinstance(m, ToolResultMessage))
    assert tr.is_error
    assert tr.details.get("error_type") == "ToolPermissionPolicyError"
