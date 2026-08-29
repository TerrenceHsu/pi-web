// WebSocket / SSE 事件 + ChatStreamItem 类型。
//
// /ws/events 是推荐实时通道；后端发过来的 event 形状宽松——前端按 type 字段分发。
// AgentEvent 的 type 来自 src/pi_agent_core_py/events.py。
//
// P1-B2: 后端统一用 WebEventEnvelope 包装事件——event_id / sequence / request_id /
// session_id / type / timestamp / payload 7 字段。chatStore.handleEvent 按这些字段
// 去重 + 隔离不同 session/request。旧裸事件 schema 已退役。

import type { FileRef } from "./files"
import type { ToolApprovalStatus } from "./approvals"
import type { PlanRun } from "./plans"
import type {
  GenerationMetrics,
  IntentAudit,
  MessageContentWarning,
  MessageUsage,
} from "./messages"

interface ContentWarningAwareItem {
  contentWarnings?: MessageContentWarning[]
}

/**
 * P1-B2: 统一事件信封——所有 WS / SSE / GET /api/events 用同一个 schema。
 *
 * 后端 _web_event_hook 在广播入口一次性生成 envelope，三处客户端看到的是同一份 dict。
 */
export interface WebEventEnvelope {
  /** "evt_<uuid4 hex>"——前端按此字段去重 */
  event_id: string
  /** "req_..." / "req_sync_..." / null（非 prompt 管理事件为 null） */
  request_id: string | null
  /** "sess_..." / null */
  session_id: string | null
  /** 全局单调递增——前端按此检测 gap */
  sequence: number
  /** event 类型名（与 payload.type 冗余，方便客户端快速判断） */
  type: string
  /** ISO8601 UTC timestamp */
  timestamp: string
  /** 原 AgentEvent 序列化结果 + _received_at_ms */
  payload: Record<string, any>
}

/**
 * 后端推过来的事件——优先按 envelope 解释；兼容期内可能仍有裸事件（hello / shutdown）。
 *
 * 后端会发：
 *   - hello：连接建立时第一个事件（protocol-level）
 *   - shutdown：服务端关闭通知（protocol-level）
 *   - AgentEvent：均被 envelope 包装，type 字段对应后端事件类型
 *     agent_start / agent_end / turn_start / turn_end
 *     message_start / message_update / message_end
 *     tool_execution_start / tool_execution_update / tool_execution_end
 *     request_queued / request_start / request_end / agent_abort
 */
export type WebEvent = WebEventEnvelope | LegacyWebEvent

/** 兼容期：hello / shutdown 等协议事件仍可能用裸 schema（无 envelope 字段） */
export interface LegacyWebEvent {
  type: string
  [key: string]: any
}

/** Type guard: 是否为 P1-B2 envelope（含 event_id + sequence + payload） */
export function isWebEventEnvelope(ev: WebEvent): ev is WebEventEnvelope {
  return (
    typeof ev === "object" &&
    ev !== null &&
    typeof (ev as any).event_id === "string" &&
    typeof (ev as any).sequence === "number" &&
    typeof (ev as any).payload === "object"
  )
}

// ============================================================================
// 状态枚举
// ============================================================================

export type ToolStatus = "running" | "done" | "error"
export type TurnStatus = "queued" | "running" | "done" | "error"

// ============================================================================
// ChatStreamItem —— 中间消息流的统一 item 类型（9 种 kind）
// ============================================================================

export interface UserMessageItem extends ContentWarningAwareItem {
  kind: "user_message"
  id: string
  /** 用户输入的文本（多块 TextContent 合并后） */
  content: string
  /** 本轮附件——FileChip 渲染用（Step 6 启用） */
  files?: FileRef[]
  /** D2-7：来自 SQLite persisted message 的稳定 row id（regenerate 后不变）。
   * 流式期间的临时 user_message item 为 undefined。 */
  messageId?: string
  /** D2-7：是否来自 SQLite persisted（用于决定是否显示 Regenerate 按钮） */
  persisted?: boolean
  /** D2-7：SQLite row idx（reconciliation 按 idx 排序） */
  messageIndex?: number
}

