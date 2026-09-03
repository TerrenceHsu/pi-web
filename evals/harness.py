"""Offline harnesses that execute the real coding-agent product boundary."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from coding_agent_app.core import (
    CodingAgentResourceSnapshot,
    CodingAgentServices,
    CodingAgentSession,
    StaticCodingAgentResourceLoader,
    clone_agent_harness,
    create_coding_agent_application,
    create_coding_agent_session,
)
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.ai.messages import AssistantMessage, TextContent
from pi_agent_core_py.ai.model_client import ModelClient
from pi_agent_core_py.ai.providers.fake import FakeProviderAdapter
from pi_agent_core_py.ai.stream_events import StreamEvent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.session_backends.sqlite import SQLiteSessionRepository
from pi_agent_core_py.skills import SkillSelection
from pi_agent_core_py.telemetry import InMemoryTelemetryContext

from .judges import EvalJudge
from .models import (
    EvalCase,
    EvalError,
    EvalJudgeResult,
    EvalObservation,
    EvalReloadCheck,
    EvalScoredObservation,
    EvalToolCall,
    EvalToolResult,
    EvalUsage,
    JsonValue,
    PromptStep,
    ReloadStep,
)

ProviderScriptFactory = Callable[
    [EvalCase],
    Sequence[Sequence[StreamEvent]],
]
ResourceFactory = Callable[[Path, EvalCase], CodingAgentResourceSnapshot]


@dataclass(frozen=True, slots=True)
class EvalRunContext:
    eval_set: str
    repetition: int
    group_key: str
    temp_root: Path | None = None


class EvalHarness(Protocol):
    name: str

    async def run(
        self,
        case: EvalCase,
        context: EvalRunContext,
    ) -> EvalObservation: ...


@dataclass(frozen=True, slots=True)
class EvalSuite:
    name: str
    cases: tuple[EvalCase, ...]
    baseline: EvalHarness
    candidates: tuple[EvalHarness, ...]
    judges: tuple[EvalJudge, ...]
    repetitions: int = 1
    minimum_candidate_score: float = 1.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("eval suite name must not be empty")
        if not self.cases:
            raise ValueError("eval suite must contain at least one case")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("eval case ids must be unique within a suite")
        if not self.candidates:
            raise ValueError("eval suite must contain at least one candidate")
        names = [self.baseline.name, *(candidate.name for candidate in self.candidates)]
        if any(not name.strip() for name in names):
            raise ValueError("eval harness name must not be empty")
        if len(names) != len(set(names)):
            raise ValueError("eval harness names must be unique within a suite")
        if self.repetitions < 1:
            raise ValueError("eval repetitions must be positive")
        if not 0 <= self.minimum_candidate_score <= 1:
            raise ValueError("minimum candidate score must be between 0 and 1")


def _empty_resources(_workspace: Path, _case: EvalCase) -> CodingAgentResourceSnapshot:
    return CodingAgentResourceSnapshot()


def _read_only_tool(name: str) -> bool:
    return name.startswith(("read", "view", "list", "search"))


@dataclass(frozen=True, slots=True)
class LocalCodingAgentHarnessConfig:
    name: str
    scripts: ProviderScriptFactory
    resources: ResourceFactory = _empty_resources
    system_prompt: str = "You are a local coding agent under deterministic evaluation."
    model: str = "fake-1"
    read_only_tool: Callable[[str], bool] = _read_only_tool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("eval harness name must not be empty")
        if not self.system_prompt.strip():
            raise ValueError("eval system prompt must not be empty")
        if not self.model.strip():
            raise ValueError("eval model name must not be empty")


class LocalCodingAgentHarness:
    """Run one eval case without Web, environment credentials or network I/O."""

    def __init__(self, config: LocalCodingAgentHarnessConfig) -> None:
        self.config = config
        self.name = config.name

    async def run(
        self,
        case: EvalCase,
        context: EvalRunContext,
    ) -> EvalObservation:
        started = time.perf_counter()
        response: str | None = None
        stop_reason: str | None = None
        messages: tuple[dict[str, Any], ...] = ()
        tool_calls: list[EvalToolCall] = []
        tool_results: list[EvalToolResult] = []
        workspace_files: tuple[str, ...] = ()
        workspace_text: dict[str, str] = {}
        system_prompts: tuple[str, ...] = ()
        telemetry_records: tuple[dict[str, JsonValue], ...] = ()
        reload_checks: list[EvalReloadCheck] = []
        message_counts: list[int] = []
        usage = EvalUsage()
        error: EvalError | None = None
        resources = CodingAgentResourceSnapshot()
        workspace_bound = False

        temp_parent = str(context.temp_root) if context.temp_root is not None else None
        try:
            with tempfile.TemporaryDirectory(prefix="pi-local-eval-", dir=temp_parent) as root:
                root_path = Path(root)
                workspace = root_path / "workspace"
                workspace.mkdir()
                resources = self.config.resources(workspace, case)
                scripts = [list(turn) for turn in self.config.scripts(case)]
                adapter = FakeProviderAdapter(scripts, model=self.config.model)
                template = AgentHarness(
                    Agent(
                        system_prompt=self.config.system_prompt,
                        client=ModelClient(adapter),
                        tools=list(resources.tools),
                    )
                )
                telemetry = InMemoryTelemetryContext()
                repository = SQLiteSessionRepository(root_path / "sessions.sqlite")
                services = CodingAgentServices(
                    resources=StaticCodingAgentResourceLoader(resources),
                    session_repository=repository,
                    workspace_store=workspace,
                    telemetry=telemetry,
                )
                session_id = f"eval-{case.id}-{context.repetition}"
                app = None
                try:
                    initial_storage = await repository.create(
                        session_id=session_id,
                        metadata={"kind": "local-eval"},
                    )
                    await initial_storage.release()

                    async def session_factory(
                        requested_session_id: str,
                    ) -> CodingAgentSession:
                        metadata = next(
                            item
                            for item in await repository.list()
                            if item.id == requested_session_id
                        )
                        storage = await repository.open(metadata)
                        harness = clone_agent_harness(template)
                        harness.agent.state.messages = await repository.store.list_messages(
                            requested_session_id
                        )
                        return create_coding_agent_session(
                            session_id=requested_session_id,
                            harness=harness,
                            read_only_tool=self.config.read_only_tool,
                            services=services,
                            session_storage=storage,
                        )

                    app = create_coding_agent_application(
                        services=services,
                        session_factory=session_factory,
                    )
                    session = await app.session(session_id)
                    workspace_bound = services.workspace_store == workspace
                    skill_selection = (
                        SkillSelection(names=list(case.skill_names))
                        if case.skill_names
                        else None
                    )
                    for step in case.steps:
                        if isinstance(step, PromptStep):
                            await session.run_prompt(
                                step.content,
                                mode=case.mode,
                                coding_tool_names=case.coding_tool_names,
                                skill_selection=skill_selection,
                                system_prompt_suffix=case.system_prompt_suffix,
                            )
                            await _persist_session(repository, session)
                            _collect_snapshot(session.harness, tool_calls, tool_results)
                            message_counts.append(len(session.harness.agent.state.messages))
                        elif isinstance(step, ReloadStep):
                            before = len(session.harness.agent.state.messages)
                            session = await app.runtime.replace(session_id)
                            after = len(session.harness.agent.state.messages)
                            reload_checks.append(EvalReloadCheck(before, after))
                            message_counts.append(after)

                    final_messages = list(session.harness.agent.state.messages)
                    messages = tuple(
                        message.model_dump(mode="json") for message in final_messages
                    )
                    terminal = next(
                        (
                            message
                            for message in reversed(final_messages)
                            if isinstance(message, AssistantMessage)
                        ),
                        None,
                    )
                    if terminal is not None:
                        response = _assistant_text(terminal)
                        stop_reason = terminal.stop_reason
                    usage = _summarize_usage(final_messages, tool_results)
                    system_prompts = tuple(adapter.all_system_prompt_calls)
                    workspace_files, workspace_text = _snapshot_workspace(workspace)
                    telemetry_records = _telemetry_records(telemetry)
                except Exception as exc:
                    error = EvalError(type(exc).__name__, str(exc)[:512] or None)
                    telemetry_records = _telemetry_records(telemetry)
                    workspace_files, workspace_text = _snapshot_workspace(workspace)
                    system_prompts = tuple(adapter.all_system_prompt_calls)
                finally:
                    cleanup_errors: list[Exception] = []
                    if app is not None:
                        try:
                            await app.close()
                        except Exception as exc:
                            cleanup_errors.append(exc)
                    try:
                        await repository.close()
                    except Exception as exc:
                        cleanup_errors.append(exc)
                    try:
                        await template.close()
                    except Exception as exc:
                        cleanup_errors.append(exc)
                    if error is None and cleanup_errors:
                        cleanup = cleanup_errors[0]
                        error = EvalError(type(cleanup).__name__, str(cleanup)[:512] or None)
        except Exception as exc:
            error = EvalError(type(exc).__name__, str(exc)[:512] or None)

        return EvalObservation(
            eval_set=context.eval_set,
            case_id=case.id,
            harness=self.name,
            repetition=context.repetition,
            group_key=context.group_key,
            response=response,
            stop_reason=stop_reason,
            messages=messages,
            tool_calls=tuple(tool_calls),
            tool_results=tuple(tool_results),
            system_prompts=system_prompts,
            workspace_files=workspace_files,
            workspace_text=workspace_text,
            resource_skills=tuple(skill.name for skill in resources.skills),
            resource_tools=tuple(tool.name for tool in resources.tools),
            resource_mcp_tools=resources.mcp_tool_names,
            context_fragments=resources.context_fragments,
            workspace_bound=workspace_bound,
            telemetry=telemetry_records,
            reload_checks=tuple(reload_checks),
            message_counts=tuple(message_counts),
            usage=usage,
            total_ms=max(0.0, (time.perf_counter() - started) * 1000),
            error=error,
        )


async def run_suite(
    suite: EvalSuite,
    *,
    temp_root: Path | None = None,
) -> tuple[EvalScoredObservation, ...]:
    """Run baseline and candidates in stable paired order."""

    results: list[EvalScoredObservation] = []
    harnesses = (suite.baseline, *suite.candidates)
    for repetition in range(1, suite.repetitions + 1):
        for case in suite.cases:
            group_key = derive_group_key(case, repetition)
            for harness in harnesses:
                observation = await harness.run(
                    case,
                    EvalRunContext(
                        eval_set=suite.name,
                        repetition=repetition,
                        group_key=group_key,
                        temp_root=temp_root,
                    ),
                )
                judgments: list[EvalJudgeResult] = []
                if observation.infrastructure_ok:
                    for judge in suite.judges:
                        try:
                            judgments.append(judge.evaluate(case, observation))
                        except Exception as exc:
                            judgments.append(
                                EvalJudgeResult(
                                    name=judge.name,
                                    score=0,
                                    rationale=f"judge failed: {type(exc).__name__}",
                                )
                            )
                results.append(EvalScoredObservation(observation, tuple(judgments)))
    return tuple(results)


def derive_group_key(case: EvalCase, repetition: int) -> str:
    if repetition < 1:
        raise ValueError("eval repetition must be positive")
    return json.dumps([case.id.strip(), repetition], separators=(",", ":"))


async def _persist_session(
    repository: SQLiteSessionRepository,
    session: CodingAgentSession,
) -> None:
    await repository.store.replace_messages(
        session.session_id,
        list(session.harness.agent.state.messages),
    )
    snapshot = session.harness.last_snapshot
    if snapshot is not None:
        await repository.store.append_snapshot(session.session_id, snapshot)


def _collect_snapshot(
    harness: AgentHarness,
    calls: list[EvalToolCall],
    results: list[EvalToolResult],
) -> None:
    snapshot = harness.last_snapshot
    if snapshot is None:
        return
    calls.extend(
        EvalToolCall(
            id=call.id,
            name=call.name,
            arguments={str(key): _to_json(value) for key, value in call.arguments.items()},
        )
        for call in snapshot.tool_calls
    )
    results.extend(
        EvalToolResult(
            tool_call_id=result.tool_call_id,
            name=result.name,
            text="".join(
                str(item.get("text", ""))
                for item in result.content
                if item.get("type") == "text"
            ),
            is_error=result.is_error,
            terminate=result.terminate,
        )
        for result in snapshot.tool_results
    )


def _assistant_text(message: AssistantMessage) -> str:
    return "".join(
        part.text for part in message.content if isinstance(part, TextContent)
    )


def _summarize_usage(
    messages: Sequence[Any],
    tool_results: Sequence[EvalToolResult],
) -> EvalUsage:
    assistants = [message for message in messages if isinstance(message, AssistantMessage)]
    costs = [
        message.usage.cost.total
        for message in assistants
        if message.usage.cost is not None
    ]
    terminal = assistants[-1] if assistants else None
    return EvalUsage(
        provider=terminal.provider if terminal is not None else None,
        model=terminal.model if terminal is not None else None,
        input_tokens=sum(message.usage.input for message in assistants),
        output_tokens=sum(message.usage.output for message in assistants),
        total_tokens=sum(message.usage.total_tokens for message in assistants),
        tool_calls=len(tool_results),
        tool_errors=sum(result.is_error for result in tool_results),
        estimated_cost=(
            sum(costs) if assistants and len(costs) == len(assistants) else None
        ),
    )


def _snapshot_workspace(workspace: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    files = tuple(
        sorted(
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob("*")
            if path.is_file()
        )
    )
    text: dict[str, str] = {}
    for relative in files:
        path = workspace / relative
        try:
            if path.stat().st_size <= 1_000_000:
                text[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
    return files, text


def _telemetry_records(
    telemetry: InMemoryTelemetryContext,
) -> tuple[dict[str, JsonValue], ...]:
    return tuple(
        {
            "name": span.name,
            "attributes": {
                key: _to_json(value) for key, value in span.attributes.items()
            },
            "events": [
                {
                    "name": event.name,
                    "attributes": {
                        key: _to_json(value)
                        for key, value in event.attributes.items()
                    },
                }
                for event in span.events
            ],
            "status": span.status.status,
            "duration_ms": span.duration_ms,
            "settled": span.settled,
        }
        for span in telemetry.get_spans()
    )


def _to_json(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list | tuple):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json(item) for key, item in value.items()}
    return str(value)


__all__ = [
    "EvalHarness",
    "EvalRunContext",
    "EvalSuite",
    "LocalCodingAgentHarness",
    "LocalCodingAgentHarnessConfig",
    "ProviderScriptFactory",
    "ResourceFactory",
    "derive_group_key",
    "run_suite",
]
