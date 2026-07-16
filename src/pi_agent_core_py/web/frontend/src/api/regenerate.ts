// D2-5: Regenerate API client。

import type {
  RegenerateResponse,
  RevisionListResponse,
} from "../types"
import { requestJson } from "./client"

/**
 * POST /api/sessions/{sid}/messages/{aid}/regenerate——触发 regenerate。
 *
 * 立即返回 202 + regeneration_id（== revision.id）+ request_id。
 *
 * 错误码（稳定 code，详见后端 _regen_error_response）：
 *   - 404 session_not_found / message_not_found
 *   - 400 regenerate_target_not_assistant
 *   - 409 regenerate_target_not_latest / regenerate_missing_user_message /
 *          request_already_active / revision_already_running / request_conflict
 *   - 503 extension_store_unavailable
 *   - 500 regenerate_start_failed
 */
export function regenerateMessage(
  sessionId: string,
  assistantMessageId: string,
) {
  return requestJson<RegenerateResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}` +
      `/messages/${encodeURIComponent(assistantMessageId)}/regenerate`,
    {
      method: "POST",
      body: {},
    },
  )
}

/**
 * GET /api/sessions/{sid}/messages/{aid}/revisions——列出 revision 历史。
 *
 * 安全 serializer——响应**不含** content_json / base_content_sha256 / request_id。
 * D2-7 UI 暂不调用此函数；保留以便 D2-8+ 实现 revision history drawer。
 */
export function listMessageRevisions(
  sessionId: string,
  assistantMessageId: string,
  params?: { limit?: number; before_revision_number?: number },
) {
  const query: Record<string, string | number> = {}
  if (params?.limit !== undefined) query.limit = params.limit
  if (params?.before_revision_number !== undefined) {
    query.before_revision_number = params.before_revision_number
  }
  return requestJson<RevisionListResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}` +
      `/messages/${encodeURIComponent(assistantMessageId)}/revisions`,
    Object.keys(query).length > 0 ? { query } : undefined,
  )
}
