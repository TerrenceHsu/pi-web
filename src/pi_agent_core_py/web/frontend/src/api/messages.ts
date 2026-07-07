// Messages / Prompt API。

import type { MessagesResponse, PromptRequest, PromptResponse } from "../types"
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
 */
export function sendPrompt(payload: PromptRequest) {
  return requestJson<PromptResponse>("/api/prompt", {
    method: "POST",
    body: payload,
  })
}

/** GET /api/messages?session_id=...——不传则用当前 agent.state.messages。 */
export function getMessages(sessionId?: string) {
  return requestJson<MessagesResponse>("/api/messages", {
    query: sessionId ? { session_id: sessionId } : undefined,
  })
}
