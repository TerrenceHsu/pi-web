<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue"

import { abortRun, getState } from "../../api/state"
import { getContextSource } from "../../api/contextBudget"
import { ApiError } from "../../api/client"
import { listSlashCommands } from "../../api/slashCommands"
import type { ContextSourcePage, SlashCommandDefinition } from "../../types"
import { useChatStore } from "../../stores/chatStore"
import { useCodingSandboxStore } from "../../stores/codingSandboxStore"
import { useContextBudgetStore } from "../../stores/contextBudgetStore"
import { useFileStore } from "../../stores/fileStore"
import { useProviderStore } from "../../stores/providerStore"
import { useSessionStore } from "../../stores/sessionStore"
import { useSkillStore } from "../../stores/skillStore"
import ChatInput from "./ChatInput.vue"
import MessageList from "./MessageList.vue"
import ProviderSelector from "../providers/ProviderSelector.vue"
import EmptyState from "../common/EmptyState.vue"
import ErrorBanner from "../common/ErrorBanner.vue"
import ContextBudgetBadge from "./ContextBudgetBadge.vue"
import SandboxApprovalBar from "../coding-sandbox/SandboxApprovalBar.vue"

const props = withDefaults(defineProps<{ mode?: "default" | "knowledge" }>(), {
  mode: "default",
})
const emit = defineEmits<{ (event: "open-workspace"): void }>()

const sessionStore = useSessionStore()
const chatStore = useChatStore()
const codingSandboxStore = useCodingSandboxStore()
const contextBudgetStore = useContextBudgetStore()
const fileStore = useFileStore()
const skillStore = useSkillStore()
const providerStore = useProviderStore()
const knowledgeMode = computed(() => props.mode === "knowledge")
const codingMode = ref(false)
const planMode = ref(false)
const planModeEnabled = ref(false)
const planModeAvailable = computed(() => planModeEnabled.value && codingSandboxStore.available)

const activeSessionId = computed(() => sessionStore.activeSessionId)
const hasSession = computed(() => !!activeSessionId.value)
const items = computed(() => chatStore.streamItems)
const errorMessage = computed(
  () => chatStore.error || codingSandboxStore.error || fileStore.error || contextBudgetStore.error,
)
const contextBudget = computed(() => contextBudgetStore.getBudget(activeSessionId.value))
const compaction = computed(() => contextBudgetStore.getCompaction(activeSessionId.value))
const contextBlocked = computed(() => contextBudget.value?.estimate.level === "blocked"
  && compaction.value?.can_auto_compact !== true)
const contextDetailsOpen = ref(false)
const sourcePage = ref<ContextSourcePage | null>(null)
const sourceEntryId = ref<string | null>(null)
const sourceLoading = ref(false)
const sourceError = ref<string | null>(null)
const visibleSourceCount = ref(50)
let sourceVersion = 0
const compactionStatusLabel = computed(() => {
  if (contextBudgetStore.compactingSessionId === activeSessionId.value) return "Compacting…"
  const labels: Record<string, string> = {
    idle: "Not compacted",
    prepared: "Compacting…",
    committed: "Summary active",
    failed: "Compaction failed; previous context retained",
    interrupted: "Compaction interrupted; previous context retained",
  }
  const status = compaction.value?.status ?? "idle"
  return labels[status] ?? status
})
const compactionTokenChange = computed(() => {
  const stats = compaction.value?.token_stats
  const before = stats?.estimated_input_tokens_before
  const after = stats?.estimated_input_tokens_after
  return typeof before === "number" && typeof after === "number"
    ? `Estimated input: ~${before.toLocaleString()} → ~${after.toLocaleString()} tokens`
    : null
})
const pendingAttachments = computed(() => fileStore.pendingAttachments)
const uploading = computed(() => fileStore.uploading)
const slashCommands = ref<SlashCommandDefinition[]>([
  {
    name: "/checkpointer",
    description: "Summarize this conversation to Memory.md, then clear it.",
    requires_provider: true,
    accepts_arguments: false,
  },
])

