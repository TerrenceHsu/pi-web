"""工具参数 JSON Schema 校验（Step 16 新增）。

在 `_execute_tool_with_hooks` 中接入：`registry.get(name)` → `before_tool_call`
→ 改名重查 → **`validate_tool_arguments`** → `tool.execute(...)` → `after_tool_call`。

校验失败时：
- 抛 `ToolArgumentValidationError`，由 loop 捕获并包成 `ToolResult(is_error=True)`。
- details 至少包含：
    {"error_type": "ToolArgumentValidationError",
     "validation": {"tool_name": ..., "message": ...}}
- 不调用 `tool.execute`，不调用 `after_tool_call`（工具没成功执行）。

约束：
- 空 schema（`{}` / `None` / 非 dict）视为无约束，任何 arguments 都通过。
- `arguments` 必须是 dict；否则失败。
- 用 `jsonschema` 包（Draft7）。
"""
from __future__ import annotations

from typing import Any

import jsonschema


class ToolArgumentValidationError(Exception):
    """工具参数 JSON Schema 校验失败。

    `message` 已经包含 tool_name、schema path、原始 schema 错误描述，便于
    在 ToolResult.details.validation.message 里直接展示。
    """


def validate_tool_arguments(
    *,
    tool_name: str,
    schema: dict[str, Any] | None,
    arguments: Any,
) -> None:
    """按 schema 校验 arguments；不通过抛 ToolArgumentValidationError，通过返回 None。

    - 空 schema（None / {} / 非 dict）→ 视为无约束，直接通过
    - arguments 不是 dict → 失败
    - schema 本身非法（jsonschema.SchemaError）→ 包成 ToolArgumentValidationError，
      避免 loop 兜底只捕获 ToolArgumentValidationError 时被穿透成普通 Exception
    - 多条错误时，按 path 排序后取第一条抛出（避免错误信息过长）
    """
    if not isinstance(arguments, dict):
        raise ToolArgumentValidationError(
            f"tool '{tool_name}' arguments must be a dict, "
            f"got {type(arguments).__name__}"
        )

    # 空 schema 视为无约束（ToolDef 默认 schema 是 {"type":"object","properties":{}}，
    # 这里也走 Draft7 校验，等价通过）
    if not isinstance(schema, dict) or not schema:
        return

    # 先检查 schema 本身合法——非法 schema 抛 SchemaError，包成统一错误
    try:
        jsonschema.Draft7Validator.check_schema(schema)
        validator = jsonschema.Draft7Validator(schema)
    except jsonschema.SchemaError as e:
        raise ToolArgumentValidationError(
            f"tool '{tool_name}' has invalid JSON Schema: {e.message}"
        ) from e

    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if not errors:
        return

    first = errors[0]
    path_str = "/".join(str(p) for p in first.absolute_path) or "<root>"
    raise ToolArgumentValidationError(
        f"tool '{tool_name}' argument validation failed at {path_str}: {first.message}"
    )


__all__ = [
    "ToolArgumentValidationError",
    "validate_tool_arguments",
]
