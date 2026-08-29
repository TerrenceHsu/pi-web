<script setup lang="ts">
import { computed, ref } from "vue"

import type { FileRef, SlashCommandDefinition } from "../../types"
import AttachmentBar from "./AttachmentBar.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"

const props = withDefaults(
  defineProps<{
    sending: boolean
    uploading?: boolean
    pendingAttachments?: FileRef[]
    sessionId?: string | null
    wsConnected?: boolean
    /** Provider 是否就绪——由父组件从 providerStore.canSendPrompt 传入。
     * 默认 true 保持向后兼容（不传则不阻止发送）。 */
    providerReady?: boolean
    contextBlocked?: boolean
    slashCommands?: SlashCommandDefinition[]
    attachmentsEnabled?: boolean
    knowledgeMode?: boolean
    codingMode?: boolean
    codingModeAvailable?: boolean
    planMode?: boolean
    planModeAvailable?: boolean
  }>(),
  {
    uploading: false,
    pendingAttachments: () => [],
    sessionId: null,
    wsConnected: false,
    providerReady: true,
    contextBlocked: false,
    slashCommands: () => [],
    attachmentsEnabled: true,
    knowledgeMode: false,
    codingMode: false,
    codingModeAvailable: true,
    planMode: false,
    planModeAvailable: false,
  },
)

const emit = defineEmits<{
  (e: "submit", text: string): void
  (e: "abort"): void
  (e: "upload-files", files: FileList | File[]): void
  (e: "remove-attachment", fileId: string): void
  (e: "update:codingMode", enabled: boolean): void
  (e: "update:planMode", enabled: boolean): void
}>()

const text = ref("")
const fileInput = ref<HTMLInputElement | null>(null)
const selectedCommandIndex = ref(0)

const matchingCommands = computed(() => {
  const query = text.value.trimStart().toLocaleLowerCase()
  if (!query.startsWith("/") || query.includes("\n")) return []
  return props.slashCommands.filter((command) => command.name.toLocaleLowerCase().startsWith(query))
})

const showCommandMenu = computed(() => !props.sending && matchingCommands.value.length > 0)

