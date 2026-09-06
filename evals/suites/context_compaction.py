"""Deterministic context-view safety evaluations, without a model or Web server.

These scenarios check preservation and publication contracts; they do not claim
to evaluate a language model's summarization quality or Web retry scheduling.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from agent_workspace.structured_memory import (
    MemoryEvidence,
    MemoryItem,
    memory_prompt_text,
    render_memory,
)
from coding_agent_app.core.context_management import ContextManagementService
from coding_agent_app.core.tool_context import ToolContextBudgeter
from pi_agent_core_py.agent.context import convert_to_llm
from pi_agent_core_py.agent.harness.compaction.budget import estimate_message_tokens
from pi_agent_core_py.agent.harness.compaction.projection import render_summary, validate_summary
from pi_agent_core_py.agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore
from pi_agent_core_py.session_backends.sqlite.context_store import (
    ContextStoreError,
    SQLiteContextStore,
)

from ..harness import EvalRunContext, EvalSuite
from ..judges import ExactTextJudge, StopReasonJudge
from ..models import EvalCase, EvalError, EvalObservation, EvalUsage, PromptStep

_CONSTRAINT = "Execute Python only after explicit confirmation."
_FAILURE = "CSV decoding failed. Do not repeat unchanged encoding; ask for the correct encoding."


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        api="eval",
        provider="offline",
        model="deterministic",
        content=[TextContent(text=text)],
        timestamp=1,
    )


def _messages() -> list[AgentMessage]:
    return [
        UserMessage(content=[TextContent(text=_CONSTRAINT)], timestamp=1),
        _assistant("Investigation detail. " * 1500),
        UserMessage(content=[TextContent(text="The CSV remains unresolved.")], timestamp=2),
        _assistant(_FAILURE + " Investigation detail. " * 1500),
        UserMessage(content=[TextContent(text="Continue with the current task.")], timestamp=3),
        _assistant("Waiting for the correct encoding."),
    ]


def _summary(ids: list[str]) -> dict[str, Any]:
    return {
        "current_goal": "Resolve the CSV decoding issue safely.",
        "facts": [{"text": _CONSTRAINT, "source_entry_ids": [ids[0]]}],
        "decisions": [],
        "failed_attempts": [{"text": _FAILURE, "source_entry_ids": [ids[3]]}],
        "open_questions": [{"text": "What is the input encoding?", "source_entry_ids": [ids[2]]}],
        "next_steps": [],
        "artifacts": [],
        "memory_item_ids": ["memory-confirmation"],
    }


class ContextCompactionHarness:
    def __init__(self, name: str, *, projected: bool) -> None:
        self.name = name
        self.projected = projected

    async def run(self, case: EvalCase, context: EvalRunContext) -> EvalObservation:
        started = time.perf_counter()
        error: EvalError | None = None
        passed = False
        messages: list[AgentMessage] = []
        visible: list[AgentMessage] = []
        try:
            with tempfile.TemporaryDirectory(
                prefix="pi-context-eval-", dir=context.temp_root
            ) as root:
                repository = SQLiteSessionStore(Path(root) / "sessions.sqlite")
                await repository.init()
                try:
                    store = SQLiteContextStore(repository)
                    await store.initialize()
                    await repository.create_session(session_id="eval-session")
                    await repository.create_session(session_id="other-session")
                    messages = _messages()
                    await repository.replace_messages("eval-session", messages)
                    snapshot = await store.snapshot("eval-session")
                    manager = ContextManagementService(store)
                    summary = validate_summary(
                        json.dumps(_summary(snapshot.entry_ids)),
                        set(snapshot.entry_ids[:4]),
                        {"memory-confirmation"},
                    )
                    await manager.publish(
                        snapshot,
                        4,
                        render_summary(summary),
                        token_stats={},
                        trigger="eval",
                        summary=summary.model_dump(),
                    )
                    visible = (
                        await manager.project("eval-session", messages)
                        if self.projected
                        else list(messages)
                    )
                    passed = await self._scenario(
                        case.id, repository, store, manager, messages, visible
                    )
                finally:
                    await repository.close()
        except Exception as exc:
            error = EvalError(type(exc).__name__)
        estimated_tokens = estimate_message_tokens(convert_to_llm(visible))
        return EvalObservation(
            eval_set=context.eval_set,
            case_id=case.id,
            harness=self.name,
            repetition=context.repetition,
            group_key=context.group_key,
            response="SAFE" if passed else "UNSAFE",
            stop_reason="stop" if passed else "error",
            messages=tuple(message.model_dump(mode="json") for message in messages),
            tool_calls=(),
            tool_results=(),
            system_prompts=(),
            workspace_files=(),
            workspace_text={},
            resource_skills=(),
            resource_tools=(),
            resource_mcp_tools=(),
            context_fragments=(),
            workspace_bound=False,
            telemetry=(),
            reload_checks=(),
            message_counts=(len(messages), len(visible)),
            usage=EvalUsage(
                provider="offline",
                model="deterministic",
                input_tokens=estimated_tokens,
                total_tokens=estimated_tokens,
            ),
            total_ms=max(0.0, (time.perf_counter() - started) * 1000),
            error=error,
        )

    async def _scenario(
        self,
        case_id: str,
        repository: SQLiteSessionStore,
        store: SQLiteContextStore,
        manager: ContextManagementService,
        messages: list[AgentMessage],
        visible: list[AgentMessage],
    ) -> bool:
        before = await store.snapshot("eval-session")
        if case_id == "fact-and-memory-source-retention":
            memory = render_memory(
                [
                    MemoryItem(
                        id="memory-confirmation",
                        kind="constraint",
                        text=_CONSTRAINT,
                        sources=[MemoryEvidence(entry_id=before.entry_ids[0], role="user")],
                        updated_at=1,
                    )
                ],
                "",
            )
            projected_memory = memory_prompt_text(
                memory, set((await store.snapshot("eval-session")).entry_ids)
            )
            text = json.dumps([message.model_dump(mode="json") for message in visible])
            return (
                _CONSTRAINT in text
                and "pending_confirmation" not in projected_memory
                and "memory-confirmation; active" in projected_memory
                and await repository.list_messages("eval-session") == messages
                and (
                    not self.projected
                    or estimate_message_tokens(convert_to_llm(visible))
                    < estimate_message_tokens(convert_to_llm(messages))
                )
            )
        if case_id == "stale-branch-view-invalidated":
            changed = [
                UserMessage(content=[TextContent(text="New independent branch")]),
                *messages[1:],
            ]
            await repository.replace_messages("eval-session", changed)
            return (
                await manager.active_record("eval-session") is None
                and await manager.project("eval-session", changed) == changed
            )
        if case_id == "failed-attempts-survive-repeated-projection":
            repeated = await manager.project("eval-session", messages)
            repeated_again = await manager.project("eval-session", messages)
            text = json.dumps([message.model_dump(mode="json") for message in repeated])
            return (
                _FAILURE in text
                and "CSV succeeded" not in text
                and repeated == repeated_again
                and await store.snapshot("eval-session") == before
            )
        if case_id == "invalid-summary-cannot-replace-current-view":
            active = await manager.active_record("eval-session")
            rejected = 0
            for invalid in (
                "not JSON",
                json.dumps(
                    {
                        **_summary(before.entry_ids),
                        "facts": [
                            {"text": "forged success", "source_entry_ids": ["foreign-entry"]}
                        ],
                    }
                ),
            ):
                try:
                    validate_summary(invalid, set(before.entry_ids[:4]), {"memory-confirmation"})
                except ValueError:
                    rejected += 1
            return (
                rejected == 2
                and await manager.active_record("eval-session") == active
                and await store.snapshot("eval-session") == before
            )
        if case_id == "current-tool-error-readback-and-isolation":
            body = (
                "Traceback: decode failed\n"
                + "error detail " * 2500
                + "\nUnicodeError: wrong encoding"
            )
            call = AssistantMessage(
                api="eval",
                provider="offline",
                model="deterministic",
                content=[ToolCall(id="current-call", name="read_csv", arguments={})],
            )
            result = ToolResultMessage(
                tool_call_id="current-call",
                name="read_csv",
                content=[TextContent(text=body)],
                is_error=True,
            )
            source: list[AgentMessage] = [call, result]
            view = await ToolContextBudgeter(store).project(
                "eval-session", source, request_id="pending-request", per_result_tokens=800
            )
            if not isinstance(view[1], ToolResultMessage) or not isinstance(
                view[1].content[0], TextContent
            ):
                return False
            metadata = json.loads(view[1].content[0].text.splitlines()[1])
            ref = metadata["ref"]
            parts: list[str] = []
            offset = 0
            while True:
                page = await store.read_tool_output("eval-session", ref, offset, 12000)
                parts.append(page["text"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            isolated = False
            try:
                await store.read_tool_output("other-session", ref)
            except ContextStoreError:
                isolated = True
            return (
                view[0] == call
                and view[1].tool_call_id == "current-call"
                and view[1].is_error
                and "".join(parts) == body
                and isolated
                and source[1] == result
                and await store.snapshot("eval-session") == before
            )
        raise ValueError("unknown context evaluation case")


def build_suite() -> EvalSuite:
    cases = tuple(
        EvalCase(
            id=case,
            steps=(PromptStep("Evaluate the context safety contract."),),
            tags=("context", "offline", "safety"),
        )
        for case in (
            "fact-and-memory-source-retention",
            "stale-branch-view-invalidated",
            "failed-attempts-survive-repeated-projection",
            "invalid-summary-cannot-replace-current-view",
            "current-tool-error-readback-and-isolation",
        )
    )
    return EvalSuite(
        name="context-compaction",
        cases=cases,
        baseline=ContextCompactionHarness("raw-history-baseline", projected=False),
        candidates=(ContextCompactionHarness("source-bound-projection", projected=True),),
        judges=(ExactTextJudge("SAFE"), StopReasonJudge()),
    )


__all__ = ["ContextCompactionHarness", "build_suite"]
