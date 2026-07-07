"""Step 15 — Compaction / Branch Summary 测试（对照用户清单 26.1–26.20）。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    AgentTool,
    BranchSummary,
    BranchSummaryConfig,
    CompactionConfig,
    CompactionResult,
    DoneEvent,
    FakeClient,
    InMemorySessionStore,
    SessionMemory,
    SummaryMessage,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    Usage,
    UserMessage,
    compact_messages,
    create_branch_summary,
    default_summary_generator,
    deserialize_message,
    serialize_message,
)
from pi_agent_core_py.context import convert_to_llm

# ============================================================================
# fixtures
# ============================================================================


class EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = "Echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, tool_call_id: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id, name=self.name,
            content=[TextContent(text=args.get("text", ""))],
        )


class GatedTool(AgentTool):
    name = "gated"
    label = "Gated"
    description = "Gated"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    def __init__(self, gate: asyncio.Event):
        self.gate = gate

    async def execute(self, tool_call_id: str, args: dict[str, Any]) -> ToolResult:
        await self.gate.wait()
        return ToolResult(
            tool_call_id=tool_call_id, name=self.name,
            content=[TextContent(text=args.get("text", ""))],
        )


def _text_script(text: str = "ok") -> list:
    return [TextDeltaEvent(delta=text), DoneEvent(stop_reason="stop", usage=Usage())]


def _make_messages(n: int, prefix: str = "msg") -> list:
    """构造 n 条 UserMessage + AssistantMessage 交替的 messages。"""
    msgs = []
    for i in range(n):
        msgs.append(UserMessage(content=[TextContent(text=f"{prefix}-user-{i}")]))
        # 不需要 AssistantMessage——单 type 也够测 compaction
    return msgs


# ============================================================================
# 26.1 SummaryMessage 序列化 / 反序列化
# ============================================================================


def test_summary_message_serialize_roundtrip():
    msg = SummaryMessage(
        summary_type="context_compaction",
        content=[TextContent(text="这是摘要")],
        source_message_count=10,
        source_snapshot_ids=["snap-1", "snap-2"],
        source_turn_count=2,
        metadata={"k": "v"},
    )

    d = serialize_message(msg)
    assert d["role"] == "summary"
    assert d["summary_type"] == "context_compaction"
    assert d["source_message_count"] == 10

    restored = deserialize_message(d)
    assert isinstance(restored, SummaryMessage)
    assert restored.role == "summary"
    assert restored.content[0].text == "这是摘要"


# ============================================================================
# 26.2 convert_to_llm 支持 SummaryMessage
# ============================================================================


def test_convert_to_llm_summary_message():
    msg = SummaryMessage(
        content=[TextContent(text="历史摘要内容")],
    )
    llm_msgs = convert_to_llm([msg])
    assert len(llm_msgs) == 1
    # 转成 LLMUserMessage
    assert llm_msgs[0].role == "user"
    text = llm_msgs[0].content[0].text
    assert "[Conversation Summary]" in text
    assert "历史摘要内容" in text


# ============================================================================
# 26.3 compact_messages 不足最小条数不执行
# ============================================================================


@pytest.mark.asyncio
async def test_compact_messages_not_enough():
    msgs = _make_messages(3)
    config = CompactionConfig(min_messages_to_compact=10, keep_last_n_messages=2)

    result = await compact_messages(msgs, config=config)

    assert result.applied is False
    assert result.reason == "not_enough_messages"


# ============================================================================
# 26.4 compact_messages 正常压缩
# ============================================================================


@pytest.mark.asyncio
async def test_compact_messages_normal():
    msgs = _make_messages(20)
    config = CompactionConfig(
        keep_last_n_messages=4,
        min_messages_to_compact=5,
    )

    result = await compact_messages(msgs, config=config)

    assert result.applied is True
    assert result.reason is None
    assert result.summary_message is not None
    assert isinstance(result.summary_message, SummaryMessage)
    assert len(result.retained_messages) == 4
    # new_messages = [summary, *retained]
    assert len(result.new_messages) == 5
    assert result.new_messages[0]["role"] == "summary"
    # 剩下 4 条
    assert [m["role"] for m in result.new_messages[1:]] == ["user"] * 4
    # 压缩掉了 16 条
    assert len(result.compacted_messages) == 16
    assert result.source is not None
    assert result.source.source_message_count == 20
    assert result.source.retained_message_count == 4
    assert result.source.compacted_message_count == 16


# ============================================================================
# 26.5 keep_last_n_messages=0
# ============================================================================


@pytest.mark.asyncio
async def test_compact_messages_keep_zero():
    msgs = _make_messages(5)
    config = CompactionConfig(
        keep_last_n_messages=0,
        min_messages_to_compact=3,
    )

    result = await compact_messages(msgs, config=config)

    assert result.applied is True
    assert len(result.retained_messages) == 0
    # new_messages 只有 SummaryMessage
    assert len(result.new_messages) == 1
    assert result.new_messages[0]["role"] == "summary"


# ============================================================================
# 26.6 default_summary_generator 包含用户请求
# ============================================================================


@pytest.mark.asyncio
async def test_default_summary_generator_includes_user_requests():
    from pi_agent_core_py import CompactionInput

    msgs_dicts = [
        {"role": "user", "content": [{"type": "text", "text": "我要做一个 agent"}], "timestamp": 0},
        {"role": "assistant", "content": [{"type": "text", "text": "好的"}],
         "api": "", "provider": "", "model": "", "stop_reason": "stop",
         "usage": {"input": 0, "output": 0, "total_tokens": 0}, "timestamp": 0},
    ]
    inp = CompactionInput(
        messages_to_compact=msgs_dicts,
        retained_messages=[],
        config={"max_summary_chars": 4000, "include_tool_results": True},
    )
    text = default_summary_generator(inp)

    assert "我要做一个 agent" in text
    assert "Key User Requests" in text


# ============================================================================
# 26.7 AgentHarness.compact_context
# ============================================================================


@pytest.mark.asyncio
async def test_harness_compact_context():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    # 跑 10 轮 build up messages
    for i in range(10):
        await harness.run_prompt(f"turn-{i}")

    config = CompactionConfig(
        keep_last_n_messages=4,
        min_messages_to_compact=5,
    )
    result = await harness.compact_context(config=config)

    assert result.applied is True
    # agent.messages[0] 是 summary
    assert agent.state.messages[0].role == "summary"
    # harness.last_compaction_result
    assert harness.last_compaction_result is result


# ============================================================================
# 26.8 compact_context running 时拒绝
# ============================================================================


@pytest.mark.asyncio
async def test_compact_context_during_running_rejected():
    gate = asyncio.Event()
    fake = FakeClient([
        [ToolCallEvent(tool_call=ToolCall(id="c1", name="gated", arguments={})),
         DoneEvent(stop_reason="tool_use", usage=Usage())],
        _text_script("done"),
    ])
    agent = Agent(system_prompt="x", client=fake, tools=[GatedTool(gate)])
    harness = AgentHarness(agent)

    task = asyncio.create_task(harness.run_prompt("hi"))
    await asyncio.sleep(0.05)
    assert agent.state.status == "running"

    with pytest.raises(RuntimeError, match="Cannot compact"):
        await harness.compact_context()

    gate.set()
    await task


# ============================================================================
# 26.9 compact_context 写入 session
# ============================================================================


@pytest.mark.asyncio
async def test_compact_context_writes_session():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    harness.attach_session(session)

    for i in range(10):
        await harness.run_prompt(f"turn-{i}")

    config = CompactionConfig(keep_last_n_messages=4, min_messages_to_compact=5)
    await harness.compact_context(config=config)

    assert len(session.state.compactions) == 1
    assert session.state.messages[0]["role"] == "summary"


# ============================================================================
# 26.10 compact_session
# ============================================================================


@pytest.mark.asyncio
async def test_compact_session():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    harness.attach_session(session)

    for i in range(10):
        await harness.run_prompt(f"turn-{i}")

    config = CompactionConfig(keep_last_n_messages=4, min_messages_to_compact=5)
    result = await harness.compact_session(config=config)

    assert result.applied is True
    # session.messages 被压缩
    assert session.state.messages[0]["role"] == "summary"
    # agent 同步
    assert agent.state.messages[0].role == "summary"
    assert [m.role for m in agent.state.messages] == [
        d["role"] for d in session.state.messages
    ]


# ============================================================================
# 26.11 compaction 不删除 snapshots
# ============================================================================


@pytest.mark.asyncio
async def test_compaction_preserves_snapshots():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    harness.attach_session(session)

    for i in range(10):
        await harness.run_prompt(f"turn-{i}")
    snap_count_before = len(session.state.snapshots)
    assert snap_count_before == 10

    config = CompactionConfig(keep_last_n_messages=4, min_messages_to_compact=5)
    await harness.compact_session(config=config)

    # snapshots 不变
    assert len(session.state.snapshots) == snap_count_before


# ============================================================================
# 26.12 BranchSummary 创建
# ============================================================================


@pytest.mark.asyncio
async def test_create_branch_summary():
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    for i in range(5):
        await harness.run_prompt(f"turn-{i}")

    summary = await harness.create_branch_summary(
        config=BranchSummaryConfig(branch_id="main", title="Main Summary"),
    )

    assert isinstance(summary, BranchSummary)
    assert summary.branch_id == "main"
    assert summary.title == "Main Summary"
    assert summary.summary  # 非空


# ============================================================================
# 26.13 BranchSummary 不改变 messages
# ============================================================================


@pytest.mark.asyncio
async def test_branch_summary_does_not_change_messages():
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    harness.attach_session(session)

    for i in range(5):
        await harness.run_prompt(f"turn-{i}")

    agent_msgs_before = list(agent.state.messages)
    session_msgs_before = list(session.state.messages)

    await harness.create_branch_summary(
        config=BranchSummaryConfig(branch_id="main"),
    )

    # messages 不变
    assert agent.state.messages == agent_msgs_before
    assert session.state.messages == session_msgs_before


# ============================================================================
# 26.14 BranchSummary 写入 session
# ============================================================================


@pytest.mark.asyncio
async def test_branch_summary_writes_session():
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    harness.attach_session(session)

    for i in range(5):
        await harness.run_prompt(f"turn-{i}")

    summary = await harness.create_branch_summary(
        config=BranchSummaryConfig(branch_id="main"),
    )

    assert len(session.state.branch_summaries) == 1
    fetched = harness.get_branch_summary()
    assert fetched is not None
    assert fetched.branch_id == "main"
    assert fetched.id == summary.id


# ============================================================================
# 26.15 clear_branch_summaries
# ============================================================================


@pytest.mark.asyncio
async def test_clear_branch_summaries():
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    harness.attach_session(session)

    for i in range(3):
        await harness.run_prompt(f"turn-{i}")

    await harness.create_branch_summary()
    await harness.create_branch_summary()
    assert len(session.state.branch_summaries) == 2

    harness.clear_branch_summaries()

    assert session.state.branch_summaries == []
    assert harness.last_branch_summary is None
    assert harness.get_branch_summary() is None


# ============================================================================
# 26.16 SessionMemory append_compaction / get_compactions
# ============================================================================


@pytest.mark.asyncio
async def test_session_append_get_compactions():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    for i in range(10):
        await harness.run_prompt(f"turn-{i}")

    config = CompactionConfig(keep_last_n_messages=4, min_messages_to_compact=5)
    result = await harness.compact_context(config=config)

    session = SessionMemory(session_id="s1")
    session.append_compaction(result)

    compactions = session.get_compactions()
    assert len(compactions) == 1
    assert isinstance(compactions[0], CompactionResult)
    assert compactions[0].applied is True


# ============================================================================
# 26.17 SessionMemory append_branch_summary / get_branch_summaries
# ============================================================================


@pytest.mark.asyncio
async def test_session_append_get_branch_summaries():
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    for i in range(3):
        await harness.run_prompt(f"turn-{i}")

    summary = await harness.create_branch_summary(
        config=BranchSummaryConfig(branch_id="b1"),
    )

    session = SessionMemory(session_id="s1")
    session.append_branch_summary(summary)

    fetched = session.get_branch_summaries()
    assert len(fetched) == 1
    assert isinstance(fetched[0], BranchSummary)
    assert fetched[0].branch_id == "b1"


# ============================================================================
# 26.18 auto-save after compaction
# ============================================================================


@pytest.mark.asyncio
async def test_auto_save_after_compaction():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="s1")
    store = InMemorySessionStore()
    harness.attach_session(
        session, store=store, auto_save_policy="after_any_request",
    )

    for i in range(10):
        await harness.run_prompt(f"turn-{i}")

    config = CompactionConfig(keep_last_n_messages=4, min_messages_to_compact=5)
    await harness.compact_session(config=config)

    # store 中 session 已更新（messages[0] 是 summary）
    loaded = await store.load("s1")
    assert loaded.state.messages[0]["role"] == "summary"
    assert len(loaded.state.compactions) == 1


# ============================================================================
# 26.19 custom summary_generator
# ============================================================================


@pytest.mark.asyncio
async def test_custom_summary_generator_sync():
    msgs = _make_messages(10)

    def gen(input):
        return "CUSTOM SUMMARY"

    result = await compact_messages(
        msgs,
        config=CompactionConfig(keep_last_n_messages=2, min_messages_to_compact=5),
        summary_generator=gen,
    )

    assert result.applied is True
    assert "CUSTOM SUMMARY" in result.summary_message.content[0].text


@pytest.mark.asyncio
async def test_custom_summary_generator_async():
    msgs = _make_messages(10)

    async def gen(input):
        await asyncio.sleep(0)
        return "ASYNC SUMMARY"

    result = await compact_messages(
        msgs,
        config=CompactionConfig(keep_last_n_messages=2, min_messages_to_compact=5),
        summary_generator=gen,
    )

    assert "ASYNC SUMMARY" in result.summary_message.content[0].text


# ============================================================================
# 26.20 不做 Vector Memory
# ============================================================================


def test_no_vector_memory_types():
    """Step 15 不引入 Vector Memory / RAG / 长期记忆等类型。"""
    import pi_agent_core_py as pkg
    exports = set(pkg.__all__)
    forbidden = {
        "VectorMemory", "VectorStore", "Embedding",
        "RAGMemory", "LongTermMemory",
        "Retriever", "KnowledgeBase",
    }
    assert not (exports & forbidden), f"Step 15 不应导出：{exports & forbidden}"


# ============================================================================
# 额外：compact_context 未 attach session 时也能跑
# ============================================================================


@pytest.mark.asyncio
async def test_compact_context_without_session():
    fake = FakeClient([_text_script(str(i)) for i in range(20)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)  # 不 attach session

    for i in range(10):
        await harness.run_prompt(f"turn-{i}")

    config = CompactionConfig(keep_last_n_messages=4, min_messages_to_compact=5)
    result = await harness.compact_context(config=config)

    assert result.applied is True
    assert agent.state.messages[0].role == "summary"


# ============================================================================
# 额外：compact_session 未 attach session 抛错
# ============================================================================


@pytest.mark.asyncio
async def test_compact_session_without_session_raises():
    fake = FakeClient([])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    with pytest.raises(RuntimeError, match="未 attach session"):
        await harness.compact_session()


# ============================================================================
# 额外：compaction 保留的最近 N 条按原序
# ============================================================================


@pytest.mark.asyncio
async def test_compaction_retains_recent_in_order():
    msgs = []
    for i in range(10):
        msgs.append(UserMessage(content=[TextContent(text=f"u{i}")]))

    config = CompactionConfig(keep_last_n_messages=3, min_messages_to_compact=5)
    result = await compact_messages(msgs, config=config)

    retained_texts = [m["content"][0]["text"] for m in result.retained_messages]
    assert retained_texts == ["u7", "u8", "u9"]


# ============================================================================
# 额外：CompactionResult.to_dict / BranchSummary.to_json
# ============================================================================


@pytest.mark.asyncio
async def test_compaction_result_to_dict():
    msgs = _make_messages(10)
    result = await compact_messages(
        msgs,
        config=CompactionConfig(keep_last_n_messages=2, min_messages_to_compact=5),
    )
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["applied"] is True
    assert d["summary_message"]["role"] == "summary"


def test_branch_summary_to_json():
    summary = BranchSummary(
        id="bs-1",
        branch_id="main",
        summary="test summary",
    )
    js = summary.to_json(indent=2)
    assert '"id": "bs-1"' in js
    assert '"summary": "test summary"' in js


# ============================================================================
# 额外：create_branch_summary 函数（不通过 harness）
# ============================================================================


@pytest.mark.asyncio
async def test_create_branch_summary_function_with_messages():
    msgs = _make_messages(5)
    summary = await create_branch_summary(
        messages=msgs,
        config=BranchSummaryConfig(branch_id="custom"),
    )
    assert summary.branch_id == "custom"
    assert summary.source_session_id is None
    assert summary.source_message_count == 5
    assert "Conversation Summary" in summary.summary or "User Requests" in summary.summary


# ============================================================================
# 额外：default generator 长度限制
# ============================================================================


@pytest.mark.asyncio
async def test_default_generator_max_chars():
    from pi_agent_core_py import CompactionInput

    # 构造 100 条长 message
    msgs_dicts = []
    for i in range(100):
        msgs_dicts.append({
            "role": "user",
            "content": [{"type": "text", "text": f"用户消息 {i} " * 50}],
            "timestamp": 0,
        })

    inp = CompactionInput(
        messages_to_compact=msgs_dicts,
        retained_messages=[],
        config={"max_summary_chars": 500, "include_tool_results": True},
    )
    text = default_summary_generator(inp)
    assert len(text) <= 500


@pytest.mark.asyncio
async def test_compaction_config_validation():
    msgs = _make_messages(5)

    with pytest.raises(ValueError):
        await compact_messages(
            msgs,
            config=CompactionConfig(keep_last_n_messages=-1, min_messages_to_compact=1),
        )

    with pytest.raises(ValueError):
        await compact_messages(
            msgs,
            config=CompactionConfig(min_messages_to_compact=0),
        )

    with pytest.raises(ValueError):
        await compact_messages(
            msgs,
            config=CompactionConfig(max_summary_chars=0, min_messages_to_compact=1),
        )


# ============================================================================
# 额外：BranchSummaryConfig.include_messages / include_snapshots 生效
# ============================================================================


@pytest.mark.asyncio
async def test_branch_summary_config_include_messages_false():
    """include_messages=False → source_message_count == 0；摘要 Scope 显示 0。"""
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    for i in range(3):
        await harness.run_prompt(f"turn-{i}")

    summary = await harness.create_branch_summary(
        config=BranchSummaryConfig(
            branch_id="b",
            include_messages=False,  # 不纳入 messages
            include_snapshots=True,
        ),
    )

    # 即使 session 有 messages，include_messages=False 时 source_message_count = 0
    assert summary.source_message_count == 0
    # "Compacted messages: 0" 应出现在摘要正文
    assert "Compacted messages: 0" in summary.summary


@pytest.mark.asyncio
async def test_branch_summary_config_include_snapshots_false():
    """include_snapshots=False → source_snapshot_ids == []；source_turn_count == 0。"""
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    for i in range(3):
        await harness.run_prompt(f"turn-{i}")

    summary = await harness.create_branch_summary(
        config=BranchSummaryConfig(
            branch_id="b",
            include_messages=True,
            include_snapshots=False,  # 不纳入 snapshots
        ),
    )

    # messages 仍纳入
    assert summary.source_message_count > 0
    # 但 snapshots 不纳入
    assert summary.source_snapshot_ids == []
    assert summary.source_turn_count == 0


@pytest.mark.asyncio
async def test_branch_summary_config_both_false():
    """两个都关：source 都是 0；summary 仍能生成（空 Scope）。"""
    fake = FakeClient([_text_script(str(i)) for i in range(10)])
    agent = Agent(system_prompt="x", client=fake)
    harness = AgentHarness(agent)

    for i in range(2):
        await harness.run_prompt(f"turn-{i}")

    summary = await harness.create_branch_summary(
        config=BranchSummaryConfig(
            branch_id="empty",
            include_messages=False,
            include_snapshots=False,
        ),
    )

    assert summary.source_message_count == 0
    assert summary.source_turn_count == 0
    assert summary.source_snapshot_ids == []
    # summary 仍非空（至少含 section headers）
    assert "Conversation Summary" in summary.summary


# ============================================================================
# 额外：SummaryGenerator 类型别名存在并可导入
# ============================================================================


def test_summary_generator_type_alias_exists():
    """SummaryGenerator 类型别名必须存在并能从顶层导入。"""
    import typing

    from pi_agent_core_py import SummaryGenerator

    # 用 typing.get_type_hints 或 get_origin 检验
    assert SummaryGenerator is not None
    # 它应该是 Callable 的 subtype / alias
    origin = typing.get_origin(SummaryGenerator)
    assert origin is typing.Callable or origin is not None


def test_summary_generator_accepts_sync_and_async():
    """sync 和 async generator 都能用作 SummaryGenerator（运行时检验）。"""
    import inspect

    from pi_agent_core_py import CompactionInput

    def sync_gen(input: CompactionInput) -> str:
        return "sync"

    async def async_gen(input: CompactionInput) -> str:
        return "async"

    # 验证 inspect.isawaitable 行为一致
    sync_result = sync_gen(CompactionInput())
    assert not inspect.isawaitable(sync_result)

    coro = async_gen(CompactionInput())
    assert inspect.isawaitable(coro)
    coro.close()  # 避免未 await 警告
