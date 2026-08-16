// Provider Store——P1-E M2-1。
//
// 全局数据：definitions / profiles / credentials。
// Session Binding：currentBinding + 状态机 (idle/loading/loaded/error)。
// 不读 Secret；不访问真实 Provider 网络；不持久化到 localStorage / sessionStorage。
// 不 import sessionStore / chatStore / 其它 store——协调在 App.vue。

import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as providersApi from "../api/providers"
import { ApiError } from "../api/client"
import type {
  BindingLoadState,
  CredentialCreateRequest,
  CredentialLabelUpdateRequest,
  CredentialRotateRequest,
  CredentialView,
  ModelCapabilitiesPutRequest,
  ProviderDefinitionView,
  ProviderProfileCreateRequest,
  ProviderProfileStatus,
  ProviderProfileUpdateRequest,
  ProviderProfileView,
  ProviderModelOption,
  ResolvedModelCapabilitiesView,
  SessionModelBindingView,
  VisibleProviderId,
} from "../types"

// ============================================================================
// 常量
// ============================================================================

const VISIBLE_PROVIDER_IDS: ReadonlySet<VisibleProviderId> = new Set(["glm", "qwen", "kimi"])

// 安全固定文案——不暴露后端 response body / exception / stack。
const LOAD_ERROR_SAFE = "Provider configuration could not be loaded."
const BINDING_LOAD_ERROR_SAFE = "Provider binding could not be loaded."
const MUTATION_ERROR_SAFE = "Provider operation failed."
const NETWORK_ERROR_SAFE = "Network error."
const DELETE_USED_CREDENTIAL_SAFE =
  "Credential is in use by one or more profiles and cannot be deleted."

// ============================================================================
// Helpers
// ============================================================================

/**
 * 把任意错误转为安全字符串。
 *
 * - ApiError：只读取其 detail（后端已脱敏）；其它字段丢弃
 * - network 错误（status=0）：固定 network 文案
 * - 未知错误：fallback
 *
 * 不返回 JSON.stringify / String(error)——那些可能含 secret / stack / payload。
 */
function toSafeProviderError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return NETWORK_ERROR_SAFE
    if (typeof error.detail === "string" && error.detail.length > 0) {
      return error.detail
    }
    return fallback
  }
  return fallback
}

function isVisibleProvider(providerId: string): boolean {
  return (VISIBLE_PROVIDER_IDS as Set<string>).has(providerId)
}

// ============================================================================
// Store
// ============================================================================

