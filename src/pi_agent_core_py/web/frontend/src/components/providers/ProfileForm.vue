<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"

import { useProviderStore } from "../../stores/providerStore"
import { useSessionStore } from "../../stores/sessionStore"
import type {
  CredentialStorageMode,
  CredentialView,
  ProviderModelOption,
  ProviderProfileView,
  VisibleProviderId,
} from "../../types"
import ErrorBanner from "../common/ErrorBanner.vue"

// ============================================================================
// Props / Emits
// ============================================================================

const props = defineProps<{
  providerId: VisibleProviderId
  providerDisplayName: string
  /** null = 新建草稿。 */
  profile: ProviderProfileView | null
  /** null = 无匹配 Credential（新 Profile 或 Profile 引用丢失）。 */
  credential: CredentialView | null
  /** Modal 在打开时捕获的 sessionId——用于 staged save 与 Apply 的 session 校验。 */
  sessionId: string | null
}>()

const emit = defineEmits<{
  (e: "cancel-draft"): void
  (e: "saved"): void
  (e: "session-mismatch"): void
}>()

// ============================================================================
// Stores
// ============================================================================

const providerStore = useProviderStore()
const sessionStore = useSessionStore()

// ============================================================================
// 局部 form 状态——Secret 仅在 apiKey / envVarName ref 中，从不进 Pinia
// ============================================================================

const profileName = ref("")
const modelInput = ref("")
const enabled = ref(true)
const isDefault = ref(false)

const credentialLabel = ref("")
const selectedStorageMode = ref<CredentialStorageMode>("keyring")
const apiKey = ref("")
const envVarName = ref("")

/** Per-profile static model suggestions——仅已有 Profile 在 onMounted 加载。
 * 新 Profile 不调用 /models（spec §10）。 */
const modelOptions = ref<ProviderModelOption[]>([])

/** Stage A 成功后写入——Stage B 复用。Stage B 失败时保留以便重试。 */
const effectiveCredentialId = ref<string | null>(null)
const credentialStageCompleted = ref(false)

const fieldErrors = ref<Record<string, string>>({})
const localError = ref<string | null>(null)
const saveStatus = ref<"idle" | "saving" | "saved" | "error">("idle")
const applyStatus = ref<"idle" | "applying" | "applied" | "error">("idle")

// ============================================================================
// 初始化（mount 时一次性从 props + store 注入）
// ============================================================================

function initializeFromProps() {
  if (props.profile) {
    profileName.value = props.profile.name
    modelInput.value = props.profile.default_model
    enabled.value = props.profile.enabled
    isDefault.value = props.profile.is_default
    effectiveCredentialId.value = props.profile.credential_id
  } else {
    profileName.value = ""
    modelInput.value = ""
    enabled.value = true
    isDefault.value = false
    effectiveCredentialId.value = null
  }
  credentialStageCompleted.value = false

  if (props.credential) {
    credentialLabel.value = props.credential.label
    selectedStorageMode.value = props.credential.storage_mode
  } else {
    credentialLabel.value = props.profile ? "" : `${props.providerDisplayName} key`
    selectedStorageMode.value = "keyring"
  }
  apiKey.value = ""
  envVarName.value = ""
  fieldErrors.value = {}
  localError.value = null
  saveStatus.value = "idle"
  applyStatus.value = "idle"
}

initializeFromProps()

// ============================================================================
// 模型建议——只对已有 Profile 在 mount 时加载一次（spec §10）
// ============================================================================

onMounted(async () => {
  if (props.profile) {
    try {
      modelOptions.value = await providerStore.loadProfileModels(props.profile.id)
    } catch {
      // 模型加载失败不阻塞表单——用户仍可手动输入
      modelOptions.value = []
    }
  }
})

// ============================================================================
// Helpers
// ============================================================================

function clearSecrets() {
  apiKey.value = ""
  envVarName.value = ""
}

onBeforeUnmount(() => {
  clearSecrets()
})

function cancelDraft() {
  clearSecrets()
  emit("cancel-draft")
}

/** 当前 Profile 最新状态——从 store 查找；失败时 fallback 到 props。 */
const currentProfile = computed<ProviderProfileView | null>(() => {
  if (!props.profile) return null
  const found = providerStore.profiles.find((p) => p.id === props.profile!.id)
  return found ?? props.profile
})

