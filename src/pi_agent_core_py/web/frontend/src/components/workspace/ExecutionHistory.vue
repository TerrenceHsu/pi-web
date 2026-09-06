<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue"
import { requestJson } from "../../api/client"

interface Task {
  task_id: string; kind: string; state: string; cleanup_pending: boolean
  commands_used: number; max_commands: number; charged_ms: number; max_execution_ms: number
}
interface Detail extends Task {
  logs: { command_id: string; input: string; result: unknown }[]
}
const props = defineProps<{ sessionId: string }>()
const tasks = ref<Task[]>([])
const detail = ref<Detail | null>(null)
const error = ref("")
const busy = ref(false)
let generation = 0
const base = () => `/api/sessions/${encodeURIComponent(props.sessionId)}/execution-tasks`
async function refresh() {
  const token = generation
  try {
    const value = await requestJson<{ tasks: Task[] }>(base())
    if (token === generation) { tasks.value = value.tasks; error.value = "" }
  } catch { if (token === generation) error.value = "Execution history is unavailable." }
}
async function inspect(id: string) {
  const token = generation
  detail.value = null
  try {
    const value = await requestJson<Detail>(`${base()}/${encodeURIComponent(id)}`)
    if (token === generation) detail.value = value
  } catch { if (token === generation) error.value = "Task detail is unavailable." }
}
async function revoke(id: string) {
  if (busy.value) return
  const token = generation
  busy.value = true
  try {
    await requestJson(`${base()}/${encodeURIComponent(id)}/revoke`, { method: "POST" })
    if (token === generation) { detail.value = null; await refresh() }
  } catch { if (token === generation) error.value = "Revocation failed; execution may still be cleaning up." }
  finally { if (token === generation) busy.value = false }
}
watch(() => props.sessionId, () => {
  generation++; tasks.value = []; detail.value = null; busy.value = false; void refresh()
}, { immediate: true })
onBeforeUnmount(() => { generation++ })
</script>

<template>
  <details class="history" data-testid="execution-history" @toggle="refresh">
    <summary>Private execution tasks · Coding / Plan / Bash</summary>
    <button @click="refresh">Refresh tasks</button>
    <p>Complete Bash input; bounded command output. History never grants permission to replay.</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <ul>
      <li v-for="task in tasks" :key="task.task_id">
        <button @click="inspect(task.task_id)">{{ task.kind }} · {{ task.state }}</button>
        <span>{{ task.commands_used }}/{{ task.max_commands }} commands ·
          {{ Math.ceil(task.charged_ms / 1000) }}/{{ task.max_execution_ms / 1000 }} s</span>
        <span v-if="task.cleanup_pending">Cleanup pending</span>
        <button
          v-if="['pending', 'approved', 'active'].includes(task.state)"
          :disabled="busy" @click="revoke(task.task_id)">Revoke</button>
      </li>
    </ul>
    <section v-if="detail">
      <button @click="detail = null">Close private log</button>
      <article v-for="log in detail.logs" :key="log.command_id">
        <strong>{{ log.command_id }}</strong><pre>{{ log.input }}</pre>
        <pre>{{ log.result == null ? 'No confirmed result (never automatically replayed).' : JSON.stringify(log.result, null, 2) }}</pre>
      </article>
    </section>
  </details>
</template>

<style scoped>
.history { padding: 10px 0; font-size: 12px; }
summary, button { cursor: pointer; } ul { padding: 0; list-style: none; }
li { display: flex; flex-wrap: wrap; gap: 6px; padding: 6px 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 240px; overflow: auto; }
p { color: var(--muted); } [role="alert"] { color: #b91c1c; }
</style>
