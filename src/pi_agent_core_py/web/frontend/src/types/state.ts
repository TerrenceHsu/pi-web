// 旧 DeveloperDrawer / InspectorPanel 用的类型（保留以向后兼容）。
// 这些类型对应 v0.0.22 spec endpoint：/api/state /api/events /api/snapshots
// /api/policy/audit 等。新 UI Step 5 之后会逐步淡出。

export type JsonValue = any

export interface AgentStateSummary {
  running: boolean
  last_error: string | null
  agent_status: string
  queue_size: number
  turn_count: number
  message_count: number
  snapshot_count: number
  event_count: number
}

export interface EventsResponse {
  count: number
  events: JsonValue[]
}

export interface SnapshotSummary {
  index: number
  id: string | null
  request_type: string | null
  status: string
  error: string | null
  started_at: number | null
  ended_at: number | null
  duration_ms: number | null
  messages_before_count: number
  messages_after_count: number
  events_count: number
  tool_calls_count: number
  tool_results_count: number
  metadata: JsonValue
}

export interface SnapshotsResponse {
  count: number
  snapshots: SnapshotSummary[]
}

export interface PolicyAuditResponse {
  policy_name: string | null
  count: number
  records: JsonValue[]
}