const showApiKeyInput = computed(() => selectedStorageMode.value !== "env")

const showEnvVarInput = computed(() => selectedStorageMode.value === "env")

const showSessionRestartWarning = computed(() => selectedStorageMode.value === "session_only")

/** Apply 启用条件——所有都必须满足。 */
const canApply = computed(() => {
  if (!props.profile) return false
  if (!currentProfile.value) return false
  if (currentProfile.value.status !== "ready") return false
  if (!props.sessionId) return false
  if (sessionStore.activeSessionId !== props.sessionId) return false
  if (providerStore.savingBinding) return false
  if (saveStatus.value === "saving") return false
  return true
})

/** 同名 Profile 检测——仅警告，不强制阻止。 */
const duplicateNameWarning = computed(() => {
  if (!profileName.value.trim()) return null
  if (props.profile && props.profile.name === profileName.value.trim()) return null
  const duplicate = providerStore.profiles.some(
    (p) =>
      p.provider_id === props.providerId &&
      p.name === profileName.value.trim() &&
      p.id !== props.profile?.id,
  )
  return duplicate ? "A profile with this name already exists." : null
})

// ============================================================================
// Save configuration——分阶段：A Credential, B Profile
// ============================================================================

function validateFields(): boolean {
  const errors: Record<string, string> = {}
  if (!profileName.value.trim()) errors.profileName = "Profile name is required."
  if (!modelInput.value.trim()) errors.modelInput = "Model ID is required."
  if (!credentialLabel.value.trim()) {
    errors.credentialLabel = "Credential label is required."
  }
  if (selectedStorageMode.value === "env") {
    if (!effectiveCredentialId.value && !envVarName.value.trim()) {
      errors.envVarName = "Environment variable name is required for new credentials."
    }
    if (envVarName.value && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(envVarName.value)) {
      errors.envVarName = "Invalid environment variable name."
    }
  } else {
    if (!effectiveCredentialId.value && !apiKey.value.trim()) {
      errors.apiKey = "API Key is required for new credentials."
    }
  }
  fieldErrors.value = errors
  return Object.keys(errors).length === 0
}

/**
 * 判断是否需要 Stage A——基于 credentialStageCompleted flag。
 *
 * Stage A 成功后 credentialStageCompleted=true；任何 credential 字段变化会
 * reset 为 false。重试只在 flag=false 时才重新执行 Stage A——避免重复
 * 创建 Credential（spec: 阶段 A 成功 / B 失败可安全重试）。
 */
function needsStageA(): boolean {
  return !credentialStageCompleted.value
}

// Credential 字段变化——重置 credentialStageCompleted
watch([apiKey, envVarName, selectedStorageMode, credentialLabel], () => {
  if (credentialStageCompleted.value) {
    credentialStageCompleted.value = false
    // effectiveCredentialId 保留——重试时若 Stage A 再跑会覆盖
  }
})

