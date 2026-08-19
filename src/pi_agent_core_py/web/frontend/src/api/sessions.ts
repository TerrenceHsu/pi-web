// Sessions API——P0-1 sqlite session 多会话管理。

import type {
  CreateSessionRequest,
  BranchSessionRequest,
  ForkSessionRequest,
  RenameSessionRequest,
  SessionLane,
  SessionListResponse,
  SessionSummary,
  SessionTreeResponse,
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

/** GET append-only entry path；includeAll=true 时附完整树。 */
export function getSessionTree(
  sessionId: string,
  options: { lane?: string; includeAll?: boolean } = {},
) {
  const params = new URLSearchParams()
  if (options.lane) params.set("lane", options.lane)
  if (options.includeAll) params.set("include_all", "true")
  const query = params.size ? `?${params.toString()}` : ""
  return requestJson<SessionTreeResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/tree${query}`,
  )
}

/** 从指定 entry（省略时为 source lane leaf）创建 fork。 */
export function forkSession(sessionId: string, payload: ForkSessionRequest) {
  return requestJson<{ lane: SessionLane }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/fork`,
    { method: "POST", body: payload },
  )
}

/** 把 lane leaf 移回当前路径上的 entry；null 表示根。 */
export function branchSession(sessionId: string, payload: BranchSessionRequest) {
  return requestJson<{ lane: SessionLane }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/branch`,
    { method: "POST", body: payload },
  )
}

/** 切换 active lane，并让 legacy messages 投影跟随。 */
export function setActiveLane(sessionId: string, lane: string) {
  return requestJson<{ lane: SessionLane }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/active-lane`,
    { method: "PATCH", body: { lane } },
  )
}

/** 写入 append-only label fact；null 清除当前 label。 */
export function setEntryLabel(
  sessionId: string,
  entryId: string,
  label: string | null,
) {
  return requestJson<{ entry_id: string; label: string | null }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/entries/${encodeURIComponent(entryId)}/label`,
    { method: "PUT", body: { label } },
  )
}

/**
 * P1-D1: GET /api/sessions/{sid}/export/markdown——导出 session 为 Markdown。
 *
 * 返回 blob + filename（从 Content-Disposition 提取）。
 * 调用方用 downloadBlob 触发浏览器下载。
 */
export function exportMarkdown(sessionId: string) {
  // 动态 import 避免循环依赖
  return import("./client").then(({ requestBlob }) =>
    requestBlob(`/api/sessions/${encodeURIComponent(sessionId)}/export/markdown`),
  )
}