watch(activeSessionId, () => {
  contextDetailsOpen.value = false
  clearContextSource()
})

watch(
  () => [activeSessionId.value, compaction.value?.active_projection_id] as const,
  ([sessionId, projectionId], previous) => {
    if (previous?.[0] === sessionId && previous?.[1] === projectionId) return
    clearContextSource()
    visibleSourceCount.value = 50
    if (!knowledgeMode.value && sessionId && projectionId
      && (previous?.[0] !== sessionId || previous?.[1] !== projectionId)) {
      void contextBudgetStore.loadCompaction(sessionId)
    }
  },
  { immediate: true },
)

function clearContextSource() {
  sourceVersion += 1
  sourcePage.value = null
  sourceEntryId.value = null
  sourceLoading.value = false
  sourceError.value = null
}

async function toggleContextDetails() {
  contextDetailsOpen.value = !contextDetailsOpen.value
  if (contextDetailsOpen.value && activeSessionId.value) {
    await contextBudgetStore.loadCompaction(activeSessionId.value)
  }
}

async function setAutoCompaction(event: Event) {
  const sessionId = activeSessionId.value
  if (!sessionId || chatStore.sending) return
  const checkbox = event.target as HTMLInputElement
  const enabled = checkbox.checked
  await contextBudgetStore.setAutoCompaction(sessionId, enabled)
  // The pending control reflects the click; failed saves restore the confirmed setting.
  if (activeSessionId.value === sessionId) checkbox.checked = compaction.value?.auto_compact === true
}

async function showContextSource(entryId: string, offset = 0) {
  const sessionId = activeSessionId.value
  if (!sessionId) return
  const version = ++sourceVersion
  sourceEntryId.value = entryId
  sourcePage.value = null
  sourceLoading.value = true
  sourceError.value = null
  try {
    const page = await getContextSource(sessionId, entryId, offset)
    if (version !== sourceVersion || activeSessionId.value !== sessionId) return
    if (page.entry_id !== entryId) throw new Error("context_source_mismatch")
    sourcePage.value = page
  } catch (cause) {
    if (version === sourceVersion) {
      sourceError.value = cause instanceof ApiError ? cause.detail : "Source could not be loaded."
    }
  } finally {
    if (version === sourceVersion) sourceLoading.value = false
  }
}

watch(
  () => codingSandboxStore.available,
  (available) => {
    if (!available) {
      codingMode.value = false
      planMode.value = false
    }
  },
)

function setCodingMode(enabled: boolean) {
  codingMode.value = enabled
  if (!enabled) planMode.value = false
}

function setPlanMode(enabled: boolean) {
  planMode.value = enabled
  if (enabled) codingMode.value = true
}

/** 保留最后一次失败的输入文本——失败时还原，避免用户重新打字。 */
const lastFailedText = ref<string>("")
const chatInputRef = ref<InstanceType<typeof ChatInput> | null>(null)

onMounted(async () => {
  if (knowledgeMode.value) {
    slashCommands.value = []
    return
  }
  try {
    const state = await getState()
    planModeEnabled.value = state.plan_mode?.enabled === true
  } catch (e) {
    console.error("load Plan mode state failed", e)
    planModeEnabled.value = false
  }
  try {
    const catalog = await listSlashCommands()
    if (Array.isArray(catalog?.commands)) {
      slashCommands.value = catalog.commands
    }
  } catch (e) {
    console.error("load slash commands failed", e)
  }
})

// async prompt 在 202 后仍继续运行；等真正 terminal 时刷新目录，
// 让 write_file 创建的文件无需刷新页面即可出现。
watch(
  () => chatStore.sending,
  (sending, wasSending) => {
    if (wasSending && !sending && activeSessionId.value) {
      void fileStore
        .loadFiles(activeSessionId.value)
        .catch((e) => console.error("reload session folder failed", e))
      void contextBudgetStore.load(activeSessionId.value)
    }
  },
)

