import { requestJson } from "./client"
import type {
  TelemetrySpanDetail,
  TelemetrySpanListResponse,
  TelemetryStatus,
  TelemetrySummary,
} from "../types/telemetry"

export function getTelemetrySummary(windowHours: number): Promise<TelemetrySummary> {
  return requestJson<TelemetrySummary>("/api/admin/telemetry/summary", {
    query: { window_hours: windowHours },
  })
}

export function listTelemetrySpans(options: {
  windowHours: number
  limit?: number
  status?: TelemetryStatus
  accountId?: string
  sessionId?: string
}): Promise<TelemetrySpanListResponse> {
  return requestJson<TelemetrySpanListResponse>("/api/admin/telemetry/spans", {
    query: {
      window_hours: options.windowHours,
      limit: options.limit ?? 100,
      status: options.status,
      account_id: options.accountId,
      session_id: options.sessionId,
      name: "web.request",
    },
  })
}

export function getTelemetrySpan(spanId: string): Promise<TelemetrySpanDetail> {
  return requestJson<TelemetrySpanDetail>(
    `/api/admin/telemetry/spans/${encodeURIComponent(spanId)}`,
  )
}
