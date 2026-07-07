// WebSocket / SSE 事件 + ChatStreamItem 类型。
//
// /ws/events 是推荐实时通道；后端发过来的 event 形状宽松——前端按 type 字段分发。
// AgentEvent 的 13 种 type 来自 src/pi_agent_core_py/events.py。

import type { AgentMessage } from "./messages"
import type { FileRef } from "./files"

/**
 * 后端推过来的任意事件——形状宽松。
 *
 * 后端会发：
 *   - hello：连接建立时第一个事件
 *   - shutdown：服务端关闭通知
 *   - 13 种 AgentEvent：type 字段对应后端事件类型
 *     agent_start / agent_end / turn_start / turn_end
 *     message_start / message_update / message_end
 *     tool_execution_start / tool_execution_end
 *     request_queued / request_start / request_end / agent_abort
 */
export interface WebEvent {
  type: string
  [key: string]: any
}

// ============================================================================
// 状态枚举
// ============================================================================

export type ToolStatus = "running" | "done" | "error"
export type TurnStatus = "queued" | "running" | "done" | "error"

// ============================================================================
// ChatStreamItem —— 中间消息流的统一 item 类型（9 种 kind）
// ============================================================================

export interface UserMessageItem {
  kind: "user_message"
  id: string
  /** 用户输入的文本（多块 TextContent 合并后） */
  content: string
  /** 本轮附件——FileChip 渲染用（Step 6 启用） */
  files?: FileRef[]
}

export interface AssistantMessageItem {
  kind: "assistant_message"
  id: string
  content: string
  /** 流式进行中——true 时显示 stream cursor */
  streaming?: boolean
}

export interface TurnInfoItem {
  kind: "turn_info"
  id: string
  title: string
  summary: string
  status?: TurnStatus
  /** 展开后显示的额外信息（事件计数 / request_id / duration 等） */
  details?: unknown
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

export interface ToolResultItem {
  kind: "tool_result"
  id: string
  toolName: string
  /** 对应 tool_call.id（可选配对） */
  toolCallId?: string
  status: "done" | "error"
  resultPreview?: string
  details?: unknown
}

export interface FileReadItem {
  kind: "file_read"
  id: string
  /** view_file / list_files */
  toolName: "view_file" | "list_files" | string
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

export interface MCPToolCallItem {
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

export type ChatStreamItem =
  | UserMessageItem
  | AssistantMessageItem
  | TurnInfoItem
  | ToolCallItem
  | ToolResultItem
  | FileReadItem
  | SkillUsedItem
  | MCPToolCallItem
  | ErrorItem

/** AssistantMessageItem 或 TurnInfoItem 等可以被 history 模式复用。 */
export type HistoryItem =
  | UserMessageItem
  | AssistantMessageItem
