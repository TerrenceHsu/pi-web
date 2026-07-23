<script setup lang="ts">
import { computed } from "vue"

import { useChatStore } from "../../stores/chatStore"
import { useProviderStore } from "../../stores/providerStore"
import { useSessionStore } from "../../stores/sessionStore"
import type { ProviderProfileView, VisibleProviderId } from "../../types"

// ============================================================================
// Stores
// ============================================================================

const providerStore = useProviderStore()
const sessionStore = useSessionStore()
const chatStore = useChatStore()

// ============================================================================
// 候选数据
// ============================================================================

const SECTION_PROVIDER_IDS: VisibleProviderId[] = ["glm", "qwen", "kimi"]

function displayNameFor(id: VisibleProviderId): string {
  const match = providerStore.visibleDefinitions.find((d) => d.id === id)
  return match?.display_name ?? id.toUpperCase()
}

/** 按 provider 分组的 usable Profiles——只含 ready + enabled + visible。 */
const profilesByProvider = computed(() => {
  const map = new Map<VisibleProviderId, ProviderProfileView[]>()
  for (const p of providerStore.usableProfiles) {
    if (!SECTION_PROVIDER_IDS.includes(p.provider_id as VisibleProviderId)) continue
    const id = p.provider_id as VisibleProviderId
    const list = map.get(id)
    if (list) list.push(p)
    else map.set(id, [p])
  }
  return map
})

function profilesFor(id: VisibleProviderId): ProviderProfileView[] {
  return profilesByProvider.value.get(id) ?? []
}

function optionLabel(profile: ProviderProfileView): string {
  return `${profile.name} · ${profile.default_model}`
}

// ============================================================================
// 当前 Binding 投影——profile 可能已缺失（被删除）但 binding 仍在
// ============================================================================

const currentBinding = computed(() => providerStore.currentBinding)

const currentProfile = computed<ProviderProfileView | null>(() => providerStore.selectedProfile)

const currentProfileMissing = computed(
  () => currentBinding.value !== null && currentProfile.value === null,
)

/** 当前 Profile 是否属于 Anthropic（隐藏）。 */
const currentIsAnthropic = computed(() => {
  if (!currentProfile.value) return false
  return !SECTION_PROVIDER_IDS.includes(currentProfile.value.provider_id as VisibleProviderId)
})

const currentProfileInvalid = computed(() => {
  if (!currentProfile.value) return false
  return currentProfile.value.status !== "ready"
})

// ============================================================================
// 状态机
// ============================================================================

const bindingLoadState = computed(() => providerStore.bindingLoadState)

const hasUsableProfiles = computed(() => providerStore.usableProfiles.length > 0)

const requestRunning = computed(
  () => chatStore.sending || chatStore.streaming || !!chatStore.currentRequestId,
)

const switchPending = computed(() => providerStore.savingBinding)

/** Select 是否禁用。 */
const disabled = computed(() => {
  if (!sessionStore.activeSessionId) return true
  if (bindingLoadState.value !== "loaded") return true
  // Binding 数据不属于当前 active session——stale（App.vue watch 会触发 refresh）
  if (providerStore.bindingSessionId !== sessionStore.activeSessionId) return true
  if (requestRunning.value) return true
  if (switchPending.value) return true
  // 当前是 invalid/missing/Anthropic 且没有可选 Profile——禁用
  if (!hasUsableProfiles.value) {
    if (currentBinding.value === null) return true // legacy 但无可选
    if (currentProfileInvalid.value || currentProfileMissing.value) return true
  }
  return false
})

const disabledTitle = computed(() => {
  if (!sessionStore.activeSessionId) return "Select a session first"
  if (bindingLoadState.value === "loading") return "Loading model…"
  if (bindingLoadState.value === "error")
    return providerStore.bindingLoadError ?? "Provider binding could not be loaded."
  if (bindingLoadState.value === "idle") return "Provider binding is not loaded yet."
  if (requestRunning.value) return "当前回答完成后可切换"
  if (switchPending.value) return "正在切换模型配置"
  if (!hasUsableProfiles.value) return "请在侧栏 Provider Settings 中配置"
  return "Provider profile"
})

// ============================================================================
// Select value——从 Store currentBinding 派生，绝不本地持有
// ============================================================================

/**
 * `<select>` value——由 store currentBinding 派生。
 * - loaded + null → ""（Legacy）
 * - loaded + ready → profile_id
 * - loaded + invalid/Anthropic → ""（不可选，但 store 仍持 binding）
 * - missing Profile → ""（store 仍持 binding.profile_id，但不在 option 中）
 */
const selectValue = computed(() => {
  if (bindingLoadState.value !== "loaded") return ""
  if (!currentBinding.value) return ""
  // binding 存在——若 Profile 可选展示其 id，否则空（store 已脱敏仍持有真实 id）
  if (currentProfile.value && !currentProfileInvalid.value && !currentIsAnthropic.value) {
    return currentProfile.value.id
  }
  return ""
})

// ============================================================================
// 当前展示文案——使用 currentBinding.model_id（不是 Profile.default_model）
// ============================================================================

