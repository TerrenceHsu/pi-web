// Chat controls need only the current runtime summary and abort endpoint.
import type { AgentStateSummary } from "../types"
import { requestJson } from "./client"

export function getState() {
  return requestJson<AgentStateSummary>("/api/state")
}

/** POST /api/abort——中止当前 in-flight prompt。 */
export function abortRun(reason?: string) {
  return requestJson<{ ok: boolean; error?: string }>("/api/abort", {
    method: "POST",
    body: reason ? { reason } : {},
  })
}
