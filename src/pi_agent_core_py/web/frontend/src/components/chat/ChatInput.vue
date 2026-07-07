<script setup lang="ts">
import { computed, ref } from "vue"

import type { FileRef } from "../../types"
import AttachmentBar from "./AttachmentBar.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"

const props = withDefaults(
  defineProps<{
    sending: boolean
    uploading?: boolean
    pendingAttachments?: FileRef[]
    sessionId?: string | null
    wsConnected?: boolean
  }>(),
  {
    uploading: false,
    pendingAttachments: () => [],
    sessionId: null,
    wsConnected: false,
  },
)

const emit = defineEmits<{
  (e: "submit", text: string): void
  (e: "abort"): void
  (e: "upload-files", files: FileList | File[]): void
  (e: "remove-attachment", fileId: string): void
}>()

const text = ref("")
const fileInput = ref<HTMLInputElement | null>(null)

const canSend = computed(() => {
  if (props.sending) return false
  if (text.value.trim()) return true
  return props.pendingAttachments.length > 0
})

function submit() {
  if (!canSend.value) return
  const t = text.value
  // 父组件决定是否清空——这里在 emit 后立刻清，父组件失败时通过 reset 函数还原
  emit("submit", t)
  text.value = ""
}

/**
 * 公开方法：父组件可在 sendPrompt 失败时还原输入。
 * Vue 3.5+ setup 中通过 defineExpose 暴露。
 */
function setText(t: string) {
  text.value = t
}
defineExpose({ setText })

function onKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    submit()
  }
}

function pickFiles() {
  // 重置 value——确保选同一文件能再次触发 change
  if (fileInput.value) fileInput.value.value = ""
  fileInput.value?.click()
}

function onFilesChosen(e: Event) {
  const target = e.target as HTMLInputElement
  if (!target.files || target.files.length === 0) return
  emit("upload-files", target.files)
  // 清空 input value 让用户可重复选择同一文件
  target.value = ""
}

function onDrop(e: DragEvent) {
  if (!e.dataTransfer?.files?.length) return
  e.preventDefault()
  emit("upload-files", e.dataTransfer.files)
}

function onDragOver(e: DragEvent) {
  // 阻止默认行为，让 drop 生效
  e.preventDefault()
}
</script>

<template>
  <div
    class="chat-input"
    data-testid="chat-input"
    @drop="onDrop"
    @dragover="onDragOver"
  >
    <AttachmentBar
      v-if="pendingAttachments.length > 0"
      :files="pendingAttachments"
      :session-id="sessionId"
      data-testid="attachment-bar"
      @remove="emit('remove-attachment', $event)"
    />

    <div class="input-row">
      <input
        ref="fileInput"
        type="file"
        multiple
        class="file-input-hidden"
        data-testid="file-input"
        @change="onFilesChosen"
      />

      <button
        class="attach-btn"
        data-testid="attach-button"
        :disabled="sending || uploading || !sessionId"
        :title="sessionId ? 'Attach files' : 'Select a session first'"
        aria-label="Attach files"
        @click="pickFiles"
      >
        <LoadingSpinner v-if="uploading" :size="14" />
        <span v-else>📎</span>
      </button>

      <textarea
        v-model="text"
        class="input-field"
        rows="1"
        data-testid="chat-input-field"
        :placeholder="
          sending
            ? 'Running…'
            : 'Message…  (Enter to send, Shift+Enter for newline)'
        "
        :disabled="sending"
        @keydown="onKey"
      ></textarea>

      <button
        v-if="!sending"
        class="send-btn primary"
        data-testid="send-button"
        :disabled="!canSend"
        title="Send (Enter)"
        @click="submit"
      >Send</button>
      <button
        v-else
        class="send-btn danger"
        data-testid="stop-button"
        title="Stop generation"
        @click="emit('abort')"
      >Stop</button>
    </div>

    <div class="input-hint">
      <span v-if="uploading">● uploading…</span>
      <span v-else-if="sending">● agent is running</span>
      <span v-else-if="!wsConnected" class="muted">○ disconnected · supports md / html / csv / parquet / text</span>
      <span v-else class="muted">connected · supports md / html / csv / parquet / text · images & PDF not parsed</span>
    </div>
  </div>
</template>

<style scoped>
.chat-input {
  max-width: var(--content-max-width);
  width: 100%;
  margin: 0 auto;
  padding: 8px 24px 16px;
}
.file-input-hidden {
  display: none;
}
.input-row {
  display: flex;
  gap: 6px;
  align-items: flex-end;
  background: white;
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  padding: 6px 6px 6px 8px;
}
.attach-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 16px;
  color: var(--muted);
  flex-shrink: 0;
}
.attach-btn:hover:not(:disabled) {
  background: var(--border);
  color: var(--fg);
}
.attach-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.input-field {
  flex: 1;
  border: none;
  outline: none;
  resize: none;
  font: inherit;
  font-size: 14px;
  line-height: 1.5;
  max-height: 160px;
  min-height: 24px;
  background: transparent;
  color: var(--fg);
  padding: 4px 0;
}
.send-btn {
  border: none;
  border-radius: 8px;
  padding: 8px 16px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  align-self: flex-end;
}
.send-btn.primary {
  background: var(--accent);
  color: white;
}
.send-btn.primary:hover:not(:disabled) {
  background: var(--accent-hover);
}
.send-btn.primary:disabled {
  background: var(--border-strong);
  color: var(--muted);
  cursor: not-allowed;
}
.send-btn.danger {
  background: var(--danger);
  color: white;
}
.send-btn.danger:hover {
  opacity: 0.85;
}
.input-hint {
  margin-top: 6px;
  font-size: 11px;
  color: var(--muted);
  padding: 0 4px;
}
.input-hint .muted {
  color: var(--muted);
}
</style>
