export type ManagedSandboxStatus =
  | "creating"
  | "ready"
  | "validating"
  | "validation_failed"
  | "validated"
  | "freezing"
  | "awaiting_approval"
  | "publishing"
  | "published"
  | "cancelling"
  | "cancelled"
  | "discarding"
  | "discarded"
  | "failed"
  | "interrupted"

export interface SandboxDiffEntry {
  path: string
  status: "added" | "modified" | "deleted"
  before_sha256: string | null
  after_sha256: string | null
}

export interface SandboxDiff {
  entries: SandboxDiffEntry[]
  patch: string
  patch_truncated: boolean
}

export interface SandboxValidationCheck {
  check_id: string
  argv: string[]
  cwd: string
  timeout_seconds: number
  status:
    "not_run" | "passed" | "failed" | "timed_out" | "cancelled" | "sandbox_lost" | "execution_error"
  exit_code: number | null
  duration_ms: number
  stdout: string
  stderr: string
  output_truncated: boolean
}

export interface SandboxValidationEvidence {
  evidence_id: string
  workspace_revision: number
  checks: SandboxValidationCheck[]
  passed: boolean
  failure_code: string | null
  duration_ms: number
}

export interface ManagedSandboxOperation {
  schema_version: "pi-agent-managed-sandbox-operation/v1"
  operation_id: string
  session_id: string
  status: ManagedSandboxStatus
  config_revision: number
  created_at_ms: number
  updated_at_ms: number
  workspace_revision: number
  baseline_archive_sha256: string | null
  baseline_manifest_sha256: string | null
  validation: SandboxValidationEvidence | null
  diff: SandboxDiff | null
  artifact_id: string | null
  artifact_sha256: string | null
  publish_transaction_id: string | null
  changed_paths: string[]
  deleted_paths: string[]
  error_code: string | null
  terminal: boolean
  cancellable: boolean
  approval_required: boolean
}

export interface ManagedSandboxEvent {
  schema_version: "pi-agent-managed-sandbox-event/v1"
  operation_id: string
  session_id: string
  sequence: number
  event_type: string
  recorded_at_ms: number
  payload: Record<string, unknown>
}

export interface ManagedSandboxEventPage {
  events: ManagedSandboxEvent[]
  first_available_sequence: number | null
  last_available_sequence: number | null
  has_more: boolean
  gap: boolean
}

export interface LatestSandboxOperationResponse {
  operation: ManagedSandboxOperation | null
}
