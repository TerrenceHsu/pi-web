<script setup lang="ts">
import { computed, ref, watch } from "vue"

import { useProviderStore } from "../../stores/providerStore"
import { useSessionStore } from "../../stores/sessionStore"
import type { CredentialView, ProviderProfileView, VisibleProviderId } from "../../types"
import ErrorBanner from "../common/ErrorBanner.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import Modal from "../common/Modal.vue"
import ProviderSection from "./ProviderSection.vue"

const props = defineProps<{
  open: boolean
  sessionId: string | null
}>()

const emit = defineEmits<{ (e: "close"): void }>()

const providerStore = useProviderStore()
const sessionStore = useSessionStore()

// ============================================================================
// 静态 display name fallback——避免 definitions 加载前空白
// ============================================================================

const DISPLAY_NAME_FALLBACK: Record<VisibleProviderId, string> = {
  glm: "GLM",
  qwen: "Qwen",
  kimi: "Kimi",
}

const SECTION_PROVIDER_IDS: VisibleProviderId[] = ["glm", "qwen", "kimi"]

function displayNameFor(id: VisibleProviderId): string {
  const match = providerStore.visibleDefinitions.find((d) => d.id === id)
  return match?.display_name ?? DISPLAY_NAME_FALLBACK[id]
}

// ============================================================================
// 每次 Modal 打开时捕获 openedSessionId——后续任何 mutation 都用它做 guard
// ============================================================================

const openedSessionId = ref<string | null>(null)

// ============================================================================
// 打开 / 关闭
// ============================================================================

watch(
  () => props.open,
  async (open) => {
    if (open) {
      // 1. 捕获当前 session 作为 openedSessionId
      openedSessionId.value = props.sessionId
      // 2. 重置 error 状态（直接赋值——M2-1 Pinia ref 允许）
      providerStore.loadError = null
      providerStore.mutationError = null
      providerStore.bindingLoadError = null
      // 3. 刷新全局数据——不重新加载 binding
      //    loadX 失败时抛 safe string——Modal 捕获并写入 loadError。
      const LOAD_ERROR_FALLBACK = "Provider configuration could not be loaded."
      try {
        await Promise.all([
          providerStore.loadDefinitions(),
          providerStore.loadProfiles(),
          providerStore.loadCredentials(),
        ])
      } catch (e) {
        // store loadX 已用 toSafeProviderError 包过——e 是安全字符串。
        providerStore.loadError = typeof e === "string" && e.length > 0 ? e : LOAD_ERROR_FALLBACK
      }
    } else {
      // 关闭——清 openedSessionId；ProfileForm 通过 v-if unmount 自动清 secret
      openedSessionId.value = null
    }
  },
  { immediate: true },
)

// ============================================================================
// Session 切换 guard——Modal 打开时 session 变化则立即关闭
// ============================================================================

watch(
  () => sessionStore.activeSessionId,
  (newId) => {
    if (props.open && openedSessionId.value && newId !== openedSessionId.value) {
      // 关闭 Modal——所有 ProfileForm 通过 Modal 的 v-if unmount 自动清 secret
      emit("close")
    }
  },
)

// ============================================================================
// Section 数据——从 store 直接派生
// ============================================================================

function profilesByProvider(id: VisibleProviderId): ProviderProfileView[] {
  return providerStore.profiles.filter((p) => p.provider_id === id)
}

// ============================================================================
// Unused credentials 折叠区
// ============================================================================

const showUnusedCredentials = ref(false)

const unusedCredentials = computed<CredentialView[]>(() => providerStore.unusedCredentials)

async function deleteUnusedCredential(credentialId: string) {
  if (!window.confirm("Delete this unused credential?")) return
  await providerStore.deleteCredential(credentialId)
}

// ============================================================================
// Errors
// ============================================================================

const combinedError = computed<string | null>(() => {
  return providerStore.loadError ?? providerStore.mutationError ?? null
})

function dismissErrors() {
  providerStore.loadError = null
  providerStore.mutationError = null
}

const isLoading = computed(
  () =>
    providerStore.definitionsLoading ||
    providerStore.profilesLoading ||
    providerStore.credentialsLoading,
)
</script>

<template>
  <Modal :open="open" title="Provider settings" @close="emit('close')">
    <p class="modal-intro">
      Manage provider profiles for GLM, Qwen, and Kimi. Configuration changes apply to future
      sessions only after you click Apply.
    </p>

    <ErrorBanner
      v-if="combinedError"
      :message="combinedError"
      dismissible
      data-testid="provider-modal-error"
      @dismiss="dismissErrors"
    />

    <div v-if="isLoading" class="loading-row">
      <LoadingSpinner :size="14" />
      <span>Loading…</span>
    </div>

    <ProviderSection
      v-for="id in SECTION_PROVIDER_IDS"
      :key="id"
      :provider-id="id"
      :provider-display-name="displayNameFor(id)"
      :profiles="profilesByProvider(id)"
      :credentials="providerStore.credentials"
      :session-id="sessionId"
    />

    <section class="unused-section">
      <button
        type="button"
        class="unused-toggle"
        data-testid="unused-credentials-toggle"
        @click="showUnusedCredentials = !showUnusedCredentials"
      >
        {{ showUnusedCredentials ? "▼" : "▶" }} Unused credentials ({{ unusedCredentials.length }})
      </button>
      <div v-if="showUnusedCredentials" class="unused-list">
        <p v-if="unusedCredentials.length === 0" class="unused-empty">No unused credentials.</p>
        <div
          v-for="c in unusedCredentials"
          :key="c.credential_id"
          class="unused-item"
          :data-testid="`unused-credential-${c.credential_id}`"
        >
          <div class="unused-item-info">
            <span class="unused-label">{{ c.label }}</span>
            <span class="unused-meta">
              {{ c.masked_value }} · {{ c.storage_mode }} · {{ c.storage_status }}
            </span>
          </div>
          <button
            type="button"
            class="unused-delete"
            :data-testid="`delete-unused-${c.credential_id}`"
            @click="deleteUnusedCredential(c.credential_id)"
          >
            Delete
          </button>
        </div>
      </div>
    </section>
  </Modal>
</template>

<style scoped>
.modal-intro {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 13px;
}
.loading-row {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
  padding: 6px 0;
}
.unused-section {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.unused-toggle {
  background: none;
  border: none;
  font-size: 13px;
  color: var(--muted);
  cursor: pointer;
  padding: 4px 0;
}
.unused-toggle:hover {
  color: var(--fg);
}
.unused-list {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.unused-empty {
  font-size: 12px;
  color: var(--muted);
  margin: 0;
  padding: 6px 0;
}
.unused-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--code-bg, #fafafa);
}
.unused-item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.unused-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--fg);
}
.unused-meta {
  font-size: 11px;
  color: var(--muted);
}
.unused-delete {
  padding: 4px 8px;
  font-size: 11px;
  border: 1px solid #fecaca;
  background: white;
  color: #b91c1c;
  border-radius: 4px;
  cursor: pointer;
}
.unused-delete:hover {
  background: #fef2f2;
}
</style>
