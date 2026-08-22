import { requestJson } from "./client"
import type {
  LatestSandboxOperationResponse,
  ManagedSandboxEventPage,
  ManagedSandboxOperation,
  SandboxDiff,
} from "../types/codingSandbox"

const BASE = "/api/coding-sandbox"

export function startSandboxOperation(sessionId: string): Promise<ManagedSandboxOperation> {
  return requestJson(`${BASE}/operations`, {
    method: "POST",
    body: { session_id: sessionId },
  })
}

export function getLatestSandboxOperation(
  sessionId: string,
): Promise<LatestSandboxOperationResponse> {
  return requestJson(`${BASE}/sessions/${encodeURIComponent(sessionId)}/operation`)
}

export function getSandboxOperation(operationId: string): Promise<ManagedSandboxOperation> {
  return requestJson(`${BASE}/operations/${encodeURIComponent(operationId)}`)
}

export function getSandboxEvents(
  operationId: string,
  afterSequence = 0,
): Promise<ManagedSandboxEventPage> {
  return requestJson(`${BASE}/operations/${encodeURIComponent(operationId)}/events`, {
    query: { after_sequence: afterSequence, limit: 200 },
  })
}

export function getSandboxDiff(operationId: string): Promise<SandboxDiff> {
  return requestJson(`${BASE}/operations/${encodeURIComponent(operationId)}/diff`)
}

function action(
  operationId: string,
  name: "validate" | "prepare-publish" | "publish" | "cancel" | "discard",
): Promise<ManagedSandboxOperation> {
  return requestJson(`${BASE}/operations/${encodeURIComponent(operationId)}/${name}`, {
    method: "POST",
  })
}

export const validateSandboxOperation = (operationId: string) => action(operationId, "validate")
export const prepareSandboxPublish = (operationId: string) => action(operationId, "prepare-publish")
export const publishSandboxOperation = (operationId: string) => action(operationId, "publish")
export const cancelSandboxOperation = (operationId: string) => action(operationId, "cancel")
export const discardSandboxOperation = (operationId: string) => action(operationId, "discard")