const currentDisplayLabel = computed(() => {
  if (bindingLoadState.value === "loading") return "Loading model…"
  if (bindingLoadState.value === "error")
    return providerStore.bindingLoadError ?? "Provider binding error."
  if (bindingLoadState.value === "idle") return "—"
  // loaded
  if (!currentBinding.value) return "默认模型"
  if (currentProfileMissing.value) return "配置不可用"
  if (currentIsAnthropic.value) return "当前模型由旧配置管理"
  if (currentProfileInvalid.value) return "当前 Provider 配置不可用"
  // 当前已绑定 ready Profile——用 binding.model_id（非 Profile.default_model）
  const profile = currentProfile.value!
  return `${profile.name} · ${currentBinding.value.model_id}`
})

const currentDisplayTitle = computed(() => {
  // 完整文案 for tooltip——避免视觉截断丢失信息
  return currentDisplayLabel.value
})

const showSettingsHint = computed(() => {
  if (bindingLoadState.value !== "loaded") return false
  if (!hasUsableProfiles.value) return true
  if (currentProfileMissing.value) return true
  if (currentProfileInvalid.value) return true
  return false
})

const showLegacyHint = computed(() => {
  if (bindingLoadState.value !== "loaded") return false
  return currentBinding.value === null
})

// ============================================================================
// Binding 切换——失败不 optimistic，不 fallback
// ============================================================================

async function onChange(e: Event) {
  const target = e.target as HTMLSelectElement
  const selectedProfileId = target.value
  const sessionId = sessionStore.activeSessionId
  if (!sessionId || !selectedProfileId) return

  // 守卫：条件不满足时不发请求
  if (requestRunning.value) return
  if (switchPending.value) return
  if (bindingLoadState.value !== "loaded") return

  // 找到 profile——必须在 usableProfiles
  const profile = providerStore.usableProfiles.find((p) => p.id === selectedProfileId)
  if (!profile) return

  // 清旧 mutationError（直接赋值——M2-1 Pinia ref 允许）
  providerStore.mutationError = null

  // 调用 store——使用 profile.default_model（不是 currentBinding.model_id）
  await providerStore.setSessionBinding(sessionId, profile.id, profile.default_model)

  // 失败/成功 UI 都从 store currentBinding 重新派生——这里无需手动同步
  // 失败时若 DOM select 暂停在新值，Vue 下一 tick 会用 selectValue 重写回 store 派生值
}

// ============================================================================
// 辅助：让 <select> 在失败后恢复到 store 派生 value（防 optimistic）
// ============================================================================

function onBlur() {
  // selectValue 是 computed——Vue 自动同步 DOM value；这里不需要额外处理
}

defineExpose({
  // 暴露 internal state 便于测试断言
  state: computed(() => ({
    bindingLoadState: bindingLoadState.value,
    selectValue: selectValue.value,
    disabled: disabled.value,
    hasUsableProfiles: hasUsableProfiles.value,
  })),
})
</script>

<template>
  <div class="provider-selector" data-testid="provider-selector">
    <label class="selector-label" for="provider-profile-select">Provider profile</label>

    <div v-if="bindingLoadState === 'error'" class="selector-error" role="alert">
      {{ providerStore.bindingLoadError ?? "Provider binding could not be loaded." }}
    </div>

    <select
      id="provider-profile-select"
      class="selector-select"
      data-testid="provider-profile-select"
      :value="selectValue"
      :disabled="disabled"
      :title="disabledTitle"
      :aria-label="disabled ? disabledTitle : 'Provider profile'"
      @change="onChange"
      @blur="onBlur"
    >
      <!-- placeholder option——selected 当 selectValue="" 且无可选项时显示 -->
      <option value="" disabled :selected="selectValue === ''">
        {{ currentDisplayLabel }}
      </option>

      <optgroup v-for="id in SECTION_PROVIDER_IDS" :key="id" :label="displayNameFor(id)">
        <option
          v-for="p in profilesFor(id)"
          :key="p.id"
          :value="p.id"
          :title="`${p.name} · ${p.default_model}`"
        >
          {{ optionLabel(p) }}
        </option>
      </optgroup>
    </select>

    <span class="selector-current" :title="currentDisplayTitle">{{ currentDisplayLabel }}</span>

    <span
      v-if="showLegacyHint"
      class="selector-hint"
      title="当前 Session 未绑定配置，使用应用默认模型"
    >
      Legacy
    </span>

    <span
      v-if="showSettingsHint"
      class="selector-hint selector-hint-warn"
      title="请在侧栏 Provider Settings 中配置"
    >
      请在侧栏 Provider Settings 中配置
    </span>
  </div>
</template>

<style scoped>
.provider-selector {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  font-size: 12px;
}
.selector-label {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.selector-select {
  max-width: 240px;
  font-size: 12px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: white;
  color: var(--fg);
  cursor: pointer;
}
.selector-select:disabled {
  opacity: 0.55;
  cursor: not-allowed;
  background: var(--code-bg);
}
.selector-current {
  font-size: 11px;
  color: var(--muted);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.selector-hint {
  font-size: 11px;
  color: var(--muted);
}
.selector-hint-warn {
  color: #92400e;
}
.selector-error {
  font-size: 11px;
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  padding: 2px 6px;
  border-radius: 4px;
}
</style>
