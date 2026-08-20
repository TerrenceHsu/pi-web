"""Compaction / Branch Summary（Step 15）。

Compaction 把较长的历史 messages 压缩成 SummaryMessage，然后默认保留最近若干个
完整 turn。BranchSummary 基于当前 session / snapshots 生成分支级旁路摘要。

```text
Compaction：
    messages_before:
        [很多历史消息..., 最近消息...]
    compact 后：
        [SummaryMessage(...历史摘要...), 最近 N 条原始消息...]

BranchSummary：
    基于 snapshots / messages / metadata 生成分支级摘要，
    只记录到 session.branch_summaries，不改变 Agent 执行上下文。
```

设计要点：
- Compaction 是**上下文管理层**，不改变 Agent 执行内核
- SummaryMessage 是普通上下文消息，可被 convert_to_llm 看见
- Compaction **只压缩 messages**，不删除 snapshots
- BranchSummary 是**旁路记录**，不改变 messages
- 默认摘要器是**确定性 extractive summary**——不依赖外部 LLM / 联网
- 可通过 summary_generator 注入更强摘要器
- token 目标只决定保留多少个完整 turn，绝不拆散 tool-call / tool-result
- 连续 compaction 把前一份摘要作为显式 previous_summary 折叠
- 可选重试只覆盖瞬时错误，且每次尝试使用同一份准备输入

Step 15 **不做**：Vector Memory / RAG / Long-term Memory / 自动后台压缩 / 数据库。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from .context import convert_to_llm
from .context_budget import ContextEstimate, estimate_message_tokens
from .messages import (
    AgentMessage,
    Message,
    SummaryMessage,
    TextContent,
)
from .providers.errors import ProviderRateLimitError, ProviderStreamError
from .snapshot import RequestSnapshot


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================================
# CompactionConfig
# ============================================================================


class CompactionConfig(BaseModel):
    """Compaction 行为配置。

    字段：
      boundary_mode           默认 turn；message 仅作为显式旧兼容模式
      keep_last_n_turns       turn 模式按数量保留最近完整 turn
      keep_recent_tokens      可选 token 目标；设置后优先于 turn 数量
      min_messages_to_compact messages 总数小于该值时不执行 compaction
      include_tool_results    摘要文本是否包含 ToolResultMessage 内容
      include_snapshot_ids    SummaryMessage.metadata 是否记录 snapshot ids
      max_summary_chars       默认摘要生成器输出最大长度
      summary_title           摘要标题（写入 metadata.summary_title）
      metadata                配置级 metadata
    """
    keep_last_n_messages: int = 8
    boundary_mode: Literal["message", "turn"] = "turn"
    keep_last_n_turns: int = 4
    keep_recent_tokens: int | None = None
    context_window: int | None = None
    reserved_output_tokens: int = 0
    min_messages_to_compact: int = 12
    include_tool_results: bool = True
    include_snapshot_ids: bool = True
    max_summary_chars: int = 4000
    summary_title: str = "Conversation Summary"
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# CompactionSource
# ============================================================================


class CompactionSource(BaseModel):
    """记录 compaction 来源统计——便于审计 / 诊断。"""
    source_message_count: int
    retained_message_count: int
    compacted_message_count: int

    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_turn_count: int = 0
    retained_turn_count: int = 0
    compacted_turn_count: int = 0

    started_at: int = Field(default_factory=_now_ms)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# CompactionInput / CompactionResult
# ============================================================================


class CompactionInput(BaseModel):
    """传给 summary_generator 的输入。

    字段都是已序列化的 dict（便于跨进程 / 跨服务调用）：
      messages_to_compact  被压缩的 messages（dict 列表）
      retained_messages    保留的最近 messages（dict 列表）
      snapshots            相关 snapshots（dict 列表）
      config               CompactionConfig 序列化后的 dict
      metadata             附加信息
    """
    messages_to_compact: list[dict[str, Any]] = Field(default_factory=list)
    retained_messages: list[dict[str, Any]] = Field(default_factory=list)
    previous_summary: str | None = None
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompactionTokenStats(BaseModel):
    """Token/window audit data produced with the shared preflight estimator."""

    message_tokens_before: int
    message_tokens_after: int
    estimated_input_tokens_before: int
    estimated_input_tokens_after: int
    projected_tokens_before: int
    projected_tokens_after: int
    context_window: int | None = None
    reserved_output_tokens: int = 0
    input_ratio_before: float | None = None
    input_ratio_after: float | None = None
    projected_ratio_before: float | None = None
    projected_ratio_after: float | None = None
    approximate: bool = True
    estimator_version: str = "mixed-char-v1"


class CompactionRetryPolicy(BaseModel):
    """Retry transient summary-generation failures without changing input."""

    max_attempts: int = 1
    initial_delay_ms: int = 250
    backoff_multiplier: float = 2.0
    max_delay_ms: int = 4_000


class CompactionRetryEvent(BaseModel):
    phase: Literal["retry_scheduled", "retry_attempt_start", "retry_finished"]
    attempt: int
    max_attempts: int
    delay_ms: int = 0
    error_type: str | None = None
    succeeded: bool | None = None


class CompactionResult(BaseModel):
    """Compaction 执行结果。

    字段：
      applied              是否真正执行了 compaction
      reason               未执行时的原因（not_enough_messages / nothing_to_compact）
      summary_message      生成的 SummaryMessage（applied=True 时）
      compacted_messages   被压缩掉的旧 messages
      retained_messages    保留的最近 messages
      new_messages         compact 后的新 messages：[summary_message] + retained_messages
      source               compaction 来源统计
      created_at           毫秒时间戳
      metadata             附加信息
    """
    applied: bool
    reason: str | None = None
    summary_message: SummaryMessage | None = None
    compacted_messages: list[dict[str, Any]] = Field(default_factory=list)
    retained_messages: list[dict[str, Any]] = Field(default_factory=list)
    new_messages: list[dict[str, Any]] = Field(default_factory=list)
    source: CompactionSource | None = None
    token_stats: CompactionTokenStats | None = None
    attempts: int = 0
    created_at: int = Field(default_factory=_now_ms)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict（与 BranchSummary.to_dict 一致）。"""
        return self.model_dump(mode="json")