async function runStageA(): Promise<boolean> {
  // 跨 storage mode：必须创建新 Credential
  const existingCred = props.credential
  const sameMode = existingCred && existingCred.storage_mode === selectedStorageMode.value

  if (selectedStorageMode.value === "env") {
    if (!sameMode || !effectiveCredentialId.value) {
      // 新建 env Credential
      if (!envVarName.value.trim()) {
        fieldErrors.value = {
          ...fieldErrors.value,
          envVarName: "Environment variable name is required for new credentials.",
        }
        return false
      }
      const created = await providerStore.createCredential({
        label: credentialLabel.value.trim(),
        storage_mode: "env",
        env_var_name: envVarName.value.trim(),
      })
      if (!created) {
        localError.value = providerStore.mutationError ?? "Credential could not be saved."
        return false
      }
      effectiveCredentialId.value = created.credential_id
    } else if (envVarName.value.trim()) {
      // 同模式 rotate
      const rotated = await providerStore.rotateCredentialSecret(effectiveCredentialId.value!, {
        env_var_name: envVarName.value.trim(),
      })
      if (!rotated) {
        localError.value = providerStore.mutationError ?? "Credential could not be rotated."
        return false
      }
    }
    // 同模式 + envVarName 空 → 跳过 secret mutation
  } else {
    // keyring / session_only
    if (!sameMode || !effectiveCredentialId.value) {
      // 新建
      if (!apiKey.value.trim()) {
        fieldErrors.value = {
          ...fieldErrors.value,
          apiKey: "API Key is required for new credentials.",
        }
        return false
      }
      const created = await providerStore.createCredential({
        label: credentialLabel.value.trim(),
        storage_mode: selectedStorageMode.value,
        secret_value: apiKey.value,
      })
      if (!created) {
        localError.value = providerStore.mutationError ?? "Credential could not be saved."
        return false
      }
      effectiveCredentialId.value = created.credential_id
    } else if (apiKey.value) {
      // 同模式 rotate
      const rotated = await providerStore.rotateCredentialSecret(effectiveCredentialId.value!, {
        secret_value: apiKey.value,
      })
      if (!rotated) {
        localError.value = providerStore.mutationError ?? "Credential could not be rotated."
        return false
      }
    }
  }

  // 同步 label——若变化且 effective credential 已存在
  if (effectiveCredentialId.value && existingCred) {
    if (credentialLabel.value.trim() !== existingCred.label) {
      const updated = await providerStore.updateCredentialLabel(effectiveCredentialId.value, {
        label: credentialLabel.value.trim(),
      })
      if (!updated) {
        localError.value = providerStore.mutationError ?? "Credential label could not be updated."
        return false
      }
    }
  }

  credentialStageCompleted.value = true
  return true
}

async function runStageB(): Promise<boolean> {
  if (props.profile) {
    const updated = await providerStore.updateProfile(props.profile.id, {
      name: profileName.value.trim(),
      credential_id: effectiveCredentialId.value!,
      default_model: modelInput.value.trim(),
      enabled: enabled.value,
      is_default: isDefault.value,
    })
    if (!updated) {
      localError.value = "Credential saved, but profile could not be saved."
      return false
    }
  } else {
    const created = await providerStore.createProfile({
      name: profileName.value.trim(),
      provider_id: props.providerId,
      credential_id: effectiveCredentialId.value!,
      default_model: modelInput.value.trim(),
      enabled: enabled.value,
      is_default: isDefault.value,
    })
    if (!created) {
      localError.value = "Credential saved, but profile could not be saved."
      return false
    }
  }
  return true
}

async function saveConfiguration() {
  // Session guard——开 save 前必须确认
  if (!props.sessionId || sessionStore.activeSessionId !== props.sessionId) {
    localError.value = "Session has changed. Please reopen settings for the current session."
    saveStatus.value = "error"
    emit("session-mismatch")
    return
  }

  if (!validateFields()) {
    saveStatus.value = "error"
    return
  }

  saveStatus.value = "saving"
  localError.value = null
  fieldErrors.value = {}

  // Stage A——Credential
  if (needsStageA()) {
    const stageAOk = await runStageA()
    if (!stageAOk) {
      saveStatus.value = "error"
      return
    }
    // Stage A 成功后再次确认 session 未切换
    if (sessionStore.activeSessionId !== props.sessionId) {
      localError.value = "Session has changed. Credential was saved but profile was not."
      saveStatus.value = "error"
      emit("session-mismatch")
      return
    }
  }

  // Stage B——Profile
  const stageBOk = await runStageB()
  if (!stageBOk) {
    saveStatus.value = "error"
    return
  }

  // 成功——清 secret、显示 Saved
  clearSecrets()
  saveStatus.value = "saved"
  setTimeout(() => {
    if (saveStatus.value === "saved") saveStatus.value = "idle"
  }, 2000)
  emit("saved")
}

// ============================================================================
// Apply to current session
// ============================================================================

async function applyToSession() {
  if (!canApply.value) return
  applyStatus.value = "applying"
  localError.value = null
  const profile = currentProfile.value!
  const result = await providerStore.setSessionBinding(
    props.sessionId!,
    profile.id,
    profile.default_model,
  )
  if (!result) {
    applyStatus.value = "error"
    localError.value = "Configuration was saved, but it could not be applied to this session."
    return
  }
  applyStatus.value = "applied"
  setTimeout(() => {
    if (applyStatus.value === "applied") applyStatus.value = "idle"
  }, 2000)
}

