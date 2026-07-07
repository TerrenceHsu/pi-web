"""Step 16 — Tool Validation 单元测试。

覆盖：
- schema 校验通过
- required 字段缺失失败
- 类型错误失败
- arguments 不是 dict 失败
- 空 object schema 允许空参数
- 空 schema 视为无约束
- 错误信息含 tool_name + path
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    ToolArgumentValidationError,
    validate_tool_arguments,
)

# ============================================================================
# 通过路径
# ============================================================================


def test_validate_pass_simple() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    validate_tool_arguments(tool_name="t", schema=schema, arguments={"name": "ok"})


def test_validate_pass_empty_object_schema() -> None:
    """无 properties / 无 required 的 object schema 允许 {}。"""
    validate_tool_arguments(
        tool_name="t", schema={"type": "object"}, arguments={},
    )


def test_validate_pass_empty_schema_treats_as_unconstrained() -> None:
    """空 schema（None / {}）视为无约束。"""
    validate_tool_arguments(tool_name="t", schema=None, arguments={"any": "thing"})
    validate_tool_arguments(tool_name="t", schema={}, arguments={})


def test_validate_pass_extra_properties_allowed() -> None:
    """默认无 additionalProperties=false 时，多传字段不算错。"""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    validate_tool_arguments(
        tool_name="t", schema=schema, arguments={"a": "x", "extra": 1},
    )


# ============================================================================
# 失败路径
# ============================================================================


def test_validate_fail_missing_required() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(tool_name="my_tool", schema=schema, arguments={})
    msg = str(exc_info.value)
    assert "my_tool" in msg
    assert "name" in msg


def test_validate_fail_wrong_type() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="t", schema=schema, arguments={"count": "not-int"},
        )
    assert "count" in str(exc_info.value)


def test_validate_fail_arguments_not_dict() -> None:
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="t",
            schema={"type": "object"},
            arguments="not a dict",  # type: ignore[arg-type]
        )
    assert "must be a dict" in str(exc_info.value)


def test_validate_fail_arguments_list() -> None:
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="t",
            schema={"type": "object"},
            arguments=[1, 2, 3],  # type: ignore[arg-type]
        )
    assert "must be a dict" in str(exc_info.value)


def test_validate_fail_enum_violation() -> None:
    schema = {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": ["red", "green"]}},
        "required": ["color"],
    }
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="t", schema=schema, arguments={"color": "blue"},
        )
    assert "color" in str(exc_info.value)


def test_validate_fail_message_contains_tool_name_and_path() -> None:
    """错误信息必须含 tool_name 与 schema path（便于 UI 展示）。"""
    schema = {
        "type": "object",
        "properties": {
            "addr": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        "required": ["addr"],
    }
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="user_tool",
            schema=schema,
            arguments={"addr": {}},
        )
    msg = str(exc_info.value)
    assert "user_tool" in msg
    assert "addr" in msg or "city" in msg


# ============================================================================
# 边界
# ============================================================================


def test_validate_pass_minimum_maximum() -> None:
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    }
    validate_tool_arguments(tool_name="t", schema=schema, arguments={"n": 5})


def test_validate_fail_below_minimum() -> None:
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    }
    with pytest.raises(ToolArgumentValidationError):
        validate_tool_arguments(tool_name="t", schema=schema, arguments={"n": 0})
