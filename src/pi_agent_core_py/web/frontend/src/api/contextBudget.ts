import type {
  ContextBudgetEstimateRequest,
  ContextBudgetResponse,
  ContextCompactionResponse,
} from "../types"
import { requestJson } from "./client"

export function getContextBudget(sessionId: string): Promise<ContextBudgetResponse> {
  return requestJson<ContextBudgetResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context-budget`,
  )
}

export function estimateContextBudget(
  sessionId: string,
  payload: ContextBudgetEstimateRequest,
): Promise<ContextBudgetResponse> {
  return requestJson<ContextBudgetResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context-budget/estimate`,
    { method: "POST", body: payload },
  )
}

export function compactContext(
  sessionId: string,
  keepLastNTurns = 4,
  keepRecentTokens?: number,
): Promise<ContextCompactionResponse> {
  return requestJson<ContextCompactionResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context/compact`,
    {
      method: "POST",
      body: {
        keep_last_n_turns: keepLastNTurns,
        ...(keepRecentTokens === undefined
          ? {}
          : { keep_recent_tokens: keepRecentTokens }),
      },
    },
  )
}