const canSend = computed(() => {
  // Provider 未就绪——阻止所有发送路径（按钮 / Enter / submit / 附件 send）
  if (!props.providerReady) return false
  if (props.contextBlocked) return false
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
  if (showCommandMenu.value && e.key === "ArrowDown") {
    e.preventDefault()
    selectedCommandIndex.value = (selectedCommandIndex.value + 1) % matchingCommands.value.length
    return
  }
  if (showCommandMenu.value && e.key === "ArrowUp") {
    e.preventDefault()
    selectedCommandIndex.value =
      (selectedCommandIndex.value - 1 + matchingCommands.value.length) %
      matchingCommands.value.length
    return
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    const selected = matchingCommands.value[selectedCommandIndex.value]
    if (showCommandMenu.value && selected && text.value.trim() !== selected.name) {
      chooseCommand(selected)
      return
    }
    submit()
  }
}

function chooseCommand(command: SlashCommandDefinition) {
  text.value = command.name
  selectedCommandIndex.value = 0
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
  if (!props.attachmentsEnabled) return
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
  <div class="chat-input" data-testid="chat-input" @drop="onDrop" @dragover="onDragOver">
    <AttachmentBar
      v-if="attachmentsEnabled && pendingAttachments.length > 0"
      :files="pendingAttachments"
      :session-id="sessionId"
      data-testid="attachment-bar"
      @remove="emit('remove-attachment', $event)"
    />

    <div v-if="showCommandMenu" class="slash-menu" role="listbox" aria-label="Slash commands">
      <button
        v-for="(command, index) in matchingCommands"
        :key="command.name"
        type="button"
        class="slash-option"
        :class="{ selected: index === selectedCommandIndex }"
        role="option"
        :aria-selected="index === selectedCommandIndex"
        :data-testid="`slash-command-${command.name.slice(1)}`"
        @mousedown.prevent="chooseCommand(command)"
      >
        <strong>{{ command.name }}</strong>
        <span>{{ command.description }}</span>
      </button>
    </div>

    <div class="input-row">
      <button
        v-if="!knowledgeMode"
        type="button"
        class="coding-mode-btn"
        :class="{ active: codingMode }"
        data-testid="coding-mode-toggle"
        :aria-pressed="codingMode"
        :disabled="sending || !codingModeAvailable"
        :title="
          codingModeAvailable
            ? 'Automatically run this request in the managed Coding Sandbox'
            : 'Managed Coding Sandbox is unavailable'
        "
        @click="emit('update:codingMode', !codingMode)"
      >
        &lt;/&gt; Code
      </button>

      <button
        v-if="!knowledgeMode"
        type="button"
        class="coding-mode-btn plan-mode-btn"
        :class="{ active: planMode }"
        data-testid="plan-mode-toggle"
        :aria-pressed="planMode"
        :disabled="sending || !planModeAvailable"
        :title="
          planModeAvailable
            ? 'Use Planner, Executor, and Verifier for this Coding request'
            : 'Plan mode is unavailable'
        "
        @click="emit('update:planMode', !planMode)"
      >
        ☷ Plan
      </button>

      <input
        v-if="attachmentsEnabled"
        ref="fileInput"
        type="file"
        multiple
        class="file-input-hidden"
        data-testid="file-input"
        @change="onFilesChosen"
      />

      <button
        v-if="attachmentsEnabled"
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
            : knowledgeMode
              ? 'Ask this Wiki or propose an approved change…'
              : 'Message or / command…  (Enter to send)'
        "
        :disabled="sending"
        @keydown="onKey"
      ></textarea>

      <button
        v-if="!sending"
        class="send-btn primary"
        data-testid="send-button"
        :disabled="!canSend"
        :title="contextBlocked ? 'Compact context before sending' : 'Send (Enter)'"
        @click="submit"
      >
        Send
      </button>
      <button
        v-else
        class="send-btn danger"
        data-testid="stop-button"
        title="Stop generation"
        @click="emit('abort')"
      >
        Stop
      </button>
    </div>

    <div class="input-hint">
      <span v-if="uploading">● uploading…</span>
      <span v-else-if="sending">● agent is running</span>
      <span v-else-if="contextBlocked" class="context-blocked"
        >Context limit reached · compact before sending</span
      >
      <span v-else-if="knowledgeMode" class="muted"
        >Knowledge mode · approved Wiki pages and read-only Raw evidence · edits require
        approval</span
      >
      <span v-else-if="planMode" class="coding-hint"
        >Plan mode · approve tasks before Sandbox execution · Verifier checks each task</span
      >
      <span v-else-if="codingMode" class="coding-hint"
        >Coding mode · Sandbox starts automatically · validated changes require approval</span
      >
      <span v-else-if="!wsConnected" class="muted"
        >○ disconnected · supports md / html / csv / parquet / text</span
      >
      <span v-else class="muted"
        >connected · supports md / html / csv / parquet / text · images & PDF not parsed</span
      >
    </div>
  </div>
</template>

<style scoped>
.chat-input {
  max-width: var(--content-max-width);
  width: 100%;
  margin: 0 auto;
  padding: 8px 24px 16px;
  position: relative;
}
.slash-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 6px;
  padding: 5px;
  background: white;
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgb(15 23 42 / 12%);
}
.slash-option {
  display: grid;
  grid-template-columns: 130px 1fr;
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--fg);
  text-align: left;
  cursor: pointer;
}
.slash-option span {
  color: var(--muted);
  font-size: 12px;
}
.slash-option.selected,
.slash-option:hover {
  background: var(--border);
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
.coding-mode-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 68px;
  height: 32px;
  padding: 0 7px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: 600 11px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.coding-mode-btn:hover:not(:disabled),
.coding-mode-btn.active {
  border-color: #93c5fd;
  background: #eff6ff;
  color: #1d4ed8;
}
.coding-mode-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.coding-hint {
  color: #1d4ed8;
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
.context-blocked {
  color: #991b1b;
}
</style>