export const useProviderStore = defineStore("providers", () => {
  // ----- 全局数据 -----
  const definitions = ref<ProviderDefinitionView[]>([])
  const profiles = ref<ProviderProfileView[]>([])
  const credentials = ref<CredentialView[]>([])

  // ----- Session Binding -----
  const currentBinding = ref<SessionModelBindingView | null>(null)
  const bindingSessionId = ref<string | null>(null)
  const bindingLoadState = ref<BindingLoadState>("idle")

  // ----- 加载状态 -----
  const definitionsLoading = ref(false)
  const profilesLoading = ref(false)
  const credentialsLoading = ref(false)

  // ----- Mutation 状态 -----
  const savingCredential = ref(false)
  const savingProfile = ref(false)
  const savingBinding = ref(false)

  // ----- 错误 -----
  const loadError = ref<string | null>(null)
  const bindingLoadError = ref<string | null>(null)
  const mutationError = ref<string | null>(null)

  // ----- 初始化 -----
  const initialized = ref(false)

  // ----- 内部（非 reactive）状态 -----
  // 模块级 token——每次 refresh / clear 自增；旧响应到达时校验 token 仍为最新。
  // 不能放在 ref 里——否则响应式的 watcher 可能读到中间态。
  let bindingLoadToken = 0

  // ========================================================================
  // Getters
  // ========================================================================

  /** 只返回 GLM / Qwen / Kimi——隐藏 Anthropic。 */
  const visibleDefinitions = computed(() =>
    definitions.value.filter((d) => isVisibleProvider(d.id)),
  )

  /** 仅返回 visible + enabled + ready 的 Profile。 */
  const usableProfiles = computed(() =>
    profiles.value.filter(
      (p) => isVisibleProvider(p.provider_id) && p.enabled && p.status === "ready",
    ),
  )

  /**
   * 按 provider_id 分组的 Profile——Settings 使用。
   *
   * 包含 visible provider 下的所有 Profile（disabled / needs_key / etc）。
   * 排除 Anthropic Profile（产品边界）。
   */
  const profilesByProvider = computed(() => {
    const map = new Map<VisibleProviderId, ProviderProfileView[]>()
    for (const p of profiles.value) {
      if (!isVisibleProvider(p.provider_id)) continue
      const list = map.get(p.provider_id as VisibleProviderId)
      if (list) {
        list.push(p)
      } else {
        map.set(p.provider_id as VisibleProviderId, [p])
      }
    }
    return map
  })

  /** 从 raw profiles 中按 currentBinding.profile_id 解析——不从 usableProfiles。 */
  const selectedProfile = computed(() => {
    const pid = currentBinding.value?.profile_id
    if (!pid) return null
    return profiles.value.find((p) => p.id === pid) ?? null
  })

  /** 从 raw definitions 中按 selectedProfile.provider_id 解析。 */
  const selectedProvider = computed(() => {
    const providerId = selectedProfile.value?.provider_id
    if (!providerId) return null
    return definitions.value.find((d) => d.id === providerId) ?? null
  })

  /**
   * 必须来自 currentBinding.model_id——不是 Profile.default_model。
   *
   * Legacy（binding=null）→ null。
   */
  const selectedModel = computed(() => currentBinding.value?.model_id ?? null)

  /** selectedProfile.status；无 Binding → null。 */
  const currentProfileStatus = computed<ProviderProfileStatus | null>(
    () => selectedProfile.value?.status ?? null,
  )

  /**
   * 未被任何 Profile 引用的 Credential。
   *
   * 引用计数必须遍历 raw profiles（含 Anthropic / disabled / needs_key）——
   * 否则隐藏的 Anthropic Profile 会让其 Credential 被误判为 unused。
   */
  const unusedCredentials = computed(() => {
    const usedIds = new Set<string>()
    for (const p of profiles.value) {
      if (p.credential_id) usedIds.add(p.credential_id)
    }
    return credentials.value.filter((c) => !usedIds.has(c.credential_id))
  })

  /**
   * 冻结语义：
   *   - bindingLoadState !== "loaded" → false
   *   - loaded + currentBinding === null → true（Legacy）
   *   - loaded + selectedProfile missing → false
   *   - loaded + selectedProfile.status !== "ready" → false
   *   - loaded + ready → true
   */
  const canSendPrompt = computed(() => {
    if (bindingLoadState.value !== "loaded") return false
    if (currentBinding.value === null) return true
    const profile = selectedProfile.value
    if (!profile) return false
    return profile.status === "ready"
  })

  // ========================================================================
  // 全局数据加载
  // ========================================================================

  async function loadDefinitions(): Promise<void> {
    definitionsLoading.value = true
    try {
      const resp = await providersApi.getProviderDefinitions()
      definitions.value = resp
    } catch (e) {
      throw toSafeProviderError(e, LOAD_ERROR_SAFE)
    } finally {
      definitionsLoading.value = false
    }
  }

  async function loadProfiles(): Promise<void> {
    profilesLoading.value = true
    try {
      const resp = await providersApi.listProviderProfiles()
      profiles.value = resp.profiles
    } catch (e) {
      throw toSafeProviderError(e, LOAD_ERROR_SAFE)
    } finally {
      profilesLoading.value = false
    }
  }

  async function loadCredentials(): Promise<void> {
    credentialsLoading.value = true
    try {
      const resp = await providersApi.listCredentials()
      credentials.value = resp.credentials
    } catch (e) {
      throw toSafeProviderError(e, LOAD_ERROR_SAFE)
    } finally {
      credentialsLoading.value = false
    }
  }

  /**
   * 并行加载 definitions / profiles / credentials。
   *
   * 单项失败不阻断其它项；错误写到 loadError；initialized 始终到 true。
   * 不向 App 抛 raw error。
   */
  async function initialize(): Promise<void> {
    if (initialized.value) return
    loadError.value = null
    const results = await Promise.allSettled([loadDefinitions(), loadProfiles(), loadCredentials()])
    const errors = results
      .filter((r): r is PromiseRejectedResult => r.status === "rejected")
      .map((r) => r.reason)
    if (errors.length > 0) {
      loadError.value =
        typeof errors[0] === "string" && errors[0].length > 0 ? errors[0] : LOAD_ERROR_SAFE
    }
    initialized.value = true
  }

  // ========================================================================
  // Binding 加载状态机
  // ========================================================================

  /**
   * 私有：发起 GET binding；响应到达时校验 token + sessionId 一致才写状态。
   *
   * 失败固定写到 bindingLoadError（安全文案）——不写 ApiError.detail。
   */
  async function fetchBindingForSession(sessionId: string, token: number): Promise<void> {
    try {
      const resp = await providersApi.getSessionModelBinding(sessionId)
      // stale 校验——token 与 sessionId 都必须仍是当前值
      if (token !== bindingLoadToken) return
      if (sessionId !== bindingSessionId.value) return
      currentBinding.value = resp.binding
      bindingLoadState.value = "loaded"
      bindingLoadError.value = null
    } catch (e) {
      if (token !== bindingLoadToken) return
      if (sessionId !== bindingSessionId.value) return
      currentBinding.value = null
      bindingLoadState.value = "error"
      bindingLoadError.value = toSafeProviderError(e, BINDING_LOAD_ERROR_SAFE)
    }
  }

  /**
   * 公开：按 sessionId 刷新 Binding——idle → loading → loaded/error。
   *
   * 不能把请求失败解释为 binding=null（Legacy）——失败固定进 error 状态。
   */
  async function refreshForSession(sessionId: string): Promise<void> {
    currentBinding.value = null
    bindingSessionId.value = sessionId
    bindingLoadState.value = "loading"
    bindingLoadToken += 1
    const token = bindingLoadToken
    bindingLoadError.value = null
    await fetchBindingForSession(sessionId, token)
  }

  /**
   * 清空 Session 状态——通常在 activeSessionId 变 null 时调。
   *
   * token 自增——使任何 pending 旧响应失效。
   */
  function clearSessionState(): void {
    bindingLoadToken += 1
    currentBinding.value = null
    bindingSessionId.value = null
    bindingLoadState.value = "idle"
    bindingLoadError.value = null
  }

  // ========================================================================
  // Mutation Actions——供 M2-2/M2-3 使用
  // ========================================================================

  /**
   * 创建 Credential——Secret 仅作为瞬时参数传给 API。
   *
   * 成功后刷新 credentials + profiles（masked_value / status 可能变化）。
   * 失败固定写入 mutationError；不向 caller 抛 raw error。
   */
  async function createCredential(
    payload: CredentialCreateRequest,
  ): Promise<CredentialView | null> {
    mutationError.value = null
    savingCredential.value = true
    try {
      const resp = await providersApi.createCredential(payload)
      await Promise.all([loadCredentials(), loadProfiles()])
      return resp.credential
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return null
    } finally {
      savingCredential.value = false
    }
  }

  async function updateCredentialLabel(
    credentialId: string,
    payload: CredentialLabelUpdateRequest,
  ): Promise<CredentialView | null> {
    mutationError.value = null
    savingCredential.value = true
    try {
      const resp = await providersApi.updateCredentialLabel(credentialId, payload)
      await loadCredentials()
      return resp.credential
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return null
    } finally {
      savingCredential.value = false
    }
  }

  /**
   * 轮换 secret——成功后刷新 credentials + profiles（masked / status 变化）。
   * Secret 仅作为瞬时参数传给 API；不缓存到 ref / state。
   */
  async function rotateCredentialSecret(
    credentialId: string,
    payload: CredentialRotateRequest,
  ): Promise<CredentialView | null> {
    mutationError.value = null
    savingCredential.value = true
    try {
      const resp = await providersApi.rotateCredentialSecret(credentialId, payload)
      await Promise.all([loadCredentials(), loadProfiles()])
      return resp.credential
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return null
    } finally {
      savingCredential.value = false
    }
  }

  /**
   * 删除 Credential——M2 冻结规则：只能删未被任何 Profile 引用的 Credential。
   *
   * 引用检查必须遍历 raw profiles（含 Anthropic / disabled）。
   * 仍被引用：mutationError 固定文案，返回 false，不调 API。
   */
  async function deleteCredential(credentialId: string): Promise<boolean> {
    mutationError.value = null
    const stillUsed = profiles.value.some((p) => p.credential_id === credentialId)
    if (stillUsed) {
      mutationError.value = DELETE_USED_CREDENTIAL_SAFE
      return false
    }
    savingCredential.value = true
    try {
      await providersApi.deleteCredential(credentialId)
      await loadCredentials()
      return true
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return false
    } finally {
      savingCredential.value = false
    }
  }

  /**
   * 创建 Profile——成功后刷新 profiles + credentials（unusedCredentials 计算）。
   * is_default=true 可能让其它 Profile 的 is_default 被后端清除——不能局部更新。
   */
  async function createProfile(
    payload: ProviderProfileCreateRequest,
  ): Promise<ProviderProfileView | null> {
    mutationError.value = null
    savingProfile.value = true
    try {
      const resp = await providersApi.createProviderProfile(payload)
      await Promise.all([loadProfiles(), loadCredentials()])
      return resp.profile
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return null
    } finally {
      savingProfile.value = false
    }
  }

  async function updateProfile(
    profileId: string,
    payload: ProviderProfileUpdateRequest,
  ): Promise<ProviderProfileView | null> {
    mutationError.value = null
    savingProfile.value = true
    try {
      const resp = await providersApi.updateProviderProfile(profileId, payload)
      // 重新 GET profiles——default 切换可能影响其它 Profile 的 is_default
      await Promise.all([loadProfiles(), loadCredentials()])
      return resp.profile
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return null
    } finally {
      savingProfile.value = false
    }
  }

  /**
   * 删除 Profile——成功后刷新 profiles + credentials。
   * 不自动删除关联 Credential（即使变 unused）。
   */
  async function deleteProfile(profileId: string): Promise<boolean> {
    mutationError.value = null
    savingProfile.value = true
    try {
      await providersApi.deleteProviderProfile(profileId)
      await Promise.all([loadProfiles(), loadCredentials()])
      return true
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return false
    } finally {
      savingProfile.value = false
    }
  }

  /**
   * PUT binding——body 由 caller 显式传入 profile_id + model_id。
   *
   * Store 不自行替换为 Profile.default_model——M2-3 Selector 负责传值。
   * 成功后若 sessionId 仍是当前 active，更新 currentBinding；否则不覆盖。
   * 失败不 fallback、不清空原 currentBinding。
   */
  async function setSessionBinding(
    sessionId: string,
    profileId: string,
    modelId: string,
  ): Promise<SessionModelBindingView | null> {
    mutationError.value = null
    savingBinding.value = true
    try {
      const resp = await providersApi.putSessionModelBinding(sessionId, {
        profile_id: profileId,
        model_id: modelId,
      })
      if (sessionId === bindingSessionId.value) {
        currentBinding.value = resp.binding
        bindingLoadState.value = "loaded"
        bindingLoadError.value = null
      }
      return resp.binding
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return null
    } finally {
      savingBinding.value = false
    }
  }

  /** GET /api/provider-profiles/{id}/models——静态建议列表，可能为空。 */
  async function loadProfileModels(profileId: string): Promise<ProviderModelOption[]> {
    mutationError.value = null
    try {
      const resp = await providersApi.getProviderProfileModels(profileId)
      return resp.models
    } catch (e) {
      mutationError.value = toSafeProviderError(e, MUTATION_ERROR_SAFE)
      return []
    }
  }

  async function loadModelCapabilities(
    profileId: string,
    modelId: string,
  ): Promise<ResolvedModelCapabilitiesView | null> {
    try {
      return (await providersApi.getModelCapabilities(profileId, modelId)).capabilities
    } catch (error) {
      mutationError.value = toSafeProviderError(error, MUTATION_ERROR_SAFE)
      return null
    }
  }

  async function saveModelCapabilities(
    profileId: string,
    payload: ModelCapabilitiesPutRequest,
  ): Promise<ResolvedModelCapabilitiesView | null> {
    try {
      return (await providersApi.putModelCapabilities(profileId, payload)).capabilities
    } catch (error) {
      mutationError.value = toSafeProviderError(error, MUTATION_ERROR_SAFE)
      return null
    }
  }

  function resetWorkspace(): void {
    bindingLoadToken += 1
    definitions.value = []
    profiles.value = []
    credentials.value = []
    currentBinding.value = null
    bindingSessionId.value = null
    bindingLoadState.value = "idle"
    definitionsLoading.value = false
    profilesLoading.value = false
    credentialsLoading.value = false
    savingCredential.value = false
    savingProfile.value = false
    savingBinding.value = false
    loadError.value = null
    bindingLoadError.value = null
    mutationError.value = null
    initialized.value = false
  }

  return {
    // 全局数据
    definitions,
    profiles,
    credentials,
    // Session Binding
    currentBinding,
    bindingSessionId,
    bindingLoadState,
    // 加载状态
    definitionsLoading,
    profilesLoading,
    credentialsLoading,
    // Mutation 状态
    savingCredential,
    savingProfile,
    savingBinding,
    // 错误
    loadError,
    bindingLoadError,
    mutationError,
    // 初始化
    initialized,
    // Getters
    visibleDefinitions,
    usableProfiles,
    profilesByProvider,
    selectedProfile,
    selectedProvider,
    selectedModel,
    currentProfileStatus,
    unusedCredentials,
    canSendPrompt,
    // 全局加载
    initialize,
    loadDefinitions,
    loadProfiles,
    loadCredentials,
    // Binding 状态机
    refreshForSession,
    clearSessionState,
    // Mutations
    createCredential,
    updateCredentialLabel,
    rotateCredentialSecret,
    deleteCredential,
    createProfile,
    updateProfile,
    deleteProfile,
    setSessionBinding,
    loadProfileModels,
    loadModelCapabilities,
    saveModelCapabilities,
    resetWorkspace,
  }
})
