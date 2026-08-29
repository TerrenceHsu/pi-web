// Messages / Prompt 类型。
//
// /api/messages?session_id=... → 历史消息列表
// /api/prompt  → 触发一次 prompt（同步阻塞）

import type { FileBlock } from "./files"
import type { JsonValue } from "./state"

export type MessageRole = "user" | "assistant" | "toolResult" | "summary" | "system" | string

export interface TextContent {
  type: "text"
  text: string
}

export interface ThinkingContent {
  type: "thinking"
  thinking: string
  thinking_signature?: string | null
  redacted?: boolean
}

/** UserMessage.content 中可能出现的工具调用块（assistant 也含此类型）。 */
export interface ToolCallContent {
  type: "toolCall"
  id?: string
  name?: string
  args?: JsonValue
}

/** ToolResultMessage.content 中的块。 */
export interface ToolResultContent {
  type: "toolResult"
  tool_call_id?: string
  name?: string
  result?: JsonValue
  output?: JsonValue
  is_error?: boolean
}

export type UserContent =
  | TextContent
  | ThinkingContent
  | FileBlock
  | ToolCallContent
  | ToolResultContent
  | { type: string; [k: string]: JsonValue }

export interface MessageUsage {
  input: number
  output: number
  total_tokens: number
}

export interface GenerationMetrics {
  latency_ms: number | null
  time_to_first_token_ms: number | null
  usage_available: boolean
}

export interface MessageContentWarning {
  code: "unicode_replacement_character" | string
  suspected: boolean
  replacement_character_count: number
  affected_value_count: number
  /** RFC 6901 JSON Pointer paths; never content snippets. */
  affected_paths: string[]
  paths_truncated: boolean
  /** U+FFFD replacement destroys the original code point, so this is false. */
  auto_repairable: false
}

export interface MessageContentIntegritySummary {
  suspected_message_count: number
  replacement_character_count: number
}

/** 后端任意消息的统一形状——字段宽松，UI 按 role 分发。 */
export interface AgentMessage {
  role: MessageRole
  content: UserContent[]
  timestamp?: number
  /** 工具结果消息中的附加字段（非 content） */
  tool_call_id?: string
  is_error?: boolean
  terminate?: boolean
  details?: Record<string, JsonValue>
  tool_calls?: JsonValue[]
  name?: string
  api?: string
  provider?: string
  model?: string
  stop_reason?: string
  error_message?: string | null
  usage?: MessageUsage | null
  /** Context.tools 中从该工具结果之后可用的工具名；不是注册指令。 */
  added_tool_names?: string[]
  generation_metrics?: GenerationMetrics | null
  summary_type?: string
  source_message_count?: number
  source_turn_count?: number
  created_at?: number
  /** Read-only Web serialization metadata; never persisted into the message. */
  content_warnings?: MessageContentWarning[]
}

/** GET /api/messages response。 */
export interface MessagesResponse {
  count: number
  /**
   * D2-6 起：传 session_id 时返回 PersistedMessageDto（含 message_id）；
   * 不传 session_id 时返回 legacy AgentMessage（无 message_id）。
   * 前端用 'message_id' in m 区分。
   */
  messages: PersistedMessageDto[] | AgentMessage[]
  /** 后端在指定 session_id 时回填 */
  session_id?: string
  content_integrity?: MessageContentIntegritySummary
}

/**
 * D2-6：persisted message DTO——SQLite row 的 Web 层视图。
 *
 * `message_id` 是 SQLite messages.id（稳定，regenerate 后不变）。
 * 前端 reconciliation 必须用此 ID 而非 index。
 *
 * **映射规则**（D2-7 审核 §1 集成注意事项）：
 * - 持久化字段（message_id / session_id / idx / created_at）→ 始终读 DTO 顶层
 * - 完整 AgentMessage 内容 → 统一读 dto.message
 * - 兼容 role/content → 仅旧客户端兼容字段
 *
 * 顶层 role/content 与嵌套 message 必须从同一反序列化对象生成——不能独立加工。
 */
export interface PersistedMessageDto {
  message_id: string
  session_id: string
  idx: number
  /** 兼容字段——同 message.role */
  role: AgentMessage["role"]
  /** 兼容字段——同 message.content */
  content: AgentMessage["content"]
  created_at: number
  /** 完整 AgentMessage——前端应统一从此字段读完整内容 */
  message: AgentMessage
}

/** Type guard：响应中的 message 是否是 PersistedMessageDto。 */
export function isPersistedMessageDto(
  m: PersistedMessageDto | AgentMessage,
): m is PersistedMessageDto {
  return typeof (m as PersistedMessageDto).message_id === "string"
}

export type IntentRoute = "read_only" | "coding" | "knowledge"
export type IntentMode = "auto" | IntentRoute
export type ExecutionMode = "direct" | "plan"

export interface IntentAudit {
  route: IntentRoute
  confidence: number
  source: "explicit" | "rule" | "fallback" | "session_binding"
  reason_code: string
  explicit: boolean
}

/** POST /api/prompt body。 */
export interface PromptRequest {
  text: string
  session_id?: string
  file_ids?: string[]
  /** 自动创建/复用 Sandbox，限制为 coding_* 工具，并在结束后验证、冻结。 */
  coding_mode?: boolean
  /** 可选显式覆盖；省略或 auto 时由产品路由器判定。 */
  intent_mode?: IntentMode
  /** direct 使用单 Agent；plan 使用 Planner–Executor–Verifier 状态机。 */
  execution_mode?: ExecutionMode
  /** 顶层快捷字段——等价于 skill_selection.names；后端会合并去重。 */
  skill_names?: string[]
  skill_selection?: {
    names?: string[]
    tags?: string[]
    values?: Record<string, JsonValue>
  }
}

