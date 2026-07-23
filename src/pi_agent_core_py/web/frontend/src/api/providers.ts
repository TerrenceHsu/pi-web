// Provider REST API client——P1-E M2-1。
//
// 复用 api/client.ts 的 requestJson + ApiError——禁止直接 fetch / axios / SDK。
// 所有 path 参数都 encodeURIComponent；Secret 仅作为 create/rotate 参数传递。
//
// 不实现：
// - POST /api/credentials/{id}/validate
// - POST /api/provider-hints

import type {
  CredentialCreateRequest,
  CredentialDeleteResponse,
  CredentialLabelUpdateRequest,
  CredentialMutationResponse,
  CredentialRotateRequest,
  CredentialsResponse,
  ProviderDefinitionView,
  ProviderModelOption,
  ProviderModelsResponse,
  ProviderProfileCreateRequest,
  ProviderProfileMutationResponse,
  ProviderProfileUpdateRequest,
  ProviderProfilesResponse,
  SessionModelBindingPutRequest,
  SessionModelBindingResponse,
} from "../types"
import { requestJson } from "./client"

// ============================================================================
// Provider Definitions
// ============================================================================

/** GET /api/provider-definitions——返回裸数组（不是 {definitions: [...]}）。 */
export function getProviderDefinitions(): Promise<ProviderDefinitionView[]> {
  return requestJson<ProviderDefinitionView[]>("/api/provider-definitions")
}

// ============================================================================
// Credentials
// ============================================================================

/** GET /api/credentials——列表。 */
export function listCredentials(): Promise<CredentialsResponse> {
  return requestJson<CredentialsResponse>("/api/credentials")
}

/** POST /api/credentials——创建 + secret 原子事务；返回 201 + warnings。 */
export function createCredential(
  payload: CredentialCreateRequest,
): Promise<CredentialMutationResponse> {
  return requestJson<CredentialMutationResponse>("/api/credentials", {
    method: "POST",
    body: payload,
  })
}

/** PATCH /api/credentials/{credential_id}——仅更新 label。 */
export function updateCredentialLabel(
  credentialId: string,
  payload: CredentialLabelUpdateRequest,
): Promise<CredentialMutationResponse> {
  return requestJson<CredentialMutationResponse>(
    `/api/credentials/${encodeURIComponent(credentialId)}`,
    { method: "PATCH", body: payload },
  )
}

/** PUT /api/credentials/{credential_id}/secret——轮换 + CAS；返回 warnings。 */
export function rotateCredentialSecret(
  credentialId: string,
  payload: CredentialRotateRequest,
): Promise<CredentialMutationResponse> {
  return requestJson<CredentialMutationResponse>(
    `/api/credentials/${encodeURIComponent(credentialId)}/secret`,
    { method: "PUT", body: payload },
  )
}

/** DELETE /api/credentials/{credential_id}——返回 200 + body（非 204）。 */
export function deleteCredential(credentialId: string): Promise<CredentialDeleteResponse> {
  return requestJson<CredentialDeleteResponse>(
    `/api/credentials/${encodeURIComponent(credentialId)}`,
    { method: "DELETE" },
  )
}

// ============================================================================
// Provider Profiles
// ============================================================================

/** GET /api/provider-profiles——列表。 */
export function listProviderProfiles(): Promise<ProviderProfilesResponse> {
  return requestJson<ProviderProfilesResponse>("/api/provider-profiles")
}

/** POST /api/provider-profiles——创建；返回 201。 */
export function createProviderProfile(
  payload: ProviderProfileCreateRequest,
): Promise<ProviderProfileMutationResponse> {
  return requestJson<ProviderProfileMutationResponse>("/api/provider-profiles", {
    method: "POST",
    body: payload,
  })
}

/** PATCH /api/provider-profiles/{profile_id}——不含 provider_id（immutable）。 */
export function updateProviderProfile(
  profileId: string,
  payload: ProviderProfileUpdateRequest,
): Promise<ProviderProfileMutationResponse> {
  return requestJson<ProviderProfileMutationResponse>(
    `/api/provider-profiles/${encodeURIComponent(profileId)}`,
    { method: "PATCH", body: payload },
  )
}

/**
 * DELETE /api/provider-profiles/{profile_id}——返回 204 No Content。
 *
 * requestJson 在 resp.status === 204 时返回 null——这里返回 null 表示删除成功。
 * 调用方据此判断 "deleted"，不依赖 body。
 */
export function deleteProviderProfile(profileId: string): Promise<null> {
  return requestJson<null>(`/api/provider-profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  })
}

/** GET /api/provider-profiles/{profile_id}/models——静态建议列表，可能为空。 */
export function getProviderProfileModels(profileId: string): Promise<ProviderModelsResponse> {
  return requestJson<ProviderModelsResponse>(
    `/api/provider-profiles/${encodeURIComponent(profileId)}/models`,
  )
}

// ============================================================================
// Session Binding
// ============================================================================

/**
 * GET /api/sessions/{session_id}/model-binding。
 *
 * response.binding === null 是合法 Legacy 状态（不是错误）。
 */
export function getSessionModelBinding(sessionId: string): Promise<SessionModelBindingResponse> {
  return requestJson<SessionModelBindingResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/model-binding`,
  )
}

/** PUT /api/sessions/{session_id}/model-binding——body.profile_id + body.model_id。 */
export function putSessionModelBinding(
  sessionId: string,
  payload: SessionModelBindingPutRequest,
): Promise<SessionModelBindingResponse> {
  return requestJson<SessionModelBindingResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/model-binding`,
    { method: "PUT", body: payload },
  )
}

// ============================================================================
// Convenience re-export for type-only consumers
// ============================================================================

export type { ProviderModelOption, SessionModelBindingPutRequest }
