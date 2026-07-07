<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue"
import * as api from "../api"

interface LiveAssistant {
  text: string
  toolCalls: any[]
}

const props = defineProps<{
  refreshTick: number
  running: boolean
  liveAssistant: LiveAssistant | null
}>()

const messages = ref<any[]>([])
const error = ref<string | null>(null)
const scrollContainer = ref<HTMLElement | null>(null)

async function load() {
  try {
    const resp = await api.getMessages()
    messages.value = resp.messages
    await nextTick()
    scrollToBottom()
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

function scrollToBottom() {
  const el = scrollContainer.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => props.refreshTick,
  () => load(),
  { immediate: true },
)

watch(
  () => props.running,
  async () => {
    await nextTick()
    scrollToBottom()
  },
)

function isUser(msg: any): boolean {
  return msg?.role === "user"
}
function isAssistant(msg: any): boolean {
  return msg?.role === "assistant"
}

function textBlocks(msg: any): string[] {
  const content = msg?.content
  if (!Array.isArray(content)) return []
  return content
    .filter((c: any) => c && c.type === "text" && typeof c.text === "string")
    .map((c: any) => c.text)
}

function toolCalls(msg: any): any[] {
  const content = msg?.content
  if (!Array.isArray(content)) return []
  return content.filter((c: any) => c && c.type === "toolCall")
}

function toolResultSummary(msg: any): string {
  const content = msg?.content
  if (!Array.isArray(content)) return JSON.stringify(content)
  return content
    .map((c: any) => {
      if (!c) return ""
      if (c.type === "text") return c.text
      if (c.type === "toolCall") return `<tool_call:${c.name}>`
      if (c.type === "toolResult") {
        const raw =
          typeof c.result === "string"
            ? c.result
            : JSON.stringify(c.result ?? c.output ?? "")
        const snippet = raw.length > 240 ? raw.slice(0, 240) + "…" : raw
        return `tool_result (${c.name || c.tool_call_id || "?"}): ${snippet}`
      }
      return `<${c.type}>`
    })
    .join("\n")
}

const showLive = computed(
  () => props.running && props.liveAssistant !== null,
)
const liveText = computed(() => props.liveAssistant?.text ?? "")
const liveToolCalls = computed(() => props.liveAssistant?.toolCalls ?? [])
const showTypingDots = computed(
  () => props.running && (props.liveAssistant === null || liveText.value === ""),
)

</script>

<template>
  <div ref="scrollContainer" class="msg-scroll">
    <div class="msg-container">
      <div v-if="error" class="error-text">{{ error }}</div>

      <template v-if="messages.length">
        <div
          v-for="(msg, i) in messages"
          :key="i"
          :class="[
            'msg-row',
            isUser(msg)
              ? 'msg-row-user'
              : isAssistant(msg)
                ? 'msg-row-assistant'
                : 'msg-row-tool',
          ]"
        >
          <div
            v-if="isUser(msg)"
            class="msg-bubble msg-bubble-user"
          >
            <span class="msg-bubble-text">{{ textBlocks(msg).join("\n") || "(empty user message)" }}</span>
          </div>

          <div v-else-if="isAssistant(msg)" class="msg-assistant">
            <div class="msg-avatar">AI</div>
            <div class="msg-assistant-body">
              <div
                v-for="(t, ti) in textBlocks(msg)"
                :key="ti"
                class="msg-assistant-text"
              >{{ t }}</div>
              <details v-if="toolCalls(msg).length" class="msg-tool-details">
                <summary>{{ toolCalls(msg).length }} tool call(s)</summary>
                <pre>{{ JSON.stringify(toolCalls(msg), null, 2) }}</pre>
              </details>
            </div>
          </div>

          <details v-else class="msg-tool-result">
            <summary>tool result</summary>
            <pre>{{ toolResultSummary(msg) }}</pre>
          </details>
        </div>
      </template>

      <div v-else-if="!error && !showTypingDots" class="msg-empty">
        <div class="msg-empty-title">Start a conversation</div>
        <div class="msg-empty-hint">
          Send a prompt below — the assistant will reply, call tools, and stream updates here.
        </div>
      </div>

      <div v-if="showLive || showTypingDots" class="msg-row msg-row-assistant">
        <div class="msg-assistant">
          <div class="msg-avatar">AI</div>
          <div class="msg-assistant-body">
            <div v-if="liveText" class="msg-assistant-text">
              {{ liveText }}<span class="stream-cursor"></span>
            </div>
            <div v-else class="msg-typing">
              <span></span><span></span><span></span>
            </div>
            <details v-if="liveToolCalls.length" class="msg-tool-details">
              <summary>{{ liveToolCalls.length }} tool call(s) in flight</summary>
              <pre>{{ JSON.stringify(liveToolCalls, null, 2) }}</pre>
            </details>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
