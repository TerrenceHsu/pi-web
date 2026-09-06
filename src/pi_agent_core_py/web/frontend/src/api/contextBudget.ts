import type {
  ContextBudgetEstimateRequest,
  ContextBudgetResponse,
  ContextCompactionResponse,
  ContextCompactionStatus,
  ContextSourcePage,
} from "../types"
import { requestJson } from "./client"

export function getContextBudget(sessionId: string): Promise<ContextBudgetResponse> {
  return requestJson<ContextBudgetResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context-budget`,
  )
}

export function getContextCompaction(sessionId: string): Promise<ContextCompactionStatus> {
  return requestJson<ContextCompactionStatus>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context/compaction`,
  )
}

export function setContextAutoCompaction(
  sessionId: string,
  autoCompact: boolean,
): Promise<ContextCompactionStatus> {
  return requestJson<ContextCompactionStatus>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context/compaction`,
    { method: "PUT", body: { auto_compact: autoCompact } },
  )
}

export function getContextSource(
  sessionId: string,
  entryId: string,
  offset = 0,
): Promise<ContextSourcePage> {
  return requestJson<ContextSourcePage>(
    `/api/sessions/${encodeURIComponent(sessionId)}/context/source/${encodeURIComponent(entryId)}`,
    { query: { offset, max_chars: 6000 } },
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