/**
 * POST /api/prompt response。
 *
 * - ok=true：messages 是最终 messages；applied_skill_names 是本轮实际启用的 skill
 * - ok=false：error 字段含原因（harness 异常等）
 * - attachments：P0-3 文件附件 metadata，前端展示用
 */
export interface PromptResponse {
  ok: boolean
  session_id?: string
  messages?: AgentMessage[]
  applied_skill_names?: string[]
  attachments?: {
    attached_file_ids: string[]
    attached_file_names: string[]
    attached_file_count: number
    attached_supported_file_count: number
    attached_unsupported_file_count: number
  }
  error?: string
  error_type?: string
  coding_sandbox?: {
    operation_id: string
    status: string
    workspace_revision: number
    artifact_id: string | null
    approval_required: boolean
  } | null
  intent?: IntentAudit | null
  continuity?: {
    status: "updated" | "pending_retry" | "deferred" | "skipped" | "unavailable"
    operation_id?: string
    source_sha256?: string
    memory_file_id?: string
    workspace_revision?: number
    recovered?: boolean
    blocked_by_sandbox_operation_id?: string | null
    error_code?: string
    reason?: string
  } | null
  /** detail 字段——某些 4xx 路径会返回这个（HTTPException） */
  detail?: string
}

/**
 * 旧 PromptPayload——ChatPanel.vue 调 `api.sendPrompt(text, skillSelection)`
 * 时通过 wrapper 转 PromptRequest。保留是为了让旧 .vue 编译过。
 */
export interface PromptPayload {
  text: string
  skill_selection?: {
    names?: string[]
    tags?: string[]
    values?: Record<string, JsonValue>
  }
  file_ids?: string[]
  skill_names?: string[]
  session_id?: string
}

/** 旧 SkillSelection——保留给 ChatPanel.vue 现有 input。 */
export interface SkillSelection {
  names?: string[]
  tags?: string[]
  values?: Record<string, JsonValue>
}

// ============================================================================
// P1-B3 异步 prompt / request lifecycle 类型
// ============================================================================

/** Request 当前状态——后端 Literal["queued","running","completed","error","aborted"] */
export type RequestStatus = "queued" | "running" | "completed" | "error" | "aborted"

/**
 * POST /api/prompt/async response（P1-B1）。
 *
 * 立即返回 202 + request_id——HTTP 不等模型完成。
 * 调用方用 events_url / request_url / abort_url 后续查询和控制。
 */
export interface PromptAsyncResponse {
  ok: boolean
  request_id: string
  session_id: string
  status: "queued" | "running"
  events_url: string
  request_url: string
  abort_url: string
  intent: IntentAudit | null
  execution_mode: ExecutionMode
}

/**
 * GET /api/requests/{request_id} response——active + history 都查。
 *
 * 安全约束：**不含** task / payload / system prompt / MCP env / traceback。
 *
 * D2-5 起新增 operation / regeneration_id / target_message_id（向后兼容）——
 * 普通 prompt 三字段分别为 "prompt" / null / null。
 */
export interface RequestSummary {
  request_id: string
  session_id: string | null
  status: RequestStatus
  created_at: string | null
  started_at: string | null
  ended_at: string | null
  error: string | null
  error_type: string | null
  abort_reason: string | null
  result_summary: {
    message_count?: number
    applied_skill_names?: string[]
    session_id?: string | null
    command?: string
    memory_file_id?: string
    memory_logical_path?: string
    source_message_count?: number
    source_sha256?: string
    idempotent_recovery?: boolean
    coding_sandbox?: PromptResponse["coding_sandbox"]
    continuity?: PromptResponse["continuity"]
    intent?: IntentAudit | null
  } | null
  event_start_sequence: number | null
  event_end_sequence: number | null
  /** 操作类型——prompt / regenerate / checkpointer */
  operation?: RequestOperation
  /** D2-5：regenerate 路径下的 revision.id（== regeneration_id） */
  regeneration_id?: string | null
  /** D2-5：regenerate 目标 assistant_message_id */
  target_message_id?: string | null
  awaiting_approval?: boolean
  pending_approval_count?: number
  approvals_url?: string
}

/** D2-5：request operation 类型。 */
export type RequestOperation = "prompt" | "regenerate" | "checkpointer"

/** GET /api/requests?session_id=&status=active&limit=N response（P1-B3-3） */
export interface RequestListResponse {
  count: number
  requests: RequestSummary[]
}

/**
 * D2-5：POST /api/sessions/{sid}/messages/{aid}/regenerate response（202）。
 *
 * regeneration_id == revision.id（不另生成第三个 ID）。
 */
export interface RegenerateResponse {
  ok: boolean
  operation: "regenerate"
  regeneration_id: string
  request_id: string
  session_id: string
  assistant_message_id: string
  status: "queued"
}

/**
 * D2-5：GET /api/sessions/{sid}/messages/{aid}/revisions response。
 *
 * **安全 serializer**——不含 content_json / base_content_sha256 / request_id。
 */
export interface RevisionListItem {
  revision_id: string
  revision_number: number
  status:
    | "running"
    | "completed"
    | "superseded"
    | "error"
    | "aborted"
    | "interrupted"
  created_at: string
  completed_at: string | null
  /** true 当且仅当 status == "completed" */
  is_current: boolean
}

export interface RevisionListResponse {
  session_id: string
  assistant_message_id: string
  items: RevisionListItem[]
  next_before_revision_number: number | null
}
