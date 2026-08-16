<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue"

import { abortRun, listSlashCommands } from "../../api"
import type { SlashCommandDefinition } from "../../types"
import { useChatStore } from "../../stores/chatStore"
import { useFileStore } from "../../stores/fileStore"
import { useProviderStore } from "../../stores/providerStore"
import { useSessionStore } from "../../stores/sessionStore"
import { useSkillStore } from "../../stores/skillStore"
import ChatInput from "./ChatInput.vue"
import MessageList from "./MessageList.vue"
import ProviderSelector from "../providers/ProviderSelector.vue"
import EmptyState from "../common/EmptyState.vue"
import ErrorBanner from "../common/ErrorBanner.vue"
import SessionFolderPanel from "./SessionFolderPanel.vue"

const sessionStore = useSessionStore()
const chatStore = useChatStore()
const fileStore = useFileStore()
const skillStore = useSkillStore()
const providerStore = useProviderStore()

const activeSessionId = computed(() => sessionStore.activeSessionId)
const hasSession = computed(() => !!activeSessionId.value)
const items = computed(() => chatStore.streamItems)
const errorMessage = computed(() => chatStore.error || fileStore.error)
const pendingAttachments = computed(() => fileStore.pendingAttachments)
const uploading = computed(() => fileStore.uploading)
const sessionFiles = computed(() => {
  const sid = activeSessionId.value
  return sid ? (fileStore.filesBySession[sid] ?? []) : []
})
const folderOpen = ref(true)
const slashCommands = ref<SlashCommandDefinition[]>([
  {
    name: "/checkpointer",
    description: "Summarize this conversation to Memory.md, then clear it.",
    requires_provider: true,
    accepts_arguments: false,
  },
])

/** 保留最后一次失败的输入文本——失败时还原，避免用户重新打字。 */
const lastFailedText = ref<string>("")
const chatInputRef = ref<InstanceType<typeof ChatInput> | null>(null)

watch(activeSessionId, () => {
  folderOpen.value = true
})

onMounted(async () => {
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
    }
  },
)

async function onUploadFiles(files: FileList | File[]) {
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

async function refreshFolder() {
  if (!activeSessionId.value) return
  await fileStore.loadFiles(activeSessionId.value)
}

async function deleteFolderFile(fileId: string) {
  if (!activeSessionId.value) return
  if (!window.confirm("Delete this file from the current session folder?")) return
  await fileStore.deleteFile(activeSessionId.value, fileId)
}

async function loadFolderFileContent(fileId: string) {
  if (!activeSessionId.value) throw new Error("No active session")
  return fileStore.readTextFile(activeSessionId.value, fileId)
}

async function saveFolderFileContent(
  fileId: string,
  content: string,
  expectedSha256: string,
) {
  if (!activeSessionId.value) throw new Error("No active session")
  return fileStore.updateTextFile(
    activeSessionId.value,
    fileId,
    content,
    expectedSha256,
  )
}

async function onSubmit(text: string) {
  if (!hasSession.value) return

  // 保留原始输入以便失败时还原
  lastFailedText.value = text

  const normalized = text.trim()
  if (normalized.startsWith("/")) {
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

  const fileIds = pendingAttachments.value.map((f) => f.id)
  const files = pendingAttachments.value.slice()

  try {
    await chatStore.sendPrompt({
      sessionId: activeSessionId.value!,
      text,
      fileIds,
      files,
      skillNames: [...skillStore.selectedSkillNames],
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

async function onAbort() {
  try {
    await abortRun("user clicked stop")
  } catch (e) {
    console.error("abort failed", e)
  }
}

function dismissError() {
  chatStore.error = null
  fileStore.error = null
}
</script>

<template>
  <div class="chat-panel">
    <header v-if="hasSession" class="chat-header">
      <span class="header-title">
        {{ sessionStore.sessions.find((s) => s.id === activeSessionId)?.title || "Conversation" }}
      </span>
      <ProviderSelector />
      <button
        type="button"
        class="folder-toggle"
        :aria-expanded="folderOpen"
        data-testid="session-folder-toggle"
        @click="folderOpen = !folderOpen"
      >
        Folder {{ sessionFiles.length }}
      </button>
      <span v-if="chatStore.checkpointing" class="header-status running">checkpointing</span>
      <span v-else-if="chatStore.sending" class="header-status running">running</span>
      <span v-else-if="chatStore.wsConnected" class="header-status online">online</span>
      <span v-else class="header-status offline">offline</span>
    </header>

    <SessionFolderPanel
      v-if="hasSession && folderOpen && activeSessionId"
      :files="sessionFiles"
      :session-id="activeSessionId"
      :loading="fileStore.loading"
      :load-content="loadFolderFileContent"
      :save-content="saveFolderFileContent"
      @refresh="refreshFolder"
      @delete="deleteFolderFile"
    />

    <ErrorBanner v-if="errorMessage" :message="errorMessage" dismissible @dismiss="dismissError" />
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

    <ChatInput
      v-if="hasSession"
      ref="chatInputRef"
      :sending="chatStore.sending"
      :uploading="uploading"
      :pending-attachments="pendingAttachments"
      :session-id="activeSessionId"
      :ws-connected="chatStore.wsConnected"
      :provider-ready="providerStore.canSendPrompt"
      :slash-commands="slashCommands"
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
.folder-toggle {
  padding: 3px 9px;
  border-color: var(--border);
  font-size: 11px;
  color: var(--muted);
  background: var(--chat-bg);
}
.folder-toggle[aria-expanded="true"] {
  color: var(--accent);
  border-color: var(--accent);
}
.header-status.running {
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
