export type TelemetryStatus = "running" | "ok" | "error"
export type TelemetryOutcome = "completed" | "error" | "aborted" | "running"
export type TelemetryAttributes = Record<string, string | number | boolean | unknown[] | null>

export interface TelemetryRequestSummary {
  total: number
  completed: number
  error: number
  aborted: number
  running: number
  error_rate: number
  average_duration_ms: number
  p95_duration_ms: number
}

export interface TelemetryUsageSummary {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cost: number
}

export interface TelemetryToolSummary {
  calls: number
  errors: number
  top: Array<{ name: string; calls: number }>
}

export interface TelemetryTimelineBucket {
  timestamp_ms: number
  total: number
  completed: number
  error: number
  aborted: number
  running: number
}

export interface TelemetrySummary {
  since_ms: number
  generated_at_ms: number
  requests: TelemetryRequestSummary
  usage: TelemetryUsageSummary
  tools: TelemetryToolSummary
  providers: Array<{ name: string; requests: number }>
  accounts: Array<{ id: string; name: string }>
  timeline: TelemetryTimelineBucket[]
}

export interface TelemetryErrorInfo {
  name: string
  message: string | null
}

export interface TelemetrySpanSummary {
  id: string
  trace_id: string
  parent_id: string | null
  name: string
  started_at_ms: number
  ended_at_ms: number | null
  duration_ms: number | null
  status: TelemetryStatus
  error: TelemetryErrorInfo | null
  attributes: TelemetryAttributes
}

export interface TelemetryEvent {
  name: string
  timestamp_ms: number
  attributes: TelemetryAttributes
}

export interface TelemetrySpanDetail extends TelemetrySpanSummary {
  events: TelemetryEvent[]
}

export interface TelemetrySpanListResponse {
  spans: TelemetrySpanSummary[]
  count: number
}
