export type ContextBudgetLevel = "unknown" | "normal" | "warning" | "compact" | "blocked"

export interface ContextEstimate {
  system_prompt_tokens: number
  message_tokens: number
  tool_definition_tokens: number
  estimated_input_tokens: number
  reserved_output_tokens: number
  projected_tokens: number
  context_window: number | null
  input_ratio: number | null
  projected_ratio: number | null
  level: ContextBudgetLevel
  can_send: boolean
  approximate: true
  estimator_version: string
}

export interface ContextBudgetResponse {
  session_id: string
  provider_id: string
  model_id: string
  capability_source: "user" | "static" | "unknown"
  estimate: ContextEstimate
}

export interface ContextBudgetEstimateRequest {
  text?: string
  file_ids?: string[]
  skill_names?: string[]
}

export interface ContextCompactionResponse {
  ok: true
  session_id: string
  summary_message: Record<string, unknown>
  source_message_count: number
  compacted_message_count: number
  retained_message_count: number
  snapshots_retained: number
  budget: ContextBudgetResponse
}
