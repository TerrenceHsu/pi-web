// Messages / Prompt API。

import type {
  MessagesResponse,
  PromptAsyncResponse,
  PromptRequest,
  PromptResponse,
  RequestListResponse,
  RequestSummary,
} from "../types"
import { requestJson } from "./client"

/**
 * POST /api/prompt——同步阻塞触发一次 prompt。
 *
 * 抛 ApiError 的情况（store 用 instanceof 分发）：
 *   - 400：text 为空 / file_ids 类型错 / skill_names 含空字符串 / Unknown skill
 *   - 403：file 跨 session
 *   - 404：session / file_id 不存在
 *   - 409：harness 不 idle（已有 prompt 在跑）
 *   - 500：harness 异常（last_error 含详情）
 *   - 503：file_store 未启用且 body 含 file_ids
 *
 * **P1-B3**：chatStore 默认不再调用此函数——改用 sendPromptAsync。
 * 保留作 fallback / 测试用途。
 */
export function sendPrompt(payload: PromptRequest) {
  return requestJson<PromptResponse>("/api/prompt", {
    method: "POST",
    body: payload,
  })
}

/**
 * POST /api/prompt/async（P1-B1）——立即返回 202 + request_id。
 *
 * 抛 ApiError 同 sendPrompt；额外：
 *   - 503：server shutting_down
 *
 * 调用方收到 202 后：set currentRequestId → 等 WS event 实时驱动 assistant draft。
 */
export function sendPromptAsync(payload: PromptRequest) {
  return requestJson<PromptAsyncResponse>("/api/prompt/async", {
    method: "POST",
    body: payload,
  })
}

/** GET /api/requests/{request_id}——查询 request lifecycle 状态。404 抛 ApiError。 */
export function getRequestStatus(requestId: string) {
  return requestJson<RequestSummary>(`/api/requests/${encodeURIComponent(requestId)}`)
}

/**
 * POST /api/requests/{request_id}/abort——幂等 abort。
 *
 * queued: cancel task；running: harness.abort + 等 finalize；
 * completed/error/aborted: 幂等返回当前状态。
 */
export function abortRequest(requestId: string, reason?: string) {
  return requestJson<{
    ok: boolean
    request_id: string
    status: string
    abort_reason?: string | null
    error?: string
  }>(`/api/requests/${encodeURIComponent(requestId)}/abort`, {
    method: "POST",
    body: reason ? { reason } : {},
  })
}

/**
 * GET /api/requests?session_id=&status=active&limit=N（P1-B3-3）。
 *
 * 用于页面刷新恢复——查 active session 是否有未完成 request。
 */
export function listActiveRequests(sessionId: string, limit = 1) {
  return requestJson<RequestListResponse>("/api/requests", {
    query: {
      session_id: sessionId,
      status: "active",
      limit,
    },
  })
}

/** GET /api/messages?session_id=...——不传则用当前 agent.state.messages。 */
export function getMessages(sessionId?: string) {
  return requestJson<MessagesResponse>("/api/messages", {
    query: sessionId ? { session_id: sessionId } : undefined,
  })
}
