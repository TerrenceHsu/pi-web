"""JSON-safe serializers —— Step 20 Web App 序列化层。

把 runtime 对象（AgentMessage / AgentEvent / TurnSnapshot / SessionMemory /
Skill / MCPServerState / ToolPermissionAuditRecord）转成 JSON-safe dict，
供 FastAPI response / SSE 推送 / TraceEventBuffer 使用。

设计要点：

- **不返回 live object**（MCPClient / MCPTransport / MCPAgentTool）
- **不返回完整 prompt raw / 巨型 tool schema**——避免 SSE / response 膨胀
- 默认 **不返回 Skill.prompt 正文**——`include_prompt=True` 才返回
- `to_json_safe` 是兜底——任何不认识的对象走 `repr()`

```text
runtime 对象         →  serializer  →  JSON-safe dict  →  FastAPI JSONResponse
                                  ↘                  ↘  TraceEventBuffer.append
                                                       ↘  SSE data: {...}
```
"""
from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

# ============================================================================
# to_json_safe —— 兜底序列化器
# ============================================================================


def to_json_safe(obj: Any) -> Any:
    """把任意对象转成 JSON-safe 结构。

    分支：

    - None / bool / int / float / str → 原样返回
    - BaseModel → model_dump(mode="json")（递归处理嵌套）
    - dict → 递归处理 values
    - list / tuple → 递归处理元素（tuple 也转 list）
    - Exception → {"type": "...", "message": "..."}
    - 其它对象 → repr(obj)（字符串）

    注意：

    - 不调用 json.dumps——返回的是 Python 数据结构，由 FastAPI 的 JSONResponse
      统一 encode
    - 对不支持的对象（如 lambda、文件句柄）fallback 到 repr，**不抛错**
    - 自循环引用不处理（Pydantic model_dump 不应产生；调用方自负）
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(item) for item in obj]
    if isinstance(obj, Exception):
        return {
            "type": type(obj).__name__,
            "message": str(obj),
        }
    # fallback——任何不认识的对象转 repr 字符串
    try:
        return repr(obj)
    except Exception:
        return f"<unrepresentable {type(obj).__name__}>"


# ============================================================================
# Message / Event 序列化
# ============================================================================


def serialize_message(message: Any) -> dict[str, Any]:
    """AgentMessage → JSON-safe dict。

    `message` 应该是 UserMessage / AssistantMessage / ToolResultMessage /
    SummaryMessage / CustomMessage 中的一种（都是 Pydantic BaseModel）。
    非 BaseModel 输入直接走 to_json_safe fallback。
    """
    if isinstance(message, BaseModel):
        return message.model_dump(mode="json")
    return cast("dict[str, Any]", to_json_safe(message))


def serialize_event(event: Any) -> dict[str, Any]:
    """AgentEvent → JSON-safe dict。

    所有 AgentEvent 都是 Pydantic BaseModel——走 model_dump；
    非 BaseModel fallback 到 to_json_safe。
    """
    if isinstance(event, BaseModel):
        return event.model_dump(mode="json")
    return cast("dict[str, Any]", to_json_safe(event))


# ============================================================================
# Snapshot 序列化
# ============================================================================


def serialize_snapshot_summary(snapshot: Any) -> dict[str, Any]:
    """TurnSnapshot → 紧凑 summary（不带 messages / events 详情）。

    用于 /api/snapshots 列表——避免一次返回几十个 snapshot 的全部 messages。
    想看详情调 /api/snapshots/{index} → serialize_snapshot_full。
    """
    if isinstance(snapshot, BaseModel):
        return {
            "id": getattr(snapshot, "id", None),
            "request_id": getattr(snapshot, "request_id", None),
            "request_type": getattr(snapshot, "request_type", None),
            "status": getattr(snapshot, "status", None),
            "error": getattr(snapshot, "error", None),
            "started_at": getattr(snapshot, "started_at", None),
            "ended_at": getattr(snapshot, "ended_at", None),
            "duration_ms": getattr(snapshot, "duration_ms", None),
            "messages_before_count": len(getattr(snapshot, "messages_before", []) or []),
            "messages_after_count": len(getattr(snapshot, "messages_after", []) or []),
            "events_count": len(getattr(snapshot, "events", []) or []),
            "tool_calls_count": len(getattr(snapshot, "tool_calls", []) or []),
            "tool_results_count": len(getattr(snapshot, "tool_results", []) or []),
            "metadata": to_json_safe(getattr(snapshot, "metadata", {}) or {}),
        }
    return cast("dict[str, Any]", to_json_safe(snapshot))


def serialize_snapshot_full(snapshot: Any) -> dict[str, Any]:
    """TurnSnapshot → 完整 dict（含 messages / events / tool_calls / results）。

    用于 /api/snapshots/{index} 详情页。TurnSnapshot.to_dict() 已经做了
    JSON-safe 转换（model_dump mode=json）；这里只是统一入口。
    """
    if isinstance(snapshot, BaseModel):
        # 优先用 snapshot.to_dict()（TurnSnapshot 自带这个方法）
        to_dict = getattr(snapshot, "to_dict", None)
        if callable(to_dict):
            return cast("dict[str, Any]", to_dict())
        return snapshot.model_dump(mode="json")
    return cast("dict[str, Any]", to_json_safe(snapshot))


# ============================================================================
# Session 序列化
# ============================================================================


def serialize_session(session: Any) -> dict[str, Any]:
    """SessionMemory → JSON-safe summary。

    返回：

        {
          "attached": True,
          "id": "...",
          "title": "...",
          "created_at": ...,
          "updated_at": ...,
          "turn_count": N,
          "message_count": M,
          "snapshot_count": K,
          "metadata": {...},
          "messages": [...],   # 当前 session 累计 messages
        }

    `session` 为 None → {"attached": False}。
    """
    if session is None:
        return {"attached": False}

    state = getattr(session, "state", None)
    if state is None:
        return {"attached": False}

    messages = list(getattr(state, "messages", []) or [])
    snapshots = list(getattr(state, "snapshots", []) or [])
    return {
        "attached": True,
        "id": getattr(state, "id", None),
        "title": getattr(state, "title", None),
        "created_at": getattr(state, "created_at", None),
        "updated_at": getattr(state, "updated_at", None),
        "turn_count": getattr(state, "turn_count", 0),
        "message_count": len(messages),
        "snapshot_count": len(snapshots),
        "metadata": to_json_safe(getattr(state, "metadata", {}) or {}),
        "messages": to_json_safe(messages),
    }


# ============================================================================
# MCP server state 序列化
# ============================================================================


def serialize_mcp_server_state(state: Any) -> dict[str, Any]:
    """MCPServerState → JSON-safe dict。

    包含 name / connected / tool_count / last_error / metadata。
    **不**包含 MCPClient / MCPTransport / live tool 对象。
    """
    if isinstance(state, BaseModel):
        return {
            "name": getattr(state, "name", None),
            "connected": getattr(state, "connected", False),
            "tool_count": getattr(state, "tool_count", 0),
            "last_error": getattr(state, "last_error", None),
            "metadata": to_json_safe(getattr(state, "metadata", {}) or {}),
        }
    return cast("dict[str, Any]", to_json_safe(state))


# ============================================================================
# Skill 序列化
# ============================================================================


def serialize_skill(
    skill: Any,
    *,
    include_prompt: bool = False,
) -> dict[str, Any]:
    """Skill → JSON-safe dict。

    默认**不返回 Skill.prompt 正文**——避免列表 API 膨胀；详情页传
    include_prompt=True 才返回。

    返回：

        {
          "name": ...,
          "description": ...,
          "status": "enabled" | "disabled",
          "priority": N,
          "tags": [...],
          "tool_names": [...],
          "metadata": {...},
          "prompt": "..." (only if include_prompt=True)
        }
    """
    if not isinstance(skill, BaseModel):
        return cast("dict[str, Any]", to_json_safe(skill))

    out: dict[str, Any] = {
        "name": getattr(skill, "name", None),
        "description": getattr(skill, "description", ""),
        "status": getattr(skill, "status", "enabled"),
        "priority": getattr(skill, "priority", 0),
        "tags": list(getattr(skill, "tags", []) or []),
        "tool_names": list(getattr(skill, "tool_names", []) or []),
        "metadata": to_json_safe(getattr(skill, "metadata", {}) or {}),
    }
    if include_prompt:
        # 渲染 prompt 正文——str 直接用；PromptTemplate 走 render
        prompt = getattr(skill, "prompt", None)
        if isinstance(prompt, str):
            out["prompt"] = prompt
        elif isinstance(prompt, BaseModel):
            # PromptTemplate——返回 template + variables，不调 render（render 需要 vars）
            out["prompt"] = {
                "name": getattr(prompt, "name", None),
                "template": getattr(prompt, "template", ""),
                "variables": list(getattr(prompt, "variables", []) or []),
            }
        else:
            out["prompt"] = to_json_safe(prompt)
    return out


# ============================================================================
# Policy audit record 序列化
# ============================================================================


def serialize_policy_audit_record(record: Any) -> dict[str, Any]:
    """ToolPermissionAuditRecord → JSON-safe dict。"""
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json")
    return cast("dict[str, Any]", to_json_safe(record))


__all__ = [
    "to_json_safe",
    "serialize_message",
    "serialize_event",
    "serialize_snapshot_summary",
    "serialize_snapshot_full",
    "serialize_session",
    "serialize_mcp_server_state",
    "serialize_skill",
    "serialize_policy_audit_record",
]
