<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue"
import * as api from "../api"

const props = defineProps<{ refreshTick: number }>()

const events = ref<any[]>([])
const error = ref<string | null>(null)
let es: EventSource | null = null

async function load() {
  try {
    const resp = await api.getEvents()
    events.value = resp.events.slice(-200) // 最多展示 200 条，避免 DOM 爆炸
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

function startSSE() {
  if (es) es.close()
  es = new EventSource("/api/stream")
  es.addEventListener("event", (ev: MessageEvent) => {
    try {
      const payload = JSON.parse(ev.data)
      events.value.push(payload)
      if (events.value.length > 200) {
        events.value = events.value.slice(-200)
      }
    } catch {
      // ignore parse error
    }
  })
  es.onerror = () => {
    // 浏览器自动重连
  }
}

async function clearAll() {
  try {
    await api.clearEvents()
    events.value = []
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

function formatTs(ts: number | undefined): string {
  if (!ts) return ""
  const d = new Date(ts)
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0")
}

onMounted(() => {
  load()
  startSSE()
})

onUnmounted(() => {
  if (es) es.close()
})

watch(
  () => props.refreshTick,
  () => load(),
)
</script>

<template>
  <div>
    <div class="row" style="margin-bottom: 6px;">
      <strong>{{ events.length }}</strong>
      <span style="color: var(--muted);">events (max 200 shown)</span>
      <div class="grow"></div>
      <button @click="load">Refresh</button>
      <button @click="clearAll">Clear</button>
    </div>
    <div v-if="error" class="error-text">{{ error }}</div>
    <ul v-if="events.length" class="event-stream">
      <li v-for="(ev, i) in events" :key="i">
        <span class="ts">{{ formatTs(ev._received_at_ms || ev.timestamp) }}</span>
        <span class="type">{{ ev.type }}</span>
        <span style="color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{
          JSON.stringify(ev).slice(0, 200)
        }}</span>
      </li>
    </ul>
    <div v-else class="empty-state">no events yet</div>
  </div>
</template>