# ============================================================================
# SummaryGenerator 类型
# ============================================================================


#: 摘要生成器——把 CompactionInput 转成摘要文本。
#: 可以是 sync（直接返回 str）或 async（返回 Awaitable[str]）。
#: Step 15 默认实现是 deterministic extractive summary（不依赖外部 LLM）。
SummaryGenerator = Callable[
    [CompactionInput],
    str | Awaitable[str],
]
CompactionRetryCallback = Callable[
    [CompactionRetryEvent],
    None | Awaitable[None],
]
CompactionRetryPredicate = Callable[[BaseException], bool]


# ============================================================================
# default_summary_generator
# ============================================================================


def _extract_text_from_message_dict(m: dict[str, Any]) -> str:
    """从已序列化的 message dict 提取纯文本——用于摘要。"""
    role = m.get("role")
    content = m.get("content") or []
    parts: list[str] = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    if role == "user":
        return f"用户: {' '.join(parts)}".strip()
    if role == "assistant":
        return f"助手: {' '.join(parts)}".strip()
    if role == "toolResult":
        name = m.get("name", "tool")
        return f"工具结果[{name}]: {' '.join(parts)}".strip()
    if role == "summary":
        return f"摘要: {' '.join(parts)}".strip()
    return ""


def default_summary_generator(input: CompactionInput) -> str:
    """默认摘要生成器——确定性 extractive summary。

    特点：
    - **不调用模型** / **不联网** / **不依赖外部服务**
    - 抽取式：从 messages_to_compact 中提取 UserMessage / AssistantMessage /
      ToolResultMessage / SummaryMessage 的文本
    - 控制 total 长度不超过 `config.max_summary_chars`

    输出结构：
    ```text
    # Conversation Summary

    ## Scope
    - Compacted messages: X
    - Retained messages: Y

    ## Key User Requests
    - ...

    ## Key Assistant Outputs
    - ...

    ## Tool Results
    - ...

    ## Current State
    - Recent messages are retained after this summary.
    ```
    """
    cfg_dict = input.config or {}
    max_chars = cfg_dict.get("max_summary_chars", 4000)
    include_tool_results = cfg_dict.get("include_tool_results", True)

    compacted = input.messages_to_compact
    retained_count = len(input.retained_messages)

    user_reqs: list[str] = []
    assistant_outs: list[str] = []
    tool_results: list[str] = []
    summary_texts: list[str] = []

    for m in compacted:
        role = m.get("role")
        text = _extract_text_from_message_dict(m)
        if not text:
            continue
        if role == "user":
            user_reqs.append(text)
        elif role == "assistant":
            assistant_outs.append(text)
        elif role == "toolResult":
            tool_results.append(text)
        elif role == "summary" and input.previous_summary is None:
            summary_texts.append(text)

    sections: list[str] = ["# Conversation Summary"]

    sections.append("")
    sections.append("## Scope")
    sections.append(f"- Compacted messages: {len(compacted)}")
    sections.append(f"- Retained messages: {retained_count}")

    prior_summary = input.previous_summary
    if prior_summary or summary_texts:
        sections.append("")
        sections.append("## Prior Summary")
        if prior_summary:
            sections.append(prior_summary)
        for s in summary_texts:
            sections.append(f"- {s}")

    if user_reqs:
        sections.append("")
        sections.append("## Key User Requests")
        for r in user_reqs:
            sections.append(f"- {r}")

    if assistant_outs:
        sections.append("")
        sections.append("## Key Assistant Outputs")
        for o in assistant_outs:
            sections.append(f"- {o}")

    if include_tool_results and tool_results:
        sections.append("")
        sections.append("## Tool Results")
        for r in tool_results:
            sections.append(f"- {r}")

    sections.append("")
    sections.append("## Current State")
    sections.append("- Recent messages are retained after this summary.")

    text = "\n".join(sections)
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."
    return text


