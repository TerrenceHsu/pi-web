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

export type UserContent = TextContent | FileBlock | ToolCallContent | ToolResultContent | { type: string; [k: string]: JsonValue }

/** 后端任意消息的统一形状——字段宽松，UI 按 role 分发。 */
export interface AgentMessage {
  role: MessageRole
  content: UserContent[]
  timestamp?: number
  /** 工具结果消息中的附加字段（非 content） */
  tool_call_id?: string
  tool_calls?: JsonValue[]
  name?: string
}

/** GET /api/messages response。 */
export interface MessagesResponse {
  count: number
  messages: AgentMessage[]
  /** 后端在指定 session_id 时回填 */
  session_id?: string
}

/** POST /api/prompt body。 */
export interface PromptRequest {
  text: string
  session_id?: string
  file_ids?: string[]
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
}

/**
 * GET /api/requests/{request_id} response——active + history 都查。
 *
 * 安全约束：**不含** task / payload / system prompt / MCP env / traceback。
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
  } | null
  event_start_sequence: number | null
  event_end_sequence: number | null
}

/** GET /api/requests?session_id=&status=active&limit=N response（P1-B3-3） */
export interface RequestListResponse {
  count: number
  requests: RequestSummary[]
}
