"""pi-agent-core-py: Python port of @earendil-works/pi-agent-core.

当前进度：Step 21 完成（Provider Adapter Refactor）——
Phase A/B/C 完成（Step 1–21）；Phase D 及之后未包含在本副本。
"""
from __future__ import annotations

__version__ = "0.0.21"

# 消息（Step 1 起 + Step 3 扩展 + Step 4 加 ToolCall + Step 5 加 ToolResultMessage）
# Agent（Step 8 + Step 9）
from .agent import Agent, AgentRequest, AgentState, AgentStatus, Subscriber

# Compaction / Branch Summary（Step 15）
from .compaction import (
    BranchSummary,
    BranchSummaryConfig,
    CompactionConfig,
    CompactionInput,
    CompactionResult,
    CompactionSource,
    SummaryGenerator,
    compact_messages,
    create_branch_summary,
    default_summary_generator,
)

# Context 转换
from .context import (
    TransformContextFn,
    convert_to_llm,
    transform_context,
)

# AgentEvent（Step 5 加 ToolExecutionStart/EndEvent；Step 9 加 Queue/Abort 事件）
from .events import (
    AgentAbortEvent,
    AgentEndEvent,
    AgentEvent,
    AgentRequestType,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    RequestEndEvent,
    RequestEndStatus,
    RequestQueuedEvent,
    RequestStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)

# Harness（Step 10）
from .harness import (
    AfterRequestHook,
    AgentHarness,
    BeforeRequestHook,
    HarnessContext,
    HarnessPhase,
    OnErrorHook,
    OnEventHook,
)

# Tool Hooks（Step 6 新增）
from .hooks import (
    AfterToolCallContext,
    AfterToolCallFn,
    BeforeToolCallContext,
    BeforeToolCallFn,
    BeforeToolCallResult,
    default_after_tool_call,
    default_before_tool_call,
)

# LLM 消息（Step 3 + Step 5 加 LLMToolResultMessage）
from .llm_messages import (
    LLMAssistantMessage,
    LLMMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)

# Loop
from .loop import run_event_loop, run_min_loop

