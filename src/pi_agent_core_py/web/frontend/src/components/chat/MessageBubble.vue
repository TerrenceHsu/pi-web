<script setup lang="ts">
import { computed } from "vue"

import type { ChatStreamItem, FileRef } from "../../types"
import { useChatStore } from "../../stores/chatStore"
import ErrorCard from "./ErrorCard.vue"
import FileChip from "./FileChip.vue"
import FileReadCard from "./FileReadCard.vue"
import MCPToolCard from "./MCPToolCard.vue"
import SkillUsedCard from "./SkillUsedCard.vue"
import ToolCallCard from "./ToolCallCard.vue"
import ToolResultCard from "./ToolResultCard.vue"
import TurnInfoCard from "./TurnInfoCard.vue"

const props = withDefaults(
  defineProps<{
    item: ChatStreamItem
    /** 用户消息中的 FileChip 是否可下载——需要 sessionId 才能拼下载链接 */
    sessionId?: string | null
  }>(),
  { sessionId: null },
)

const chatStore = useChatStore()

const kind = computed(() => props.item.kind)
const text = computed(() => {
  const it: any = props.item
  if (kind.value === "user_message" || kind.value === "assistant_message") {
    return it.content || ""
  }
  return ""
})

const userFiles = computed<FileRef[]>(() => {
  if (kind.value !== "user_message") return []
  const files = (props.item as any).files
  if (!Array.isArray(files) || files.length === 0) return []
  return files as FileRef[]
})

/**
 * D2-7: 是否显示 Regenerate 按钮——审核 §5 显示条件全检：
 * - assistant role
 * - persisted === true
 * - messageId 存在
 * - 是当前 session 最新 persisted assistant（与 chatStore computed 比对）
 * - 无 active request（sending/streaming/aborting 都 false）
 * - 不是临时 draft（streaming-only 或 isRegenerationDraft）
 */
const canRegenerate = computed(() => {
  const it: any = props.item
  if (kind.value !== "assistant_message") return false
  if (it.persisted !== true) return false
  if (!it.messageId) return false
  if (it.isRegenerationDraft) return false
  if (it.streaming) return false
  // 必须是最新 persisted assistant
  if (chatStore.latestPersistedAssistantMessageId !== it.messageId) return false
  // 无 active request
  if (chatStore.sending || chatStore.streaming || chatStore.aborting) return false
  if (chatStore.currentRequestId) return false
  // regeneration 进行中——禁用
  const regStatus = chatStore.regeneration?.status
  if (regStatus === "queued" || regStatus === "running" || regStatus === "syncing") {
    return false
  }
  return true
})

async function onRegenerate() {
  if (!canRegenerate.value) return
  const it: any = props.item
  if (!it.messageId || !props.sessionId) return
  try {
    await chatStore.regenerateAssistantMessage({
      sessionId: props.sessionId,
      assistantMessageId: it.messageId,
    })
  } catch {
    // 错误已写入 chatStore.error + push ErrorItem；此处不二次处理
  }
}
</script>