async function onUploadFiles(files: FileList | File[]) {
  if (knowledgeMode.value) return
  if (!activeSessionId.value) {
    fileStore.error = "No active session"
    return
  }
  try {
    await fileStore.uploadFiles(activeSessionId.value, files)
    // fileStore 已在 pendingAttachments 中加入成功项；错误进入 fileStore.error
  } catch (e) {
    console.error("uploadFiles failed", e)
  }
}

function onRemoveAttachment(fileId: string) {
  fileStore.removePendingAttachment(fileId)
}

async function onSubmit(text: string) {
  if (!hasSession.value) return

  // 保留原始输入以便失败时还原
  lastFailedText.value = text

  const normalized = text.trim()
  if (!knowledgeMode.value && normalized.startsWith("/")) {
    if (normalized.toLocaleLowerCase() !== "/checkpointer") {
      chatStore.error = `Unknown slash command: ${normalized.split(/\s+/, 1)[0]}`
      chatInputRef.value?.setText(text)
      return
    }
    try {
      await chatStore.executeCheckpointer(activeSessionId.value!)
      fileStore.clearPendingAttachments()
      await fileStore.loadFiles(activeSessionId.value!)
    } catch {
      // Canonical messages stay untouched on failure; restore the command for retry.
      chatInputRef.value?.setText(lastFailedText.value)
    }
    return
  }

  const fileIds = knowledgeMode.value ? [] : pendingAttachments.value.map((f) => f.id)
  const files = knowledgeMode.value ? [] : pendingAttachments.value.slice()
  const skillNames = knowledgeMode.value || planMode.value ? [] : [...skillStore.selectedSkillNames]

  try {
    const preview = await contextBudgetStore.preview(activeSessionId.value!, {
      text,
      file_ids: fileIds,
      skill_names: skillNames,
      coding_mode: codingMode.value || planMode.value,
    })
    if (preview?.estimate.level === "blocked" && preview.compaction?.can_auto_compact !== true) {
      contextBudgetStore.error =
        "Context budget exceeded. Compact this conversation before sending."
      chatInputRef.value?.setText(lastFailedText.value)
      return
    }
    await chatStore.sendPrompt({
      sessionId: activeSessionId.value!,
      text,
      fileIds,
      files,
      skillNames,
      codingMode: codingMode.value || planMode.value,
      executionMode: planMode.value ? "plan" : "direct",
    })
    // 成功：清空 pending；reload session files（让侧栏其它视图也同步）
    fileStore.clearPendingAttachments()
    if (activeSessionId.value) {
      fileStore
        .loadFiles(activeSessionId.value)
        .catch((e) => console.error("reload files failed", e))
    }
  } catch {
    // 失败：还原输入文本；不清空 pending attachments
    chatInputRef.value?.setText(lastFailedText.value)
  }
}

async function onCompactContext() {
  const sessionId = activeSessionId.value
  if (!sessionId || chatStore.sending) return
  contextDetailsOpen.value = true
  await contextBudgetStore.compact(sessionId)
}

async function onAbort() {
  try {
    await abortRun("user clicked stop")
  } catch (e) {
    console.error("abort failed", e)
  }
}

function dismissError() {
  chatStore.error = null
  codingSandboxStore.error = null
  fileStore.error = null
  contextBudgetStore.error = null
}
</script>