// ============================================================================
// Delete profile
// ============================================================================

async function deleteProfile() {
  if (!props.profile) return
  const confirmed = window.confirm(
    "Delete this provider profile? Existing session bindings may prevent deletion.",
  )
  if (!confirmed) return
  localError.value = null
  const ok = await providerStore.deleteProfile(props.profile.id)
  if (!ok) {
    // 409 profile_in_use / 其它——使用 store 已脱敏的 mutationError
    localError.value =
      providerStore.mutationError ?? "This profile is currently used by one or more sessions."
  }
}

// ============================================================================
// Session-mismatch watcher——Modal 仍开时 session 切换则停止任何后续 mutation
// ============================================================================

watch(
  () => sessionStore.activeSessionId,
  (newId) => {
    if (props.sessionId && newId !== props.sessionId) {
      clearSecrets()
    }
  },
)
</script>

<template>
  <div class="profile-form" :data-profile-id="profile?.id ?? 'draft'">
    <div class="profile-form-header">
      <span class="profile-form-name">{{ profile ? profile.name : "New profile" }}</span>
      <span
        v-if="profile"
        class="profile-status-badge"
        :class="`status-${profile.status}`"
        data-testid="profile-status-badge"
        >{{ profile.status }}</span
      >
    </div>

    <ErrorBanner
      v-if="localError"
      :message="localError"
      dismissible
      data-testid="profile-form-error"
      @dismiss="localError = null"
    />

    <div class="form-grid">
      <label class="form-field">
        <span class="form-label">Profile name</span>
        <input
          v-model="profileName"
          type="text"
          class="form-input"
          data-testid="profile-name-input"
          autocomplete="off"
        />
        <span v-if="fieldErrors.profileName" class="field-error">{{
          fieldErrors.profileName
        }}</span>
        <span
          v-else-if="duplicateNameWarning"
          class="field-warning"
          data-testid="duplicate-name-warning"
          >{{ duplicateNameWarning }}</span
        >
      </label>

      <label class="form-field">
        <span class="form-label">Model ID</span>
        <input
          v-model="modelInput"
          type="text"
          list="provider-model-options"
          class="form-input"
          data-testid="model-input"
          autocomplete="off"
        />
        <datalist id="provider-model-options">
          <option v-for="m in modelOptions" :key="m.id" :value="m.id">
            {{ m.display_name }}
          </option>
        </datalist>
        <span v-if="fieldErrors.modelInput" class="field-error">{{ fieldErrors.modelInput }}</span>
      </label>

      <div class="form-row">
        <label class="form-checkbox">
          <input v-model="enabled" type="checkbox" data-testid="enabled-checkbox" />
          <span>Enabled</span>
        </label>
        <label class="form-checkbox">
          <input v-model="isDefault" type="checkbox" data-testid="is-default-checkbox" />
          <span>Set as default</span>
        </label>
      </div>
    </div>

    <div class="form-divider"></div>

    <div class="form-grid">
      <label class="form-field">
        <span class="form-label">Credential label</span>
        <input
          v-model="credentialLabel"
          type="text"
          class="form-input"
          data-testid="credential-label-input"
          autocomplete="off"
        />
        <span v-if="fieldErrors.credentialLabel" class="field-error">{{
          fieldErrors.credentialLabel
        }}</span>
      </label>

      <fieldset class="form-field">
        <legend class="form-label">Storage mode</legend>
        <label class="form-radio">
          <input
            v-model="selectedStorageMode"
            type="radio"
            value="keyring"
            data-testid="storage-mode-keyring"
          />
          <span>Keyring</span>
        </label>
        <label class="form-radio">
          <input
            v-model="selectedStorageMode"
            type="radio"
            value="session_only"
            data-testid="storage-mode-session-only"
          />
          <span>Session only</span>
        </label>
        <label class="form-radio">
          <input
            v-model="selectedStorageMode"
            type="radio"
            value="env"
            data-testid="storage-mode-env"
          />
          <span>Env var</span>
        </label>
      </fieldset>

      <label v-if="showApiKeyInput" class="form-field">
        <span class="form-label">
          API Key
          <span v-if="credential" class="form-hint">(leave blank to keep current)</span>
        </span>
        <input
          v-model="apiKey"
          type="password"
          class="form-input"
          data-testid="api-key-input"
          autocomplete="new-password"
        />
        <span v-if="fieldErrors.apiKey" class="field-error">{{ fieldErrors.apiKey }}</span>
      </label>

      <div v-if="showEnvVarInput" class="form-field">
        <label for="env-var-name-input">
          <span class="form-label">
            Env var name
            <span v-if="credential" class="form-hint">(leave blank to keep current)</span>
          </span>
        </label>
        <input
          id="env-var-name-input"
          v-model="envVarName"
          type="text"
          class="form-input"
          data-testid="env-var-name-input"
          autocomplete="off"
          :placeholder="credential ? credential.masked_value : ''"
        />
        <span v-if="fieldErrors.envVarName" class="field-error">{{ fieldErrors.envVarName }}</span>
      </div>

      <p v-if="showSessionRestartWarning" class="form-warning" data-testid="session-only-warning">
        Key will be lost when the server restarts.
      </p>
    </div>

    <div class="form-actions">
      <button
        type="button"
        class="action-btn primary"
        :disabled="saveStatus === 'saving'"
        data-testid="save-configuration-btn"
        @click="saveConfiguration"
      >
        {{
          saveStatus === "saving"
            ? "Saving…"
            : saveStatus === "saved"
              ? "Saved"
              : "Save configuration"
        }}
      </button>

      <button
        v-if="profile"
        type="button"
        class="action-btn"
        :disabled="!canApply"
        data-testid="apply-to-session-btn"
        @click="applyToSession"
      >
        {{
          applyStatus === "applying"
            ? "Applying…"
            : applyStatus === "applied"
              ? "Applied"
              : "Apply to current session"
        }}
      </button>

      <button
        v-if="profile"
        type="button"
        class="action-btn danger"
        data-testid="delete-profile-btn"
        @click="deleteProfile"
      >
        Delete profile
      </button>

      <button
        v-if="!profile"
        type="button"
        class="action-btn"
        data-testid="cancel-draft-btn"
        @click="cancelDraft"
      >
        Cancel
      </button>
    </div>
  </div>