<template>
  <!-- user_message -->
  <div v-if="kind === 'user_message'" class="row row-user" data-testid="user-message">
    <div class="bubble-wrap">
      <div v-if="text" class="bubble-user">{{ text }}</div>
      <div v-if="userFiles.length > 0" class="bubble-files">
        <FileChip
          v-for="f in userFiles"
          :key="f.id"
          :file="f"
          :session-id="sessionId"
          :removable="false"
          :downloadable="true"
        />
      </div>
    </div>
  </div>

  <!-- assistant_message -->
  <div v-else-if="kind === 'assistant_message'" class="row row-assistant" data-testid="assistant-message">
    <div class="avatar">AI</div>
    <div class="body">
      <div v-if="text" class="text">
        {{ text }}<span v-if="(item as any).streaming" class="stream-cursor">▋</span>
      </div>
      <div v-else-if="(item as any).streaming" class="typing">
        <span></span><span></span><span></span>
      </div>
      <!-- D2-7: Regenerate 按钮——仅在最新 persisted assistant + 无 active request 时显示 -->
      <div v-if="canRegenerate" class="regen-actions">
        <button
          type="button"
          class="regen-btn"
          data-testid="message-regenerate-btn"
          aria-label="Regenerate response"
          @click="onRegenerate"
        >
          Regenerate
        </button>
      </div>
    </div>
  </div>

  <!-- error -->
  <div v-else-if="kind === 'error'" class="row row-card">
    <ErrorCard :item="item as any" />
  </div>

  <!-- turn_info -->
  <div v-else-if="kind === 'turn_info'" class="row row-card">
    <TurnInfoCard :item="item as any" />
  </div>

  <!-- tool_call -->
  <div v-else-if="kind === 'tool_call'" class="row row-card">
    <ToolCallCard :item="item as any" />
  </div>

  <!-- tool_result -->
  <div v-else-if="kind === 'tool_result'" class="row row-card">
    <ToolResultCard :item="item as any" />
  </div>

  <!-- file_read -->
  <div v-else-if="kind === 'file_read'" class="row row-card">
    <FileReadCard :item="item as any" />
  </div>

  <!-- mcp_tool_call -->
  <div v-else-if="kind === 'mcp_tool_call'" class="row row-card">
    <MCPToolCard :item="item as any" />
  </div>

  <!-- skill_used -->
  <div v-else-if="kind === 'skill_used'" class="row row-card">
    <SkillUsedCard :item="item as any" />
  </div>

  <!-- fallback：未知 kind 仍走 muted 占位 -->
  <div v-else class="row row-card">
    <details class="muted-card">
      <summary>
        <span class="muted-kind">{{ (item as any).kind }}</span>
        <span class="muted-title">unknown</span>
      </summary>
      <pre>{{ JSON.stringify(item, null, 2) }}</pre>
    </details>
  </div>
</template>

<style scoped>
.row {
  width: 100%;
  display: flex;
  margin: 8px 0;
}
.row-user {
  justify-content: flex-end;
}
.bubble-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  max-width: var(--content-max-width);
}
.bubble-user {
  background: var(--user-bubble);
  color: var(--user-bubble-fg);
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
  max-width: 100%;
}
.row-assistant {
  justify-content: flex-start;
  align-items: flex-start;
}
.row-assistant .body {
  max-width: var(--content-max-width);
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--border-strong);
  color: var(--fg);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  margin-right: 8px;
  flex-shrink: 0;
  margin-top: 2px;
}
.text {
  color: var(--assistant-fg);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.stream-cursor {
  display: inline-block;
  margin-left: 2px;
  color: var(--accent);
  animation: blink 0.8s infinite;
}
@keyframes blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
}
.typing {
  display: inline-flex;
  gap: 4px;
  padding: 4px 0;
}
.typing span {
  width: 6px;
  height: 6px;
  background: var(--muted);
  border-radius: 50%;
  animation: typing-bounce 1.2s infinite ease-in-out;
}
.typing span:nth-child(2) { animation-delay: 0.15s; }
.typing span:nth-child(3) { animation-delay: 0.3s; }
@keyframes typing-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-4px); opacity: 1; }
}

.row-card {
  justify-content: center;
  margin: 4px 0;
}
.row-card > * {
  max-width: var(--content-max-width);
  width: 100%;
}

.muted-card {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 12px;
  color: var(--muted);
  padding: 4px 10px;
}
.muted-card summary {
  cursor: pointer;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 2px 0;
}
.muted-kind {
  font-family: ui-monospace, monospace;
  background: var(--border);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
}
.muted-card pre {
  margin: 6px 0;
  padding: 6px 8px;
  background: white;
  border-radius: 3px;
  font-size: 11px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

/* D2-7: Regenerate 按钮 */
.regen-actions {
  margin-top: 6px;
  display: flex;
  gap: 6px;
}
.regen-btn {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--code-bg);
  color: var(--muted);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.regen-btn:hover {
  background: var(--border);
  color: var(--fg);
}
</style>
