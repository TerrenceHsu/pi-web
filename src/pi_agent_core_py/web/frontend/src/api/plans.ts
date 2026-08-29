import type { PlanApprovalResponse, PlanRunResponse } from "../types/plans"
import { requestJson } from "./client"

export function getLatestPlanRun(sessionId: string) {
  return requestJson<PlanRunResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/plan-runs/latest`,
  )
}

export function approvePlanRun(runId: string) {
  return requestJson<PlanApprovalResponse>(
    `/api/plan-runs/${encodeURIComponent(runId)}/approve`,
    { method: "POST" },
  )
}
