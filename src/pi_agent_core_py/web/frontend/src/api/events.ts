// Events API —— P1-B3 新增。
//
// GET /api/events 用于 WS reconnect replay / 缺失事件补播。
// 默认**不带 session_id**——sequence 是全局的，必须拉全部 envelope 才能维持
// 全局 cursor；session 过滤由 chatStore.handleEvent 内部决定。

import { requestJson } from "./client"
import type { WebEventEnvelope } from "../types"

export interface GetEventsOptions {
  /** 仅返回 sequence > after_sequence 的事件 */
  afterSequence?: number
  /** 最多返回 N 个事件（按 sequence 升序） */
  limit?: number
  /** 可选——session_id 过滤；replay 场景**不要**传此字段 */
  sessionId?: string
  /** 可选——request_id 过滤 */
  requestId?: string
}

export interface GetEventsResponse {
  count: number
  events: WebEventEnvelope[]
  first_available_sequence: number | null
  last_available_sequence: number | null
  has_more: boolean
  gap: boolean
}

/**
 * GET /api/events——查询事件 buffer。
 *
 * **关键约束**（用户原指令 §7.3 + B3-2）：
 * - replay 时**不要**传 sessionId——sequence 是全局的，必须拉全部 envelope
 * - chatStore.handleEvent 再用 activeSessionId 决定是否渲染
 * - 分页保护：limit=200，最多 20 页 / 4000 事件；超限 needsFinalResync
 */
export function getEvents(opts: GetEventsOptions = {}) {
  const query: Record<string, string | number> = {}
  if (opts.afterSequence !== undefined) {
    query.after_sequence = opts.afterSequence
  }
  if (opts.limit !== undefined) {
    query.limit = opts.limit
  }
  if (opts.sessionId !== undefined) {
    query.session_id = opts.sessionId
  }
  if (opts.requestId !== undefined) {
    query.request_id = opts.requestId
  }
  return requestJson<GetEventsResponse>("/api/events", {
    query: Object.keys(query).length > 0 ? query : undefined,
  })
}