# MCP tools（Step 16 新增）
from .mcp import (
    MAX_MCP_PROMPT_SKILL_NAME_LEN,
    MAX_NAMESPACED_NAME_LEN,
    MCP_NAME_RE,
    FakeMCPTransport,
    HttpMCPTransport,
    MCPAgentTool,
    MCPCallResult,
    MCPClient,
    MCPConnectionError,
    MCPError,
    MCPPromptArgument,
    MCPPromptInfo,
    MCPPromptMessage,
    MCPPromptResult,
    MCPPromptSkillAdapter,
    MCPProtocolError,
    MCPRegistry,
    MCPServerConfig,
    MCPServerState,
    MCPToolCallError,
    MCPToolInfo,
    MCPToolNotFoundError,
    MCPTransport,
    MCPTransportClosedError,
    StdioMCPTransport,
    make_mcp_prompt_skill_name,
    make_namespaced_tool_name,
    validate_namespace_part,
)
from .messages import (
    AgentMessage,
    AssistantContent,
    AssistantMessage,
    CustomMessage,
    Message,
    SummaryMessage,
    SummaryType,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

# ModelClient + Stream 事件（Step 5 加 ToolCallEvent；Step 21 拆 providers 子包）
from .model_client import (
    DoneEvent,
    ErrorEvent,
    FakeClient,
    GLMClient,
    ModelClient,
    StreamEvent,
    TextDeltaEvent,
    ToolCallEvent,
)

# Multi-Agent Router & Orchestrator（Step 28，未包含在本副本）
# Permission / Approval Policy（Step 18 新增）
from .policy import (
    AllowAllToolPermissionPolicy,
    DefaultToolPermissionPolicy,
    DenyAllToolPermissionPolicy,
    InMemoryToolPermissionAuditLog,
    PermissionDecisionType,
    ToolPermissionAuditRecord,
    ToolPermissionDecision,
    ToolPermissionPolicy,
    extract_candidate_paths,
    is_path_within_roots,
    parse_mcp_namespaced_tool,
)

# Provider Adapter（Step 21 新增）
from .providers import (
    AnthropicCompatAdapter,
    AnthropicCompatConfig,
    FakeProviderAdapter,
    GLMConfig,
    GLMProviderAdapter,
    ProviderAdapter,
    ProviderAuthenticationError,
    ProviderConfigError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderStreamError,
    to_anthropic_messages,
    to_anthropic_tools,
)

# Sandbox Execution（Step 22+）+ Slash Command（Step 24）+ Multi-Agent（Step 28）
# —— 未包含在本副本（仅保留 Step ≤21 功能）
# Session Memory（Step 12）
from .session import (
    InMemorySessionStore,
    JsonFileSessionStore,
    SessionMemory,
    SessionState,
    SessionStore,
    deserialize_message,
    deserialize_messages,
    deserialize_snapshot,
    serialize_message,
    serialize_messages,
)

# SQLite 线性 session 存储（P0-1）
from .session_sqlite import (
    SessionNotFoundError,
    SessionSerializationError,
    SQLiteSession,
    SQLiteSessionError,
    SQLiteSessionStore,
    SQLiteStoredMessage,
    SQLiteStoredSnapshot,
)

# Harness ↔ Session 同步（Step 13）
from .session_sync import (
    ISSUE_AGENT_SESSION_MESSAGES_MISMATCH,
    ISSUE_HARNESS_SESSION_SNAPSHOT_MISMATCH,
    ISSUE_LAST_SNAPSHOT_MISMATCH,
    ISSUE_MESSAGES_MISMATCH,
    ISSUE_NO_SESSION,
    ISSUE_TURN_COUNT_MISMATCH,
    SessionAutoSavePolicy,
    SessionConsistencyIssue,
    SessionConsistencyReport,
    SessionSyncConfig,
)

# Skill File Loader（Step 19 新增）
from .skill_loader import (
    DEFAULT_ALLOWED_FILENAMES,
    DEFAULT_MAX_FILE_SIZE_BYTES,
    SkillFileFormatError,
    SkillFileLoader,
    SkillFileLoadError,
    SkillFileSecurityError,
    SkillLoadConfig,
    parse_skill_markdown,
)

# Skills / Prompt Templates（Step 14）
from .skills import (
    PromptTemplate,
    PromptTemplateRenderError,
    Skill,
    SkillInjectionConfig,
    SkillNotFoundError,
    SkillRegistrationError,
    SkillRegistry,
    SkillSelection,
    SkillStatus,
    render_skill_block,
)

# Turn Snapshot（Step 11）
from .snapshot import (
    EventSnapshot,
    SnapshotBuilder,
    SnapshotStatus,
    ToolCallSnapshot,
    ToolResultSnapshot,
    TurnSnapshot,
)

# 默认对话向 system prompt（P0-5）
from .system_prompt import build_default_system_prompt

# Tool 参数校验（Step 16 新增）
from .tool_validation import (
    ToolArgumentValidationError,
    validate_tool_arguments,
)

# Tool 基础模型 + 内置工具（Step 5.5 加 WebSearchTool）
from .tools import (
    AgentTool,
    ToolDef,
    ToolExecutionMode,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    ToolResult,
    WebSearchTool,
)

__all__ = [
    "__version__",
    # messages
    "AgentMessage", "AssistantContent", "AssistantMessage", "CustomMessage",
    "Message", "SummaryMessage", "SummaryType",
    "TextContent", "ToolCall", "ToolResultMessage", "Usage", "UserMessage",
    # llm_messages
    "LLMAssistantMessage", "LLMMessage", "LLMToolResultMessage", "LLMUserMessage",
    # model_client
    "ModelClient", "GLMClient", "FakeClient",
    "StreamEvent", "TextDeltaEvent", "DoneEvent", "ErrorEvent", "ToolCallEvent",
    # providers (Step 21)
    "ProviderAdapter", "ProviderRequest",
    "ProviderError", "ProviderConfigError", "ProviderProtocolError",
    "ProviderAuthenticationError", "ProviderRateLimitError", "ProviderStreamError",
    "FakeProviderAdapter",
    "AnthropicCompatAdapter", "AnthropicCompatConfig",
    "GLMProviderAdapter", "GLMConfig",
    "to_anthropic_messages", "to_anthropic_tools",
    # events
    "AgentStartEvent", "AgentEndEvent",
    "TurnStartEvent", "TurnEndEvent",
    "MessageStartEvent", "MessageUpdateEvent", "MessageEndEvent",
    "ToolExecutionStartEvent", "ToolExecutionEndEvent",
    # Step 9 events
    "RequestQueuedEvent", "RequestStartEvent", "RequestEndEvent", "AgentAbortEvent",
    "AgentRequestType", "RequestEndStatus",
    "AgentEvent",
    # context
    "TransformContextFn", "convert_to_llm", "transform_context",
    # hooks
    "BeforeToolCallFn", "AfterToolCallFn",
    "BeforeToolCallContext", "BeforeToolCallResult",
    "AfterToolCallContext",
    "default_before_tool_call", "default_after_tool_call",
    # loop
    "run_event_loop", "run_min_loop",
    # agent
    "Agent", "AgentRequest", "AgentState", "AgentStatus", "Subscriber",
    # harness
    "AgentHarness", "HarnessContext", "HarnessPhase",
    "BeforeRequestHook", "AfterRequestHook",
    "OnEventHook", "OnErrorHook",
    # snapshot (Step 11)
    "SnapshotStatus", "SnapshotBuilder", "TurnSnapshot",
    "EventSnapshot", "ToolCallSnapshot", "ToolResultSnapshot",
    # session (Step 12)
    "SessionState", "SessionMemory",
    "SessionStore", "InMemorySessionStore", "JsonFileSessionStore",
    "serialize_message", "serialize_messages",
    "deserialize_message", "deserialize_messages",
    "deserialize_snapshot",
    # P0-1 sqlite session
    "SQLiteSessionStore", "SQLiteSession",
    "SQLiteStoredMessage", "SQLiteStoredSnapshot",
    "SQLiteSessionError", "SessionNotFoundError", "SessionSerializationError",
    # session_sync (Step 13)
    "SessionAutoSavePolicy", "SessionSyncConfig",
    "SessionConsistencyIssue", "SessionConsistencyReport",
    "ISSUE_NO_SESSION", "ISSUE_TURN_COUNT_MISMATCH", "ISSUE_MESSAGES_MISMATCH",
    "ISSUE_LAST_SNAPSHOT_MISMATCH",
    "ISSUE_AGENT_SESSION_MESSAGES_MISMATCH",
    "ISSUE_HARNESS_SESSION_SNAPSHOT_MISMATCH",
    # skills (Step 14)
    "PromptTemplate", "PromptTemplateRenderError",
    "Skill", "SkillStatus", "SkillRegistry",
    "SkillSelection", "SkillInjectionConfig",
    "SkillNotFoundError", "SkillRegistrationError",
    "render_skill_block",
    # 默认 system prompt (P0-5)
    "build_default_system_prompt",
    # compaction (Step 15)——SummaryMessage / SummaryType 已在 messages 段导出，不重复
    "CompactionConfig", "CompactionInput", "CompactionResult", "CompactionSource",
    "BranchSummary", "BranchSummaryConfig",
    "SummaryGenerator", "default_summary_generator",
    "compact_messages", "create_branch_summary",
    # tools
    "AgentTool", "ToolDef", "ToolExecutionMode",
    "ToolRegistry", "ToolResult",
    "ToolRegistrationError", "ToolNotFoundError",
    # built-in tools (Step 5.5)
    "WebSearchTool",
    # tool validation (Step 16)
    "ToolArgumentValidationError", "validate_tool_arguments",
    # MCP tools (Step 16)
    "MCPServerConfig",
    "MCP_NAME_RE", "MAX_NAMESPACED_NAME_LEN",
    "validate_namespace_part", "make_namespaced_tool_name",
    "MCPError", "MCPConnectionError", "MCPProtocolError",
    "MCPToolNotFoundError", "MCPToolCallError", "MCPTransportClosedError",
    "MCPTransport", "StdioMCPTransport", "HttpMCPTransport", "FakeMCPTransport",
    "MCPClient", "MCPToolInfo", "MCPCallResult",
    "MCPAgentTool",
    "MCPRegistry", "MCPServerState",
    # MCP prompts (Step 19)
    "MCPPromptArgument", "MCPPromptInfo", "MCPPromptMessage", "MCPPromptResult",
    "MCPPromptSkillAdapter",
    "make_mcp_prompt_skill_name", "MAX_MCP_PROMPT_SKILL_NAME_LEN",
    # Permission / Approval Policy (Step 18)
    "PermissionDecisionType",
    "ToolPermissionDecision", "ToolPermissionPolicy",
    "AllowAllToolPermissionPolicy", "DenyAllToolPermissionPolicy",
    "DefaultToolPermissionPolicy",
    "ToolPermissionAuditRecord", "InMemoryToolPermissionAuditLog",
    "is_path_within_roots", "extract_candidate_paths",
    "parse_mcp_namespaced_tool",
    # Skill File Loader (Step 19)
    "SkillLoadConfig", "SkillFileLoader",
    "SkillFileLoadError", "SkillFileFormatError", "SkillFileSecurityError",
    "parse_skill_markdown",
    "DEFAULT_ALLOWED_FILENAMES", "DEFAULT_MAX_FILE_SIZE_BYTES",
]
