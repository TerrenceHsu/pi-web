<script setup lang="ts">
import { computed, ref } from "vue"

import { abortRun } from "../../api"
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

/** 保留最后一次失败的输入文本——失败时还原，避免用户重新打字。 */
const lastFailedText = ref<string>("")
const chatInputRef = ref<InstanceType<typeof ChatInput> | null>(null)

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

async function onSubmit(text: string) {
  if (!hasSession.value) return

  // 保留原始输入以便失败时还原
  lastFailedText.value = text

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
      <span v-if="chatStore.sending" class="header-status running">running</span>
      <span v-else-if="chatStore.wsConnected" class="header-status online">online</span>
      <span v-else class="header-status offline">offline</span>
    </header>

    <ErrorBanner v-if="errorMessage" :message="errorMessage" dismissible @dismiss="dismissError" />

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
</style>
