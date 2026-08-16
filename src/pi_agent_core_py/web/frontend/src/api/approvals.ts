import type {
  ToolApprovalDecision,
  ToolApprovalListResponse,
  ToolApprovalResolveResponse,
} from "../types"
import { requestJson } from "./client"

export function listRequestApprovals(requestId: string, status = "pending") {
  return requestJson<ToolApprovalListResponse>(
    `/api/requests/${encodeURIComponent(requestId)}/approvals`,
    { query: { status } },
  )
}

export function resolveToolApproval(
  requestId: string,
  approvalId: string,
  decision: ToolApprovalDecision,
) {
  return requestJson<ToolApprovalResolveResponse>(
    `/api/requests/${encodeURIComponent(requestId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: "POST", body: { decision } },
  )
}
