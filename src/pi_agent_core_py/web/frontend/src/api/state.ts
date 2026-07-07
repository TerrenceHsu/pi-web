// 旧 DeveloperDrawer / InspectorPanel 用的 endpoint——保留以向后兼容。
//
// 这些对应 v0.0.22 baseline：/api/state /api/events /api/snapshots /api/mcp
// /api/policy/audit /api/abort /api/reset 等。Step 5 之后的新 UI 会逐步淡出。

import type {
  AgentStateSummary,
  EventsResponse,
  McpResponse,
  PolicyAuditResponse,
  SnapshotsResponse,
} from "../types"
import { requestJson } from "./client"

export function getState() {
  return requestJson<AgentStateSummary>("/api/state")
}

export function getEvents() {
  return requestJson<EventsResponse>("/api/events")
}

export function clearEvents() {
  return requestJson<{ ok: boolean; count: number }>("/api/events/clear", {
    method: "POST",
    body: {},
  })
}

export function getSnapshots() {
  return requestJson<SnapshotsResponse>("/api/snapshots")
}

export function getSnapshot(index: number) {
  return requestJson<any>(`/api/snapshots/${index}`)
}

/** 旧 GET /api/session——单数。 */
export function getSessionLegacy() {
  return requestJson<any>("/api/session")
}

/** 旧 GET /api/mcp——含 servers + tools + prompts。 */
export function getMcp() {
  return requestJson<McpResponse>("/api/mcp")
}

export function getPolicyAudit(limit = 100) {
  return requestJson<PolicyAuditResponse>(`/api/policy/audit`, {
    query: { limit },
  })
}

/** POST /api/abort——中止当前 in-flight prompt。 */
export function abortRun(reason?: string) {
  return requestJson<{ ok: boolean; error?: string }>("/api/abort", {
    method: "POST",
    body: reason ? { reason } : {},
  })
}

/** POST /api/reset——重置 agent state + 可选清 events / snapshots / audit。 */
export function resetState(options?: {
  clear_events?: boolean
  clear_snapshots?: boolean
  clear_audit?: boolean
}) {
  return requestJson<{ ok: boolean; cleared: any; error?: string }>("/api/reset", {
    method: "POST",
    body: options ?? {},
  })
}
