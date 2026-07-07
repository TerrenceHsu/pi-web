"""Smoke test 1: 核心模块 import 不产生循环。

显式 import 每个公开模块，验证：
- 无循环 import
- 旧 import 路径（pi_agent_core_py.X）仍可用
- __all__ 与实际 re-export 一致
"""
from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "pi_agent_core_py.agent",
        "pi_agent_core_py.context",
        "pi_agent_core_py.events",
        "pi_agent_core_py.harness",
        "pi_agent_core_py.hooks",
        "pi_agent_core_py.llm_messages",
        "pi_agent_core_py.loop",
        "pi_agent_core_py.messages",
        "pi_agent_core_py.model_client",
        "pi_agent_core_py.session",
        "pi_agent_core_py.session_sync",
        "pi_agent_core_py.skill_loader",
        "pi_agent_core_py.skills",
        "pi_agent_core_py.snapshot",
        "pi_agent_core_py.stream_events",
        "pi_agent_core_py.tool_validation",
        "pi_agent_core_py.providers",
        "pi_agent_core_py.policy",
        "pi_agent_core_py.mcp",
        "pi_agent_core_py.tools",
        "pi_agent_core_py.compaction",
    ],
)
def test_module_importable(module_name: str) -> None:
    """每个核心模块都能被独立 import。"""
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_top_level_reexports_present() -> None:
    """顶层 __init__ 暴露的核心符号都能取到。"""
    import pi_agent_core_py as pkg

    for symbol in (
        "Agent",
        "AgentHarness",
        "AgentState",
        "AgentStatus",
        "run_event_loop",
        "run_min_loop",
        "Message",
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "TurnSnapshot",
        "SessionMemory",
        "FakeClient",
        "ModelClient",
        "GLMClient",
        "ProviderAdapter",
        "ProviderRequest",
        "AnthropicCompatAdapter",
        "GLMProviderAdapter",
        "ToolPermissionPolicy",
        "DefaultToolPermissionPolicy",
        "InMemoryToolPermissionAuditLog",
        "MCPRegistry",
        "SkillRegistry",
        "SkillFileLoader",
    ):
        assert hasattr(pkg, symbol), f"pi_agent_core_py 缺少 {symbol}"


def test_legacy_stream_event_import() -> None:
    """旧代码用 from pi_agent_core_py.model_client import StreamEvent —— 必须保持兼容。"""
    from pi_agent_core_py.model_client import (
        DoneEvent,
        ErrorEvent,
        StreamEvent,
        TextDeltaEvent,
        ToolCallEvent,
    )

    assert issubclass(TextDeltaEvent, StreamEvent)
    assert issubclass(DoneEvent, StreamEvent)
    assert issubclass(ErrorEvent, StreamEvent)
    assert issubclass(ToolCallEvent, StreamEvent)


def test_no_duplicate_reexport_collision() -> None:
    """__all__ 不应有重复条目。"""
    import pi_agent_core_py as pkg

    all_names = pkg.__all__
    assert len(all_names) == len(set(all_names)), "顶层 __all__ 有重复"
