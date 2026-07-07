<script setup lang="ts">
import { ref, watch } from "vue"
import * as api from "../api"
import type { SessionResponse } from "../types"

const props = defineProps<{ refreshTick: number }>()

const session = ref<SessionResponse | null>(null)
const error = ref<string | null>(null)

async function load() {
  try {
    session.value = await api.getSession()
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

watch(
  () => props.refreshTick,
  () => load(),
  { immediate: true },
)
</script>

<template>
  <div>
    <div v-if="error" class="error-text">{{ error }}</div>
    <div v-if="!session" class="empty-state">loading...</div>
    <div v-else-if="!session.attached" class="empty-state">
      no session attached
    </div>
    <div v-else style="padding: 12px;">
      <h2>{{ session.id }}</h2>
      <p v-if="session.title" style="color: var(--muted);">
        title: {{ session.title }}
      </p>
      <p>
        turns: <strong>{{ session.turn_count }}</strong>,
        messages: <strong>{{ session.message_count }}</strong>,
        snapshots: <strong>{{ session.snapshot_count }}</strong>
      </p>
      <h2>metadata</h2>
      <pre>{{ JSON.stringify(session.metadata ?? {}, null, 2) }}</pre>
      <h2>messages ({{ session.messages?.length ?? 0 }})</h2>
      <pre>{{ JSON.stringify(session.messages ?? [], null, 2) }}</pre>
    </div>
  </div>
</template>
