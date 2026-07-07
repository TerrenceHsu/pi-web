// Sessions API——P0-1 sqlite session 多会话管理。

import type {
  CreateSessionRequest,
  RenameSessionRequest,
  SessionListResponse,
  SessionSummary,
} from "../types"
import { requestJson } from "./client"

/** GET /api/sessions——复数 spec endpoint。 */
export function listSessions() {
  return requestJson<SessionListResponse>("/api/sessions")
}

/** GET /api/sessions/{id}（不存在时后端返回 404）。 */
export function getSession(id: string) {
  return requestJson<SessionSummary>(`/api/sessions/${encodeURIComponent(id)}`)
}

/** POST /api/sessions——body.title / body.metadata 可选。 */
export function createSession(payload: CreateSessionRequest = {}) {
  return requestJson<SessionSummary>("/api/sessions", {
    method: "POST",
    body: payload,
  })
}

/** PATCH /api/sessions/{sid}——重命名。 */
export function renameSession(sessionId: string, title: string) {
  const body: RenameSessionRequest = { title }
  return requestJson<SessionSummary>(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    { method: "PATCH", body },
  )
}

/** DELETE /api/sessions/{sid}——级联删除 messages / snapshots / 上传文件。 */
export function deleteSession(sessionId: string) {
  return requestJson<{ ok: boolean; deleted_files: number }>(
    `/api/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  )
}
