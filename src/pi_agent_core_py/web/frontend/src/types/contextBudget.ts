import type { IntentAudit, IntentMode } from "./messages"

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
  effective_input_budget?: number | null
  effective_ratio?: number | null
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
  workspace_context: Record<string, unknown> | null
  intent: IntentAudit | null
  compaction?: ContextCompactionStatus | null
}

export interface ContextCompactionTokenStats {
  message_tokens_before?: number
  message_tokens_after?: number
  estimated_input_tokens_before?: number
  estimated_input_tokens_after?: number
  projected_tokens_before?: number
  projected_tokens_after?: number
  context_window?: number | null
  reserved_output_tokens?: number
  input_ratio_before?: number | null
  input_ratio_after?: number | null
  projected_ratio_before?: number | null
  projected_ratio_after?: number | null
  approximate?: boolean
  estimator_version?: string
}

export interface ContextCompactionStatus {
  auto_compact: boolean
  status: string
  active_projection_id: string | null
  covered_message_count: number
  token_stats: ContextCompactionTokenStats | null
  error_code: string | null
  circuit_open?: boolean
  can_auto_compact?: boolean
  summary_text?: string | null
  source_entry_ids?: string[]
}

export interface ContextSourcePage {
  entry_id: string
  text: string
  offset: number
  next_offset: number | null
  total_chars: number
}

export interface ContextBudgetEstimateRequest {
  text?: string
  file_ids?: string[]
  skill_names?: string[]
  coding_mode?: boolean
  intent_mode?: IntentMode
}

export interface ContextCompactionResponse {
  ok: true
  session_id: string
  summary_message: Record<string, unknown>
  source_message_count: number
  compacted_message_count: number
  retained_message_count: number
  snapshots_retained: number
  token_stats: {
    message_tokens_before: number
    message_tokens_after: number
    estimated_input_tokens_before: number
    estimated_input_tokens_after: number
    projected_tokens_before: number
    projected_tokens_after: number
    context_window: number | null
    reserved_output_tokens: number
    input_ratio_before: number | null
    input_ratio_after: number | null
    projected_ratio_before: number | null
    projected_ratio_after: number | null
    approximate: boolean
    estimator_version: string
  } | null
  budget_before: ContextBudgetResponse
  budget: ContextBudgetResponse
}