<template>
  <div class="chat-panel">
    <header v-if="hasSession" class="chat-header">
      <span class="header-title">
        {{ sessionStore.sessions.find((s) => s.id === activeSessionId)?.title || "Conversation" }}
      </span>
      <ProviderSelector />
      <ContextBudgetBadge
        :budget="contextBudget"
        :loading="contextBudgetStore.loadingSessionId === activeSessionId"
        :compacting="contextBudgetStore.compactingSessionId === activeSessionId"
        :disabled="chatStore.sending"
        :management-enabled="!knowledgeMode"
        @compact="onCompactContext"
        @show-details="toggleContextDetails"
      />
      <span v-if="chatStore.checkpointing" class="header-status running">checkpointing</span>
      <span
        v-else-if="
          ['awaiting_approval', 'publish_conflict'].includes(
            codingSandboxStore.operation?.status ?? '',
          )
        "
        class="header-status approval"
      >
        {{
          codingSandboxStore.operation?.status === "publish_conflict"
            ? "code publish needs attention"
            : "code approval required"
        }}
      </span>
      <span v-else-if="chatStore.pendingApprovalCount" class="header-status approval">
        approval required
      </span>
      <span v-else-if="chatStore.sending" class="header-status running">running</span>
      <span v-else-if="chatStore.wsConnected" class="header-status online">online</span>
      <span v-else class="header-status offline">offline</span>
    </header>

    <ErrorBanner v-if="errorMessage" :message="errorMessage" dismissible @dismiss="dismissError" />
    <section
      v-if="hasSession && !knowledgeMode && (contextDetailsOpen || compaction?.active_projection_id)"
      class="context-management"
      :data-testid="compaction?.active_projection_id ? 'context-summary-card' : 'context-management-panel'"
      aria-label="Working context summary"
    >
      <div class="context-management-header">
        <strong>Working context</strong>
        <span data-testid="context-compaction-status" role="status">{{ compactionStatusLabel }}</span>
        <label>
          <input
            type="checkbox"
            data-testid="context-auto-compact-toggle"
            :checked="compaction?.auto_compact === true"
            :disabled="!compaction || chatStore.sending || contextBudgetStore.settingsSessionId === activeSessionId"
            @change="setAutoCompaction"
          >
          Auto-compact
        </label>
        <button type="button" :aria-expanded="contextDetailsOpen" @click="toggleContextDetails">
          {{ contextDetailsOpen ? "Hide details" : "View summary" }}
        </button>
      </div>
      <template v-if="contextDetailsOpen">
        <p class="context-note">Only the model's working context is shortened. Original chat and Memory are unchanged.</p>
        <p v-if="compactionTokenChange" data-testid="context-token-change">{{ compactionTokenChange }}</p>
        <p v-if="compaction?.active_projection_id">
          Covers {{ compaction.covered_message_count }} original messages · {{ compaction.active_projection_id }}
        </p>
        <p v-if="compaction?.circuit_open" class="context-error">
          自动压缩已因连续失败暂停。旧上下文仍然保留，可点击 Compact 手动重试。
        </p>
        <p v-if="compaction?.error_code" class="context-error" data-testid="context-compaction-error">
          压缩未完成，旧上下文仍然保留：{{ compaction.error_code }}
        </p>
        <pre v-if="compaction?.summary_text" class="context-summary-text" data-testid="context-summary-text">{{ compaction.summary_text }}</pre>
        <p v-else class="context-note">No working summary is available yet.</p>
        <div v-if="compaction?.source_entry_ids?.length" class="context-sources" aria-label="Summary sources">
          <span>Original sources:</span>
          <button
            v-for="entryId in compaction.source_entry_ids.slice(0, visibleSourceCount)"
            :key="entryId"
            type="button"
            data-testid="context-source-button"
            @click="showContextSource(entryId)"
          >{{ entryId }}</button>
          <button
            v-if="compaction.source_entry_ids.length > visibleSourceCount"
            type="button"
            @click="visibleSourceCount += 50"
          >Show more sources ({{ compaction.source_entry_ids.length - visibleSourceCount }} remaining)</button>
        </div>
        <div v-if="sourceEntryId" class="context-source" data-testid="context-source-preview">
          <div class="context-management-header">
            <strong>{{ sourceEntryId }}</strong>
            <button type="button" @click="clearContextSource">Close source</button>
          </div>
          <p v-if="sourceLoading" role="status">Loading source…</p>
          <p v-if="sourceError" role="alert" class="context-error">{{ sourceError }}</p>
          <template v-if="sourcePage">
            <pre>{{ sourcePage.text }}</pre>
            <div class="context-source-pagination">
              <span>Characters {{ sourcePage.offset }}–{{ sourcePage.next_offset ?? sourcePage.total_chars }} of {{ sourcePage.total_chars }}</span>
              <button v-if="sourcePage.offset > 0" type="button" @click="showContextSource(sourceEntryId, Math.max(0, sourcePage.offset - 6000))">Previous page</button>
              <button v-if="sourcePage.next_offset !== null" type="button" @click="showContextSource(sourceEntryId, sourcePage.next_offset)">Next page</button>
            </div>
          </template>
        </div>
      </template>
    </section>
    <div
      v-if="chatStore.checkpointNotice"
      class="checkpoint-notice"
      data-testid="checkpoint-notice"
    >
      <span>{{ chatStore.checkpointNotice }}</span>
      <button
        type="button"
        aria-label="Dismiss checkpoint notice"
        @click="chatStore.checkpointNotice = null"
      >
        ×
      </button>
    </div>

    <MessageList
      v-if="hasSession"
      :items="items"
      :sending="chatStore.sending"
      :session-id="activeSessionId"
    />
    <EmptyState v-else title="No session" hint="Click 'New chat' in the sidebar to start." />

    <SandboxApprovalBar v-if="!knowledgeMode" @open-workspace="emit('open-workspace')" />

    <ChatInput
      v-if="hasSession"
      ref="chatInputRef"
      :sending="chatStore.sending"
      :uploading="uploading"
      :pending-attachments="pendingAttachments"
      :session-id="activeSessionId"
      :ws-connected="chatStore.wsConnected"
      :provider-ready="providerStore.canSendPrompt"
      :context-blocked="contextBlocked"
      :slash-commands="slashCommands"
      :attachments-enabled="!knowledgeMode"
      :knowledge-mode="knowledgeMode"
      :coding-mode="codingMode"
      :coding-mode-available="codingSandboxStore.available"
      :plan-mode="planMode"
      :plan-mode-available="planModeAvailable"
      @update:coding-mode="setCodingMode"
      @update:plan-mode="setPlanMode"
      @submit="onSubmit"
      @abort="onAbort"
      @upload-files="onUploadFiles"
      @remove-attachment="onRemoveAttachment"
    />
  </div>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