# ============================================================================
# compact_messages
# ============================================================================


def _turn_count(messages: list[Message]) -> int:
    return sum(1 for message in messages if getattr(message, "role", None) == "user")


def _message_token_count(messages: list[Message]) -> int:
    agent_messages = cast(list[AgentMessage], list(messages))
    return estimate_message_tokens(convert_to_llm(agent_messages))


def _split_at_complete_turn(
    messages: list[Message],
    config: CompactionConfig,
) -> tuple[list[Message], list[Message]]:
    user_boundaries = [
        index
        for index, message in enumerate(messages)
        if getattr(message, "role", None) == "user"
    ]
    if not user_boundaries:
        return [], list(messages)

    keep_tokens = config.keep_recent_tokens
    if keep_tokens is not None:
        if keep_tokens == 0:
            return list(messages), []
        retain_from = user_boundaries[-1]
        retained_tokens = _message_token_count(list(messages[retain_from:]))
        # Always keep the latest complete turn, even when that single turn is
        # larger than the target. Splitting it can orphan tool-call/results.
        for position in range(len(user_boundaries) - 2, -1, -1):
            boundary = user_boundaries[position]
            turn_end = user_boundaries[position + 1]
            turn_tokens = _message_token_count(list(messages[boundary:turn_end]))
            if retained_tokens + turn_tokens > keep_tokens:
                break
            retain_from = boundary
            retained_tokens += turn_tokens
        if retain_from == user_boundaries[0]:
            return [], list(messages)
        return list(messages[:retain_from]), list(messages[retain_from:])

    keep_turns = config.keep_last_n_turns
    if keep_turns == 0:
        return list(messages), []
    if len(user_boundaries) <= keep_turns:
        return [], list(messages)
    retain_from = user_boundaries[-keep_turns]
    return list(messages[:retain_from]), list(messages[retain_from:])


def _ratio(tokens: int, context_window: int | None) -> float | None:
    if context_window is None or context_window <= 0:
        return None
    return tokens / context_window


