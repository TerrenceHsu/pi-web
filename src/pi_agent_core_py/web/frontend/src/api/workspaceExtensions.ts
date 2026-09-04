import type { WorkspaceExtensionsResponse, WorkspaceExtensionsUpdate } from "../types"
import { requestJson } from "./client"

export function getWorkspaceExtensions(sessionId: string) {
  return requestJson<WorkspaceExtensionsResponse>(
    `/api/workspaces/${encodeURIComponent(sessionId)}/extensions`,
  )
}

export function updateWorkspaceExtensions(sessionId: string, payload: WorkspaceExtensionsUpdate) {
  return requestJson<WorkspaceExtensionsResponse>(
    `/api/workspaces/${encodeURIComponent(sessionId)}/extensions`,
    { method: "PUT", body: payload },
  )
}