.chat-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 24px;
  border-bottom: 1px solid var(--sidebar-border);
  background: var(--chat-bg);
  font-size: 13px;
}
.context-management {
  flex-shrink: 0;
  max-height: 45%;
  overflow: auto;
  margin: 8px 24px 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--chat-bg);
  color: var(--fg);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.context-management-header, .context-sources, .context-source-pagination {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.context-management-header label { margin-left: auto; }
.context-management-header label, .context-note { color: var(--muted); }
.context-management pre {
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 10px;
  border-radius: 6px;
  background: var(--code-bg);
}
.context-management button {
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--chat-bg);
  color: var(--fg);
  cursor: pointer;
  overflow-wrap: anywhere;
}
.context-management input:disabled { cursor: not-allowed; }
.context-error { color: #991b1b; }
.context-source { margin-top: 12px; }
.header-title {
  flex: 1;
  font-weight: 500;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.header-status {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--code-bg);
  color: var(--muted);
}
.header-status.running {
  background: #fef3c7;
  color: #92400e;
}
.header-status.approval {
  background: #fef3c7;
  color: #92400e;
}
.header-status.online {
  background: #dcfce7;
  color: #166534;
}
.header-status.offline {
  background: #fee2e2;
  color: #991b1b;
}
.checkpoint-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 8px 24px 0;
  padding: 8px 12px;
  border: 1px solid #a7d7b8;
  border-radius: 8px;
  background: #eefaf2;
  color: #24613a;
  font-size: 12px;
}
.checkpoint-notice button {
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
</style>