def _token_stats(
    before: list[Message],
    after: list[Message],
    config: CompactionConfig,
    context_estimate: ContextEstimate | None,
) -> CompactionTokenStats:
    message_before = _message_token_count(before)
    message_after = _message_token_count(after)
    if context_estimate is not None:
        fixed_tokens = max(
            0,
            context_estimate.estimated_input_tokens - context_estimate.message_tokens,
        )
        estimated_before = context_estimate.estimated_input_tokens
        projected_before = context_estimate.projected_tokens
        reserve = context_estimate.reserved_output_tokens
        context_window = context_estimate.context_window
        approximate = context_estimate.approximate
        estimator_version = context_estimate.estimator_version
    else:
        fixed_tokens = 0
        estimated_before = message_before
        reserve = config.reserved_output_tokens
        projected_before = estimated_before + reserve
        context_window = config.context_window
        approximate = True
        estimator_version = "mixed-char-v1"
    estimated_after = fixed_tokens + message_after
    projected_after = estimated_after + reserve
    return CompactionTokenStats(
        message_tokens_before=message_before,
        message_tokens_after=message_after,
        estimated_input_tokens_before=estimated_before,
        estimated_input_tokens_after=estimated_after,
        projected_tokens_before=projected_before,
        projected_tokens_after=projected_after,
        context_window=context_window,
        reserved_output_tokens=reserve,
        input_ratio_before=_ratio(estimated_before, context_window),
        input_ratio_after=_ratio(estimated_after, context_window),
        projected_ratio_before=_ratio(projected_before, context_window),
        projected_ratio_after=_ratio(projected_after, context_window),
        approximate=approximate,
        estimator_version=estimator_version,
    )


def _default_retry_predicate(error: BaseException) -> bool:
    return isinstance(
        error,
        (TimeoutError, ConnectionError, ProviderRateLimitError, ProviderStreamError),
    )


async def _notify_retry(
    callback: CompactionRetryCallback | None,
    event: CompactionRetryEvent,
) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def _generate_summary_with_retry(
    generator_input: CompactionInput,
    generator: SummaryGenerator,
    policy: CompactionRetryPolicy,
    predicate: CompactionRetryPredicate,
    callback: CompactionRetryCallback | None,
) -> tuple[str, int]:
    input_snapshot = generator_input.model_dump(mode="python")
    had_retry = False
    for attempt in range(1, policy.max_attempts + 1):
        if attempt > 1:
            await _notify_retry(
                callback,
                CompactionRetryEvent(
                    phase="retry_attempt_start",
                    attempt=attempt,
                    max_attempts=policy.max_attempts,
                ),
            )
        try:
            # A generator may mutate its input. Every attempt gets a fresh deep
            # validation from the same prepared snapshot.
            attempt_input = CompactionInput.model_validate(input_snapshot)
            result_text = generator(attempt_input)
            if inspect.isawaitable(result_text):
                result_text = await result_text
            if not isinstance(result_text, str):
                raise TypeError(
                    "summary_generator 必须返回 str 或 Awaitable[str]；"
                    f"实际类型：{type(result_text).__name__}"
                )
        except Exception as error:
            can_retry = predicate(error) and attempt < policy.max_attempts
            if not can_retry:
                if had_retry:
                    await _notify_retry(
                        callback,
                        CompactionRetryEvent(
                            phase="retry_finished",
                            attempt=attempt,
                            max_attempts=policy.max_attempts,
                            error_type=type(error).__name__,
                            succeeded=False,
                        ),
                    )
                raise
            had_retry = True
            delay = min(
                policy.max_delay_ms,
                round(
                    policy.initial_delay_ms
                    * (policy.backoff_multiplier ** (attempt - 1))
                ),
            )
            await _notify_retry(
                callback,
                CompactionRetryEvent(
                    phase="retry_scheduled",
                    attempt=attempt + 1,
                    max_attempts=policy.max_attempts,
                    delay_ms=delay,
                    error_type=type(error).__name__,
                ),
            )
            if delay > 0:
                await asyncio.sleep(delay / 1000)
        else:
            if had_retry:
                await _notify_retry(
                    callback,
                    CompactionRetryEvent(
                        phase="retry_finished",
                        attempt=attempt,
                        max_attempts=policy.max_attempts,
                        succeeded=True,
                    ),
                )
            return result_text, attempt
    raise AssertionError("unreachable compaction retry state")


