// Provider 类型——P1-E M2-1 Frontend。
//
// 严格对齐真实后端 wire schema（见 web/credentials_api.py / provider_profiles_api.py）。
// 所有 DTO 保持 snake_case；时间字段为 ms epoch（int）。
//
// 安全不变量：
// - CredentialView 不含 secret_value / env_var_name 原文 / secret_ref / fingerprint
// - ProviderDefinitionView 不含 key_prefix_hints / default_base_url / validation_strategy
// - ProviderProfileUpdateRequest 不含 provider_id（immutable）

// ============================================================================
// Provider 标识
// ============================================================================

export type ProviderId = "anthropic" | "glm" | "qwen" | "kimi"

export type VisibleProviderId = "glm" | "qwen" | "kimi"

export type ProviderApiStyle = "anthropic_compatible" | "openai_compatible"

// ============================================================================
// Credential 枚举
// ============================================================================

export type CredentialStorageMode = "keyring" | "session_only" | "env"

export type CredentialStorageStatus = "ready" | "needs_key" | "backend_unavailable"

export type CredentialValidationStatus = "never_validated" | "valid" | "invalid" | "error"

export type ProviderHintConfidence = "high" | "medium" | "low" | "unknown"

// ============================================================================
// Profile 枚举
// ============================================================================

export type ProviderProfileStatus =
  | "ready"
  | "disabled"
  | "needs_credential"
  | "needs_key"
  | "backend_unavailable"
  | "credential_invalid"
  | "credential_error"

// ============================================================================
// Session Binding 枚举
// ============================================================================

export type SessionBindingSource = "default" | "explicit"

// ============================================================================
// 前端 only：Binding 加载状态机
// ============================================================================

export type BindingLoadState = "idle" | "loading" | "loaded" | "error"

// ============================================================================
// View 类型（wire DTO）
// ============================================================================

export interface ProviderDefinitionView {
  id: ProviderId
  display_name: string
  api_style: ProviderApiStyle
  validation_supported: boolean
  supports_model_listing: boolean
}

export interface CredentialView {
  credential_id: string
  label: string
  storage_mode: CredentialStorageMode
  storage_status: CredentialStorageStatus
  masked_value: string
  provider_hint: string | null
  provider_hint_confidence: ProviderHintConfidence | null
  validation_status: CredentialValidationStatus
  last_validated_provider_id: string | null
  last_validated_at: number | null
  last_error_code: string | null
  created_at: number
  updated_at: number
}

export interface ProviderProfileView {
  id: string
  name: string
  provider_id: ProviderId
  provider_display_name: string
  credential_id: string
  credential_masked_value: string | null
  default_model: string
  enabled: boolean
  is_default: boolean
  status: ProviderProfileStatus
  created_at: number
  updated_at: number
}

export interface ModelCapabilitiesView {
  streaming: boolean | null
  tool_calling: boolean | null
  reasoning: boolean | null
  vision: boolean | null
  context_window: number | null
}

export interface ProviderModelOption {
  id: string
  display_name: string | null
  source: string
  capabilities: ModelCapabilitiesView
}

export interface SessionModelBindingView {
  session_id: string
  profile_id: string
  model_id: string
  source: SessionBindingSource
  created_at: number
  updated_at: number
}

// ============================================================================
// Request 类型
// ============================================================================

export interface CredentialCreateRequest {
  label: string
  storage_mode: CredentialStorageMode
  secret_value?: string
  env_var_name?: string
}

export interface CredentialLabelUpdateRequest {
  label: string
}

export interface CredentialRotateRequest {
  secret_value?: string
  env_var_name?: string
}

export interface ProviderProfileCreateRequest {
  name: string
  provider_id: ProviderId
  credential_id: string
  default_model: string
  enabled?: boolean
  is_default?: boolean
}

// 不含 provider_id——后端 immutable。
export interface ProviderProfileUpdateRequest {
  name?: string
  credential_id?: string
  default_model?: string
  enabled?: boolean
  is_default?: boolean
}

export interface SessionModelBindingPutRequest {
  profile_id: string
  model_id: string
}

// ============================================================================
// Response envelopes
// ============================================================================

// GET /api/provider-definitions 返回裸数组——不是 {definitions: [...]}。
export type ProviderDefinitionsResponse = ProviderDefinitionView[]

export interface CredentialsResponse {
  credentials: CredentialView[]
}

export interface CredentialMutationResponse {
  credential: CredentialView
  warnings: string[]
}

export interface CredentialDeleteResponse {
  credential_id: string
  deleted: boolean
  warnings: string[]
}

export interface ProviderProfilesResponse {
  profiles: ProviderProfileView[]
}

export interface ProviderProfileMutationResponse {
  profile: ProviderProfileView
}

export interface ProviderModelsResponse {
  models: ProviderModelOption[]
}

export interface SessionModelBindingResponse {
  binding: SessionModelBindingView | null
}
