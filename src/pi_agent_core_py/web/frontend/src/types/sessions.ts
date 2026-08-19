// Sessions 类型（P0-1 sqlite session + P0-4 多会话 UI）。
//
// /api/sessions 复数 spec endpoint 返回 SessionListResponse；
// /api/session 单数旧 endpoint 返回 SessionResponse（保留以兼容 DeveloperDrawer）。

import type { JsonValue } from "./state"

/** GET /api/sessions 中单条 session 摘要。 */
export interface SessionSummary {
  id: string
  title: string
  created_at: number
  updated_at: number
  metadata: JsonValue
  /** 当前 legacy messages 投影对应的 lane。 */
  active_lane: string
  /** 是否是当前 active session——后端在 list 时回填。 */
  is_current?: boolean
}

/** GET /api/sessions response。 */
export interface SessionListResponse {
  count: number
  sessions: SessionSummary[]
}

/** POST /api/sessions body。 */
export interface CreateSessionRequest {
  title?: string
  metadata?: Record<string, JsonValue>
}

/** PATCH /api/sessions/{sid} body。 */
export interface RenameSessionRequest {
  title: string
}

/** Append-only Session tree 中的 immutable entry。 */
export interface SessionTreeEntry {
  id: string
  session_id: string
  seq: number
  parent_id: string | null
  message_id: string
  role: string
  message: JsonValue
  created_at: number
  label: string | null
}

/** 命名 lane 与其 active leaf。 */
export interface SessionLane {
  session_id: string
  name: string
  leaf_entry_id: string | null
  created_at: number
  updated_at: number
  is_active: boolean
}

export interface SessionTreeResponse {
  session_id: string
  active_lane: string
  lane: string
  leaf_entry_id: string | null
  lanes: SessionLane[]
  entries: SessionTreeEntry[]
  all_entries?: SessionTreeEntry[]
}

export interface ForkSessionRequest {
  name: string
  at_entry_id?: string | null
  source_lane?: string
  activate?: boolean
}

export interface BranchSessionRequest {
  entry_id: string | null
  lane?: string
}

/**
 * 旧 GET /api/session response（单数 endpoint，向后兼容）。
 *
 * 字段宽松——后端在不同 attached 状态下返回不同字段。新 UI 用 SessionSummary；
 * DeveloperDrawer 仍用此类型。
 */
export interface SessionResponse {
  attached: boolean
  id?: string
  title?: string | null
  turn_count?: number
  message_count?: number
  snapshot_count?: number
  metadata?: JsonValue
  messages?: JsonValue[]
  /** 新字段：list endpoint 中复用此类型时回填 */
  created_at?: number
  updated_at?: number
  active_lane?: string
  is_current?: boolean
}
