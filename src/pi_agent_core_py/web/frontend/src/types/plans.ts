export type PlanRunStatus =
  | "planning"
  | "awaiting_plan_approval"
  | "executing"
  | "verifying"
  | "awaiting_artifact_approval"
  | "completed"
  | "blocked"
  | "failed"
  | "cancelled"
  | "interrupted"

export type PlanTaskStatus =
  | "pending"
  | "executing"
  | "awaiting_verification"
  | "passed"
  | "failed"
  | "blocked"

export type VerificationFailureClass =
  | "retry_executor"
  | "replan_required"
  | "user_input_required"

export interface TaskExecutionReport {
  summary: string
  changed_paths: string[]
  validation_summary: string
}

export interface TaskBlockedReport {
  reason: string
  suggestions: string[]
}

export interface VerificationReport {
  passed: boolean
  reason: string
  suggestions: string[]
  classification: VerificationFailureClass | null
}

export interface PlanTask {
  id: string
  ordinal: number
  title: string
  objective: string
  dependencies: string[]
  acceptance_criteria: string[]
  allowed_paths: string[]
  status: PlanTaskStatus
  attempt: number
  execution: TaskExecutionReport | null
  verification: VerificationReport | null
  blocked: TaskBlockedReport | null
}

export interface PlanRun {
  id: string
  session_id: string
  request_id: string
  goal: string
  status: PlanRunStatus
  plan_version: number
  summary: string | null
  sandbox_operation_id: string | null
  artifact_id: string | null
  failure_code: string | null
  tasks: PlanTask[]
  created_at_ms: number
  updated_at_ms: number
}

export interface PlanRunResponse {
  plan: PlanRun | null
}

export interface PlanApprovalResponse {
  ok: boolean
  idempotent: boolean
  plan: PlanRun
}
