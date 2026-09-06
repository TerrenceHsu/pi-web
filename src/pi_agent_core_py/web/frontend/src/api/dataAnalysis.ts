import { requestJson } from "./client"

export interface AnalysisResult {
  schema_version: string
  run_id: string
  session_id: string
  action: string
  source_name: string
  source_logical_path: string
  source_sha256: string
  source_rows: number
  analyzed_rows: number
  result_rows: number
  exported_rows: number
  preview_rows: number
  limited: boolean
  preview_limited: boolean
  columns: string[]
  rows: (string | number | boolean | null)[][]
  warnings: string[]
  source: { sheet?: string | null; sheets?: string[] }
  has_chart: boolean
  null_policy: string
  numeric_policy: string
  code?: string
  code_sha256?: string
  stdout?: string
  python_error?: string | null
}

export interface AnalysisRun {
  id: string
  session_id: string
  status: "queued" | "awaiting_approval" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted"
  error_code: string | null
  created_at: number
  result?: AnalysisResult | null
  action?: string
}

export const analysisBase = (sessionId: string) =>
  `/api/workspaces/${encodeURIComponent(sessionId)}/analysis`

export const analysisRunUrl = (sessionId: string, runId: string) =>
  `${analysisBase(sessionId)}/${encodeURIComponent(runId)}`

export const listAnalysis = (sessionId: string) =>
  requestJson<{ runs: AnalysisRun[] }>(analysisBase(sessionId))

export const getAnalysis = (sessionId: string, runId: string) =>
  requestJson<AnalysisRun>(analysisRunUrl(sessionId, runId))

export const startAnalysis = (sessionId: string, request: Record<string, unknown>) =>
  requestJson<AnalysisRun>(analysisBase(sessionId), { method: "POST", body: request })

export const analysisAction = (sessionId: string, runId: string, action: "cancel" | "retry") =>
  requestJson<AnalysisRun>(`${analysisRunUrl(sessionId, runId)}/${action}`, { method: "POST" })

export const saveAnalysis = (sessionId: string, runId: string) =>
  requestJson<{ saved: boolean }>(`${analysisRunUrl(sessionId, runId)}/save`, { method: "POST" })

export const deleteAnalysis = (sessionId: string, runId: string) =>
  requestJson<{ deleted: boolean }>(analysisRunUrl(sessionId, runId), { method: "DELETE" })
