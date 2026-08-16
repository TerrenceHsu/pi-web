export type ToolApprovalStatus = "pending" | "approved" | "denied" | "cancelled"
export type ToolApprovalDecision = "approve" | "deny"

export interface ToolApprovalRecord {
  approval_id: string
  request_id: string
  session_id: string | null
  tool_call_id: string
  tool_name: string
  tool_label: string
  arguments: Record<string, unknown>
  reason: string | null
  policy_name: string
  policy_metadata: Record<string, unknown>
  status: ToolApprovalStatus
  created_at: string
  resolved_at: string | null
}

export interface ToolApprovalListResponse {
  request_id: string
  session_id: string | null
  count: number
  approvals: ToolApprovalRecord[]
}

export interface ToolApprovalResolveResponse {
  ok: boolean
  request_id: string
  session_id: string | null
  approval: ToolApprovalRecord
  idempotent: boolean
}