async def compact_messages(
    messages: list[Message],
    *,
    snapshots: list[RequestSnapshot] | None = None,
    config: CompactionConfig | None = None,
    summary_generator: SummaryGenerator | None = None,
    context_estimate: ContextEstimate | None = None,
    retry_policy: CompactionRetryPolicy | None = None,
    retry_predicate: CompactionRetryPredicate | None = None,
    retry_callback: CompactionRetryCallback | None = None,
) -> CompactionResult:
    """对 messages 执行 compaction——返回 CompactionResult。

    步骤：
    1. config 缺省时用默认 CompactionConfig
    2. `len(messages) < config.min_messages_to_compact`
       → applied=False, reason="not_enough_messages"
    3. retained = messages[-keep_last_n:]；compacted = messages[:-keep_last_n]
       （keep_last_n=0 时 retained=[], compacted=messages）
    4. compacted 为空 → applied=False, reason="nothing_to_compact"
    5. 构造 CompactionInput，调 summary_generator（或 default）
    6. 生成 SummaryMessage
    7. new_messages = [summary_message_dict, *retained_dicts]
    8. 返回 CompactionResult(applied=True, ...)

    本函数**不修改**任何入参；只返回新结构。调用方（Harness / SessionMemory）
    负责把 new_messages 写回 Agent / Session。
    """
    cfg = config or CompactionConfig()

    # 边界校验
    if cfg.keep_last_n_messages < 0:
        raise ValueError(
            f"CompactionConfig.keep_last_n_messages 不能为负，实际：{cfg.keep_last_n_messages}"
        )
    if cfg.keep_last_n_turns < 0:
        raise ValueError(
            f"CompactionConfig.keep_last_n_turns 不能为负，实际：{cfg.keep_last_n_turns}"
        )
    if cfg.keep_recent_tokens is not None and cfg.keep_recent_tokens < 0:
        raise ValueError(
            "CompactionConfig.keep_recent_tokens 不能为负，"
            f"实际：{cfg.keep_recent_tokens}"
        )
    if cfg.context_window is not None and cfg.context_window <= 0:
        raise ValueError("CompactionConfig.context_window 必须 > 0")
    if cfg.reserved_output_tokens < 0:
        raise ValueError("CompactionConfig.reserved_output_tokens 不能为负")
    if cfg.min_messages_to_compact < 1:
        raise ValueError(
            f"CompactionConfig.min_messages_to_compact 必须 >= 1，"
            f"实际：{cfg.min_messages_to_compact}"
        )
    if cfg.max_summary_chars <= 0:
        raise ValueError(
            f"CompactionConfig.max_summary_chars 必须 > 0，实际：{cfg.max_summary_chars}"
        )
    policy = retry_policy or CompactionRetryPolicy()
    if policy.max_attempts < 1:
        raise ValueError("CompactionRetryPolicy.max_attempts 必须 >= 1")
    if policy.initial_delay_ms < 0 or policy.max_delay_ms < 0:
        raise ValueError("CompactionRetryPolicy delay 不能为负")
    if policy.backoff_multiplier < 1:
        raise ValueError("CompactionRetryPolicy.backoff_multiplier 必须 >= 1")

    # Step 2: 不足最小条数
    if len(messages) < cfg.min_messages_to_compact:
        return CompactionResult(
            applied=False,
            reason="not_enough_messages",
            token_stats=_token_stats(messages, messages, cfg, context_estimate),
            metadata={
                "message_count": len(messages),
                "min_required": cfg.min_messages_to_compact,
            },
        )

    # Step 3: 切分
    keep_n = cfg.keep_last_n_messages
    if cfg.boundary_mode == "turn":
        compacted, retained = _split_at_complete_turn(messages, cfg)
    elif keep_n == 0:
        retained = []
        compacted = list(messages)
    else:
        retained = list(messages[-keep_n:]) if keep_n < len(messages) else list(messages)
        compacted = list(messages[:-keep_n]) if keep_n < len(messages) else []

    # Step 4: 没有可压缩的
    if not compacted:
        return CompactionResult(
            applied=False,
            reason="nothing_to_compact",
            token_stats=_token_stats(messages, messages, cfg, context_estimate),
            metadata={"message_count": len(messages), "keep_last_n": keep_n},
        )

    # Step 5: 构造 CompactionInput
    from .session import serialize_messages  # 局部 import 避免循环

    compacted_dicts = serialize_messages(compacted)
    retained_dicts = serialize_messages(retained)
    snapshot_dicts = [s.to_dict() for s in (snapshots or [])]
    snapshot_ids = [s.id for s in (snapshots or [])]
    previous_summary_parts = [
        "\n".join(block.text for block in message.content).strip()
        for message in compacted
        if isinstance(message, SummaryMessage)
    ]
    previous_summary = "\n\n---\n\n".join(
        part for part in previous_summary_parts if part
    ) or None

    generator_input = CompactionInput(
        messages_to_compact=compacted_dicts,
        retained_messages=retained_dicts,
        previous_summary=previous_summary,
        snapshots=snapshot_dicts,
        config=cfg.model_dump(mode="json"),
        metadata={"summary_title": cfg.summary_title},
    )

    gen = summary_generator or default_summary_generator
    result_text, attempts = await _generate_summary_with_retry(
        generator_input,
        gen,
        policy,
        retry_predicate or _default_retry_predicate,
        retry_callback,
    )

    # Step 6: 生成 SummaryMessage
    summary_metadata: dict[str, Any] = {
        "summary_title": cfg.summary_title,
        "keep_last_n_messages": cfg.keep_last_n_messages,
        "boundary_mode": cfg.boundary_mode,
        "keep_last_n_turns": cfg.keep_last_n_turns,
        "keep_recent_tokens": cfg.keep_recent_tokens,
        "previous_summary_included": previous_summary is not None,
        "retry_attempts": attempts,
        "include_tool_results": cfg.include_tool_results,
        **dict(cfg.metadata),
    }
    if cfg.include_snapshot_ids:
        summary_metadata["source_snapshot_ids"] = snapshot_ids

    summary_msg = SummaryMessage(
        summary_type="context_compaction",
        content=[TextContent(text=result_text)],
        source_message_count=len(compacted),
        source_snapshot_ids=snapshot_ids if cfg.include_snapshot_ids else [],
        source_turn_count=_turn_count(compacted),
        metadata=summary_metadata,
    )

    # Step 7/8: token audit data uses the canonical preflight estimate when
    # supplied, then the summary metadata records the same stable projection.
    after_messages = [summary_msg, *retained]
    token_stats = _token_stats(messages, after_messages, cfg, context_estimate)
    summary_msg.metadata["token_stats"] = token_stats.model_dump(mode="json")
    summary_dict = summary_msg.model_dump(mode="json")
    new_messages = [summary_dict, *retained_dicts]

    source = CompactionSource(
        source_message_count=len(messages),
        retained_message_count=len(retained),
        compacted_message_count=len(compacted),
        source_snapshot_ids=snapshot_ids,
        source_turn_count=_turn_count(messages),
        retained_turn_count=_turn_count(retained),
        compacted_turn_count=_turn_count(compacted),
        metadata=dict(cfg.metadata),
    )

    return CompactionResult(
        applied=True,
        summary_message=summary_msg,
        compacted_messages=compacted_dicts,
        retained_messages=retained_dicts,
        new_messages=new_messages,
        source=source,
        token_stats=token_stats,
        attempts=attempts,
        metadata={
            "config": cfg.model_dump(mode="json"),
            "generator": (
                "default"
                if summary_generator is None
                else f"custom:{getattr(summary_generator, '__name__', 'lambda')}"
            ),
        },
    )