export interface AssistantMessageItem extends ContentWarningAwareItem {
  kind: "assistant_message"
  id: string
  content: string
  /** 流式进行中——true 时显示 stream cursor */
  streaming?: boolean
  /** D2-7：来自 SQLite persisted message 的稳定 row id（regenerate 后不变）。
   * 流式期间的临时 assistant draft item 为 undefined。 */
  messageId?: string
  /** D2-7：是否来自 SQLite persisted（用于决定是否显示 Regenerate 按钮） */
  persisted?: boolean
  /** D2-7：SQLite row idx（reconciliation 按 idx 排序） */
  messageIndex?: number
  /** D2-7：regeneration 流式 draft——独立 bubble，不覆盖原 active assistant */
  isRegenerationDraft?: boolean
  usage?: MessageUsage
  generationMetrics?: GenerationMetrics | null
}

export interface TurnInfoItem extends ContentWarningAwareItem {
  kind: "turn_info"
  id: string
  title: string
  summary: string
  status?: TurnStatus
  /** 展开后显示的额外信息（事件计数 / request_id / duration 等） */
  details?: unknown
  intent?: IntentAudit | null
  /** UI 折叠状态（可选） */
  collapsed?: boolean
  /** 标记淡化样式——必须为 true */
  muted: true
}

export interface ToolCallItem {
  kind: "tool_call"
  id: string
  /** tool_call.id（用于 start/end 配对） */
  toolCallId?: string
  toolName: string
  status: ToolStatus
  /** 参数预览——args 摘要字符串 */
  argsPreview?: string
  startedAt?: number
  endedAt?: number
  durationMs?: number
  details?: unknown
}

export interface ToolResultItem extends ContentWarningAwareItem {
  kind: "tool_result"
  id: string
  toolName: string
  /** 对应 tool_call.id（可选配对） */
  toolCallId?: string
  status: "done" | "error"
  resultPreview?: string
  details?: unknown
}

export interface FileReadItem extends ContentWarningAwareItem {
  kind: "file_read"
  id: string
  /** view_file / list_files / write_file */
  toolName: "view_file" | "list_files" | "write_file" | string
  toolCallId?: string
  fileName?: string
  fileId?: string
  format?: string
  status: ToolStatus
  preview?: string
  details?: unknown
}

export interface SkillUsedItem {
  kind: "skill_used"
  id: string
  skillNames: string[]
  summary?: string
}

export interface MCPToolCallItem extends ContentWarningAwareItem {
  kind: "mcp_tool_call"
  id: string
  /** mcp__server__tool 解析出的 server 部分 */
  serverName?: string
  /** mcp__server__tool 解析出的原始 tool 部分 */
  toolName: string
  /** 完整 mcp__ name（用于配对） */
  toolCallId?: string
  status: ToolStatus
  argsPreview?: string
  resultPreview?: string
  details?: unknown
}

export interface ErrorItem {
  kind: "error"
  id: string
  message: string
  details?: unknown
}

export interface ContextSummaryItem extends ContentWarningAwareItem {
  kind: "context_summary"
  id: string
  content: string
  sourceMessageCount: number
  sourceTurnCount: number
  createdAt?: number
}

export interface ToolApprovalItem {
  kind: "tool_approval"
  id: string
  approvalId: string
  requestId: string
  sessionId: string | null
  toolCallId: string
  toolName: string
  toolLabel: string
  arguments: Record<string, unknown>
  reason: string | null
  policyName: string
  status: ToolApprovalStatus
  createdAt: string
  resolvedAt: string | null
  submitting?: boolean
  error?: string | null
}

export interface PlanRunItem {
  kind: "plan_run"
  id: string
  plan: PlanRun
  submitting?: boolean
  error?: string | null
}

export type ChatStreamItem =
  | UserMessageItem
  | AssistantMessageItem
  | TurnInfoItem
  | ContextSummaryItem
  | ToolCallItem
  | ToolResultItem
  | FileReadItem
  | SkillUsedItem
  | MCPToolCallItem
  | ToolApprovalItem
  | PlanRunItem
  | ErrorItem

/** AssistantMessageItem 或 TurnInfoItem 等可以被 history 模式复用。 */
export type HistoryItem =
  | UserMessageItem
  | AssistantMessageItem