</template>

<style scoped>
.profile-form {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--code-bg, #fafafa);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.profile-form-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.profile-form-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--fg);
}
.profile-status-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--border);
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.profile-status-badge.status-ready {
  background: #dcfce7;
  color: #166534;
}
.profile-status-badge.status-disabled {
  background: #fee2e2;
  color: #991b1b;
}
.profile-status-badge.status-needs_credential,
.profile-status-badge.status-needs_key,
.profile-status-badge.status-backend_unavailable,
.profile-status-badge.status-credential_invalid,
.profile-status-badge.status-credential_error {
  background: #fef3c7;
  color: #92400e;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.form-label {
  font-weight: 500;
}
.form-hint {
  font-weight: 400;
  color: var(--muted);
  margin-left: 4px;
}
.form-input {
  padding: 6px 8px;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: white;
  color: var(--fg);
}
.form-input:focus {
  outline: none;
  border-color: var(--accent);
}
.form-row {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.form-checkbox,
.form-radio {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--fg);
  cursor: pointer;
  margin-right: 12px;
}
.field-error {
  color: #b91c1c;
  font-size: 11px;
}
.field-warning {
  color: #92400e;
  font-size: 11px;
}
.form-warning {
  margin: 0;
  font-size: 11px;
  color: #92400e;
  background: #fef3c7;
  padding: 4px 8px;
  border-radius: 4px;
}
.form-divider {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}
.form-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-top: 4px;
}
.action-btn {
  padding: 6px 10px;
  font-size: 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: white;
  color: var(--fg);
  cursor: pointer;
}
.action-btn:hover:not(:disabled) {
  background: var(--border);
}
.action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.action-btn.primary {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}
.action-btn.primary:hover:not(:disabled) {
  background: var(--accent-hover);
}
.action-btn.danger {
  color: #b91c1c;
  border-color: #fecaca;
}
.action-btn.danger:hover:not(:disabled) {
  background: #fef2f2;
}
</style>