# ============================================================================
# BranchSummary / BranchSummaryConfig
# ============================================================================


class BranchSummary(BaseModel):
    """分支级旁路摘要——不进入 Agent 上下文，只记录到 session.branch_summaries。

    字段：
      id                    branch-summary-{ts}-{uuid8}
      branch_id             用户传入的分支名（main / research-a / debug-branch）
      title                 可选标题
      summary               摘要正文
      source_session_id     来源 session id（若有）
      source_snapshot_ids   来源 snapshot id 列表
      source_message_count  摘要覆盖的原始 message 数
      source_turn_count     摘要覆盖的 turn 数
      created_at            毫秒时间戳
      metadata              附加信息
    """
    id: str
    branch_id: str
    title: str | None = None
    summary: str
    source_session_id: str | None = None
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_message_count: int = 0
    source_turn_count: int = 0
    created_at: int = Field(default_factory=_now_ms)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class BranchSummaryConfig(BaseModel):
    """BranchSummary 生成配置。"""
    branch_id: str = "main"
    title: str | None = None
    include_messages: bool = True
    include_snapshots: bool = True
    max_summary_chars: int = 4000
    metadata: dict[str, Any] = Field(default_factory=dict)


def _gen_branch_summary_id() -> str:
    """生成 branch summary id：`branch-summary-{ts}-{uuid8}`。"""
    return f"branch-summary-{_now_ms()}-{uuid.uuid4().hex[:8]}"


