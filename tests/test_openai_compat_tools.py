"""to_openai_tools 转换测试（M1-1 §十五.Tools）.

覆盖：
- 空 tools 列表
- 单 tool
- JSON Schema 保留
- 多 tools 顺序稳定
- 不加 strict=true
- 不发送旧版 functions
- tool 缺 name 抛 ProviderProtocolError
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.providers.errors import ProviderProtocolError
from pi_agent_core_py.providers.openai_compat import to_openai_tools
from pi_agent_core_py.tools import ToolDef


def _tool(
    *,
    name: str = "echo",
    label: str = "Echo",
    description: str = "Echoes text",
    parameters: dict | None = None,
) -> ToolDef:
    return ToolDef(
        name=name,
        label=label,
        description=description,
        parameters=parameters if parameters is not None else {"type": "object", "properties": {}},
    )


# ============================================================================
# 空 tools
# ============================================================================


def test_empty_tools_returns_empty_list() -> None:
    """Adapter 在 tools=[] 时省略字段；本函数仍返回 []."""
    assert to_openai_tools([]) == []


# ============================================================================
# 单 tool
# ============================================================================


def test_single_tool_emits_function_wrapper() -> None:
    out = to_openai_tools([_tool()])
    assert len(out) == 1
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "echo"
    assert out[0]["function"]["description"] == "Echoes text"


# ============================================================================
# JSON Schema 保留
# ============================================================================


def test_json_schema_preserved() -> None:
    schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to echo"},
        },
        "required": ["text"],
    }
    out = to_openai_tools([_tool(parameters=schema)])
    assert out[0]["function"]["parameters"] == schema


def test_default_schema_when_parameters_empty() -> None:
    """parameters={} 是 falsy——按 anthropic_compat 风格补全为 default object schema."""
    out = to_openai_tools([_tool(parameters={})])
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_default_schema_when_parameters_none_via_default() -> None:
    """ToolDef default parameters 为 empty object schema——保留不变."""
    t = ToolDef(name="x", label="X", description="x")
    out = to_openai_tools([t])
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ============================================================================
# 多 tools 顺序稳定
# ============================================================================


def test_multiple_tools_preserve_order() -> None:
    tools = [
        _tool(name="alpha"),
        _tool(name="beta"),
        _tool(name="gamma"),
    ]
    out = to_openai_tools(tools)
    assert [t["function"]["name"] for t in out] == ["alpha", "beta", "gamma"]


# ============================================================================
# 不加 strict=true
# ============================================================================


def test_no_strict_field_added() -> None:
    out = to_openai_tools([_tool()])
    assert "strict" not in out[0]["function"]


# ============================================================================
# 不发送旧版 functions 字段
# ============================================================================


def test_no_legacy_functions_field() -> None:
    """新 OpenAI tools 结构——不含旧 functions 字段."""
    out = to_openai_tools([_tool()])
    assert "functions" not in out[0]


# ============================================================================
# 错误：缺 name
# ============================================================================


def test_tool_missing_name_raises_protocol_error() -> None:
    t = ToolDef(name="", label="X", description="x")
    with pytest.raises(ProviderProtocolError):
        to_openai_tools([t])
