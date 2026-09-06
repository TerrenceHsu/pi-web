"""Deterministic, provider-neutral routing for the coding-agent product."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

IntentRoute = Literal["read_only", "coding", "knowledge"]
IntentMode = Literal["auto", "read_only", "coding", "knowledge"]
IntentSource = Literal["explicit", "rule", "fallback", "session_binding"]

READ_ONLY_SYSTEM_PROMPT = """# Read-only request

This turn is restricted to inspection and explanation. Use only the read-only tools
provided for this turn. Do not claim to have changed files, executed code, or completed
an implementation. If a mutation is required, explain it and ask the user to explicitly
request implementation or enable Code mode.
"""

_STRONG_READ_ONLY = re.compile(
    r"(?:不要|不需要|无需|别|禁止)(?:修改|改动|写入|写代码|执行|运行|提交|实现)"
    r"|(?:只|仅)(?:查看|检查|分析|审核|解释|说明|介绍|评估|读取)"
    r"|(?:do\s+not|don't|dont|without)\s+(?:change|modify|write|run|execute|commit)"
    r"|(?:read[ -]?only|review only|analy[sz]e only)",
    re.IGNORECASE,
)
_QUESTION_OR_PLAN = re.compile(
    r"(?:如何|怎么|怎样|为什么|是什么|有什么|哪些|能否|是否|介绍|解释|说明)"
    r"|(?:下一步|下一项)(?:做什么|是什么|怎么做|如何做)"
    r"|(?:先|首先)?(?:制定|写|给出|设计)(?:一个|整体)?(?:方案|计划)"
    r"|(?:查看|检查|审核).*(?:是否|进度|状态|完成|完整|差距|问题)"
    r"|\b(?:how|what|why|can|could|should|explain|describe|review|inspect)\b",
    re.IGNORECASE,
)
_CODING_ARTIFACT_REQUEST = re.compile(
    r"(?:写|生成|产出|编写|创建)"
    r"[^。！？\n]{0,80}"
    r"(?:源代码|源码|代码|程序|脚本|代码文件|(?:python|py|js|ts)\s*文件)",
    re.IGNORECASE,
)
_CODING_ACTION = re.compile(
    r"(?:开始|继续|进行|执行|完成|直接)?(?:实现|修复|修改|改造|新增|添加|删除|移除|"
    r"重构|编写|创建|接入|更新|迁移|补齐|开发|编码|提交|发布|部署|运行测试|复跑)"
    r"|(?:开始|继续|进行|执行)(?:下一步|下一项|阶段)"
    r"|^(?:开始|继续|下一步|下一项|开始执行|继续执行|开始\s*[A-Za-z]?\d+[A-Za-z]?)$"
    r"|\b(?:implement|fix|modify|change|add|remove|delete|refactor|write|create|build|"
    r"update|migrate|commit|deploy|execute|continue)\b",
    re.IGNORECASE,
)

_LOCAL_READ_ONLY_TOOLS = frozenset(
    {
        "list_files",
        "view_file",
        "analyze_data",
        "web_search",
        "search_knowledge",
        "search_session_history",
        "read_session_history",
        "read_tool_output",
    }
)
_MCP_READ_PREFIXES = (
    "get",
    "list",
    "read",
    "search",
    "find",
    "fetch",
    "query",
    "browse",
    "open",
    "view",
    "inspect",
    "show",
    "status",
)
_MUTATION_WORDS = frozenset(
    {
        "write",
        "create",
        "update",
        "delete",
        "remove",
        "patch",
        "move",
        "rename",
        "execute",
        "exec",
        "shell",
        "terminal",
        "command",
        "post",
        "put",
        "insert",
        "upsert",
        "sql",
        "database",
        "upload",
        "send",
    }
)


@dataclass(frozen=True, slots=True)
class IntentDecision:
    """Stable decision that can be safely returned in API audit metadata."""

    route: IntentRoute
    confidence: float
    source: IntentSource
    reason_code: str
    explicit: bool = False

    def public(self) -> dict[str, object]:
        return {
            "route": self.route,
            "confidence": self.confidence,
            "source": self.source,
            "reason_code": self.reason_code,
            "explicit": self.explicit,
        }


def parse_intent_mode(value: object) -> IntentMode:
    """Validate an API intent override without importing the web layer."""

    if value is None:
        return "auto"
    if not isinstance(value, str) or value not in {
        "auto",
        "read_only",
        "coding",
        "knowledge",
    }:
        raise ValueError("intent_mode must be auto, read_only, coding, or knowledge")
    return cast(IntentMode, value)


def route_intent(
    text: str,
    *,
    knowledge_bound: bool = False,
    intent_mode: IntentMode = "auto",
    legacy_coding_mode: bool = False,
) -> IntentDecision:
    """Resolve the route; durable Knowledge binding always owns its Session."""

    if knowledge_bound:
        return IntentDecision(
            route="knowledge",
            confidence=1.0,
            source="session_binding",
            reason_code="knowledge_session_binding",
            explicit=intent_mode == "knowledge",
        )
    if legacy_coding_mode or intent_mode == "coding":
        return IntentDecision(
            route="coding",
            confidence=1.0,
            source="explicit",
            reason_code="explicit_coding_mode",
            explicit=True,
        )
    if intent_mode == "read_only":
        return IntentDecision(
            route="read_only",
            confidence=1.0,
            source="explicit",
            reason_code="explicit_read_only_mode",
            explicit=True,
        )

    normalized = " ".join(text.strip().split())
    if _STRONG_READ_ONLY.search(normalized):
        return IntentDecision(
            route="read_only",
            confidence=0.98,
            source="rule",
            reason_code="read_only_constraint",
        )
    if _QUESTION_OR_PLAN.search(normalized):
        return IntentDecision(
            route="read_only",
            confidence=0.9,
            source="rule",
            reason_code="analysis_or_plan_request",
        )
    if _CODING_ARTIFACT_REQUEST.search(normalized):
        return IntentDecision(
            route="coding",
            confidence=0.94,
            source="rule",
            reason_code="coding_artifact_request",
        )
    if _CODING_ACTION.search(normalized):
        return IntentDecision(
            route="coding",
            confidence=0.88,
            source="rule",
            reason_code="coding_action_request",
        )
    return IntentDecision(
        route="read_only",
        confidence=0.35,
        source="fallback",
        reason_code="safe_default",
    )


def is_read_only_tool_name(name: str) -> bool:
    """Conservatively admit local readers and clearly read-only MCP tools."""

    if name in _LOCAL_READ_ONLY_TOOLS:
        return True
    if not name.startswith("mcp__"):
        return False
    parts = name.split("__", 2)
    if len(parts) != 3:
        return False
    raw_name = parts[2].lower().replace("-", "_")
    words = frozenset(part for part in raw_name.split("_") if part)
    if words & _MUTATION_WORDS:
        return False
    return raw_name.startswith(_MCP_READ_PREFIXES)
