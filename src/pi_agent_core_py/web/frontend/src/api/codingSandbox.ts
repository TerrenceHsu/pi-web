import { downloadBlob, requestBlob, requestJson } from "./client"
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

function sandboxArtifactFilePath(operationId: string, logicalPath: string): string {
  const query = new URLSearchParams({ path: logicalPath })
  return `${BASE}/operations/${encodeURIComponent(operationId)}/artifact-file?${query}`
}

export async function readSandboxArtifactFile(
  operationId: string,
  logicalPath: string,
): Promise<string> {
  const { blob } = await requestBlob(sandboxArtifactFilePath(operationId, logicalPath))
  return blob.text()
}

export async function downloadSandboxArtifactFile(
  operationId: string,
  logicalPath: string,
): Promise<void> {
  const fallback = logicalPath.split("/").at(-1) || "sandbox-file"
  const { blob, filename } = await requestBlob(sandboxArtifactFilePath(operationId, logicalPath))
  downloadBlob(blob, filename || fallback)
}

function action(
  operationId: string,
  name:
    | "validate"
    | "prepare-publish"
    | "publish"
    | "refreeze"
    | "retry-publish"
    | "cancel"
    | "discard",
  approval?: { artifact_id: string; artifact_sha256: string; review_sha256: string },
): Promise<ManagedSandboxOperation> {
  return requestJson(`${BASE}/operations/${encodeURIComponent(operationId)}/${name}`, {
    method: "POST",
    body: approval,
  })
}

export const validateSandboxOperation = (operationId: string) => action(operationId, "validate")
export const prepareSandboxPublish = (operationId: string) => action(operationId, "prepare-publish")
export const publishSandboxOperation = (operationId: string, approval?: Parameters<typeof action>[2]) => action(operationId, "publish", approval)
export const refreezeSandboxOperation = (operationId: string) => action(operationId, "refreeze")
export const retrySandboxPublish = (operationId: string, approval?: Parameters<typeof action>[2]) => action(operationId, "retry-publish", approval)
export const cancelSandboxOperation = (operationId: string) => action(operationId, "cancel")
export const discardSandboxOperation = (operationId: string) => action(operationId, "discard")