# ============================================================================
# create_branch_summary
# ============================================================================


async def create_branch_summary(
    *,
    session: Any | None = None,  # SessionMemory，避免硬 import 循环
    messages: list[Message] | None = None,
    snapshots: list[RequestSnapshot] | None = None,
    config: BranchSummaryConfig | None = None,
    summary_generator: SummaryGenerator | None = None,
) -> BranchSummary:
    """基于 session / messages / snapshots 生成分支级摘要。

    - 若传 session：messages = session.get_messages()；snapshots = session.get_snapshots()；
      source_session_id = session.id
    - 否则用传入的 messages / snapshots
    - 调用 summary_generator 或 default 生成摘要文本
    - 返回 BranchSummary——**不修改** Agent / Session messages

    BranchSummary 是旁路记录，只用于研究 / 备份 / 分支切换参考。
    """
    cfg = config or BranchSummaryConfig()

    source_session_id: str | None = None
    if session is not None:
        source_session_id = session.id
        if messages is None:
            messages = session.get_messages()
        if snapshots is None:
            snapshots = session.get_snapshots()

    messages = messages or []
    snapshots = snapshots or []

    # BranchSummaryConfig.include_messages / include_snapshots 控制是否纳入来源
    # 关闭时即使 session 或入参提供了，也不进摘要
    if not cfg.include_messages:
        messages = []
    if not cfg.include_snapshots:
        snapshots = []

    from .session import serialize_messages  # 局部 import

    messages_dicts = serialize_messages(messages)
    snapshot_dicts = [s.to_dict() for s in snapshots]
    snapshot_ids = [s.id for s in snapshots]

    # 用 CompactionInput 复用 generator（retained=[] 表示"全部进摘要"）
    gen_input = CompactionInput(
        messages_to_compact=messages_dicts,
        retained_messages=[],
        snapshots=snapshot_dicts,
        config={
            "max_summary_chars": cfg.max_summary_chars,
            "include_tool_results": True,
            "summary_title": cfg.title or "Branch Summary",
            **dict(cfg.metadata),
        },
        metadata={
            "branch_id": cfg.branch_id,
            "title": cfg.title,
        },
    )

    gen = summary_generator or default_summary_generator
    summary_text = gen(gen_input)
    if inspect.isawaitable(summary_text):
        summary_text = await summary_text
    if not isinstance(summary_text, str):
        raise TypeError(
            "summary_generator 必须返回 str 或 Awaitable[str]；"
            f"实际类型：{type(summary_text).__name__}"
        )

    return BranchSummary(
        id=_gen_branch_summary_id(),
        branch_id=cfg.branch_id,
        title=cfg.title,
        summary=summary_text,
        source_session_id=source_session_id,
        source_snapshot_ids=snapshot_ids,
        source_message_count=len(messages),
        source_turn_count=len(snapshots),
        metadata={
            "config": cfg.model_dump(mode="json"),
            "generator": (
                "default"
                if summary_generator is None
                else f"custom:{getattr(summary_generator, '__name__', 'lambda')}"
            ),
        },
    )


__all__ = [
    # Configs / Inputs / Results
    "CompactionConfig", "CompactionSource",
    "CompactionInput", "CompactionResult", "CompactionTokenStats",
    "CompactionRetryPolicy", "CompactionRetryEvent",
    "BranchSummaryConfig", "BranchSummary",
    # Generator
    "SummaryGenerator", "CompactionRetryCallback", "CompactionRetryPredicate",
    "default_summary_generator",
    # Functions
    "compact_messages", "create_branch_summary",
]
