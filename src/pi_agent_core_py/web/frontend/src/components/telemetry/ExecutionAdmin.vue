<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue"
import { requestJson } from "../../api/client"
import type { TelemetrySpanListResponse, TelemetrySpanSummary } from "../../types/telemetry"

interface Task {
  task_id: string; account_id: string; session_id: string; kind: string; backend: string
  runtime_id: string; expires_ms: number; stop_requested: boolean; error_code: string | null
  released: boolean; phase: string; commands_used: number; max_commands: number
  charged_ms: number; max_execution_ms: number
}
interface Status {
  quota: { account_tasks: number; global_tasks: number; cpu: number; memory_mb: number }
  active_tasks: number; cpu: number; memory_mb: number; cleanup_pending: number
  admission_paused: boolean; last_cleanup_ms: number | null; error_code: string | null
  cache: { bytes: number; removed_files: number; error_code?: string } | null; tasks: Task[]
}
const state = ref<Status | null>(null)
const error = ref("")
const probe = ref("")
const events = ref<TelemetrySpanSummary[]>([])
const busy = ref(false)
let timer: number | undefined
let disposed = false
const base = "/api/admin/local-execution"
async function refresh() {
  try {
    const value = await requestJson<Status>(`${base}/status`)
    if (!disposed) { state.value = value; error.value = "" }
    const spans = await requestJson<TelemetrySpanListResponse>("/api/admin/telemetry/spans", {
      query: { name: "execution.command", limit: 20, window_hours: 24 },
    })
    if (!disposed) events.value = spans.spans
  } catch { if (!disposed) error.value = "Execution administration is unavailable." }
}
async function act(path: string) {
  if (busy.value) return
  busy.value = true
  try {
    const result = await requestJson<Record<string, unknown>>(`${base}/${path}`, { method: "POST" })
    if (path === "probe") probe.value = result.environment_ready
      ? "Daemon and image ready. This read-only probe does not authorize execution or certify isolation."
      : `Not ready: ${String(result.error_code ?? "unavailable")}`
    await refresh()
  } catch { error.value = "Action failed. Cleanup is not confirmed; inspect status before retrying." }
  finally { busy.value = false }
}
onMounted(() => { void refresh(); timer = window.setInterval(() => void refresh(), 5000) })
onBeforeUnmount(() => { disposed = true; window.clearInterval(timer) })
</script>

<template>
  <section class="execution-admin" data-testid="execution-admin" aria-label="Execution administration">
    <h2>Local execution</h2>
    <p>Shared Coding / Plan / Bash quotas. Revocation stops execution; it never publishes files.</p>
    <div class="actions">
      <button :disabled="busy" @click="refresh">Refresh execution</button>
      <button :disabled="busy" @click="act('probe')">Probe daemon / image</button>
      <button :disabled="busy" @click="act('cleanup')">Retry safe cleanup</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="probe" role="status">{{ probe }}</p>
    <template v-if="state">
      <p>Tasks {{ state.active_tasks }} / {{ state.quota.global_tasks }} ·
        Per account {{ state.quota.account_tasks }} · CPU {{ state.cpu }} / {{ state.quota.cpu }} ·
        Memory {{ state.memory_mb }} / {{ state.quota.memory_mb }} MiB</p>
      <p v-if="state.admission_paused" role="alert">New execution paused: {{ state.cleanup_pending }} cleanup obligations remain.</p>
      <p v-if="state.error_code || state.cache?.error_code" role="alert">{{ state.error_code || state.cache?.error_code }}</p>
      <p v-if="state.cache">This account's cache: {{ Math.ceil(state.cache.bytes / 1048576) }} MiB.
        Last sweep removed {{ state.cache.removed_files }} expired files; pending review is protected.</p>
      <p v-if="!state.tasks.length">No execution tasks recorded.</p>
      <ul v-else>
        <li v-for="task in state.tasks" :key="task.task_id">
          <span>{{ task.kind }} · {{ task.backend }} · {{ task.account_id }} / {{ task.session_id }}</span>
          <code>{{ task.task_id }}</code>
          <span>{{ task.error_code || (task.released ? `${task.phase} · released` : task.stop_requested ? 'Stop requested' : 'Reserved / executing') }}</span>
          <span>{{ task.commands_used }}/{{ task.max_commands }} commands · {{ Math.ceil(task.charged_ms / 1000) }}/{{ task.max_execution_ms / 1000 }} s</span>
          <button v-if="!task.released" :disabled="busy || task.stop_requested" @click="act(`tasks/${encodeURIComponent(task.task_id)}/revoke`)">Revoke execution</button>
        </li>
      </ul>
    </template>
    <p>Scripts, filenames and output are not shown here. Private command history stays in the owner's Workspace.</p>
    <details>
      <summary>Recent execution Telemetry · last 24 hours</summary>
      <ul>
        <li v-for="event in events" :key="event.id">
          {{ event.attributes.kind }} / {{ event.attributes.command_kind }} ·
          {{ event.attributes.phase }} · exit {{ event.attributes.exit_code ?? 'unknown' }} ·
          {{ event.attributes.duration_ms }} ms · stdout {{ event.attributes.stdout_bytes ?? 0 }} B ·
          stderr {{ event.attributes.stderr_bytes ?? 0 }} B
          <code>{{ event.attributes.task_id }} / {{ event.attributes.command_id }}</code>
        </li>
      </ul>
    </details>
  </section>
</template>

<style scoped>
.execution-admin { padding: 16px; border: 1px solid var(--border); border-radius: 10px; }
h2 { font-size: 15px; margin: 0 0 8px; } p { font-size: 12px; color: var(--muted); }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
button { padding: 6px 10px; cursor: pointer; } button:disabled { cursor: default; opacity: .5; }
ul { padding: 0; list-style: none; } li { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px 0; }
code { overflow-wrap: anywhere; } [role="alert"] { color: #b91c1c; }
</style>
