<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue"
import * as api from "../../api/dataAnalysis"
import { useFileStore } from "../../stores/fileStore"
import { useSessionStore } from "../../stores/sessionStore"
import { useWorkspaceExtensionStore } from "../../stores/workspaceExtensionStore"
import AnalysisResult from "./AnalysisResult.vue"

const fileStore = useFileStore()
const sessions = useSessionStore()
const extensions = useWorkspaceExtensionStore()
const sid = computed(() => sessions.activeSessionId)
const enabled = computed(() => extensions.snapshot?.session_id === sid.value
  && extensions.snapshot?.tools?.some(t => t.name === "analyze_data" && t.available && t.selected))
const files = computed(() => (fileStore.filesBySession[sid.value ?? ""] ?? [])
  .filter(f => /\.(csv|tsv|xlsx|parquet)$/i.test(f.name)))
const fileId = ref("")
const action = ref("inspect")
const sheet = ref("")
const group = ref("")
const metric = ref("")
const operation = ref("sum")
const dateColumn = ref("")
const chartType = ref("bar")
const x = ref("")
const y = ref("")
const limit = ref(100)
const encoding = ref("utf-8-sig")
const headerRow = ref(0)
const dateFormat = ref("ISO8601")
const busy = ref(false)
const error = ref("")
const runs = ref<api.AnalysisRun[]>([])
const selected = ref<api.AnalysisRun | null>(null)
let generation = 0
let viewVersion = 0
let timer: ReturnType<typeof setTimeout> | undefined
const running = computed(() => selected.value && ["queued", "awaiting_approval", "running"].includes(selected.value.status))

async function refresh(session: string, token: number) {
  try {
    const response = await api.listAnalysis(session)
    if (generation !== token) return
    runs.value = response.runs
  } catch (e) { if (generation === token) error.value = String(e) }
}

async function view(runId: string, session = sid.value, token = generation) {
  if (!session) return
  const version = ++viewVersion
  clearTimeout(timer)
  try {
    const next = await api.getAnalysis(session, runId)
    if (generation !== token || session !== sid.value || version !== viewVersion) return
    selected.value = next
    if (["queued", "awaiting_approval", "running"].includes(next.status)) {
      timer = setTimeout(() => void view(runId, session, token), 800)
    } else await refresh(session, token)
  } catch (e) { if (generation === token && version === viewVersion) error.value = String(e) }
}

async function start() {
  const session = sid.value, token = generation
  if (!session || !enabled.value || busy.value) return
  busy.value = true
  error.value = ""
  const request: Record<string, unknown> = {
    file_id: fileId.value, action: action.value, encoding: encoding.value,
    header_row: headerRow.value, limit: limit.value,
  }
  if (sheet.value) request.sheet = sheet.value
  if (action.value === "aggregate" || action.value === "timeseries") {
    request.group_by = group.value ? [group.value] : []
    request.metrics = [{ column: metric.value, operation: operation.value }]
  }
  if (action.value === "timeseries") {
    request.date_column = dateColumn.value
    request.date_format = dateFormat.value
    request.frequency = "month"
  }
  if (action.value === "chart") Object.assign(request, { chart_type: chartType.value, x: x.value, y: y.value || null })
  try {
    const run = await api.startAnalysis(session, request)
    if (generation === token) await view(run.id, session, token)
  } catch (e) { if (generation === token) error.value = String(e) }
  finally { if (generation === token) busy.value = false }
}

async function runAction(run: api.AnalysisRun, operation: "cancel" | "retry" | "delete") {
  const session = sid.value, token = generation
  if (!session || busy.value) return
  if (operation === "delete" && !window.confirm("Delete this analysis history and preview? Saved Workspace files remain.")) return
  busy.value = true
  error.value = ""
  try {
    if (operation === "delete") {
      viewVersion++
      clearTimeout(timer)
      await api.deleteAnalysis(session, run.id)
      if (generation === token && selected.value?.id === run.id) selected.value = null
    } else {
      const next = await api.analysisAction(session, run.id, operation)
      if (generation === token) await view(next.id, session, token)
    }
    await refresh(session, token)
  } catch (e) { if (generation === token) error.value = String(e) }
  finally { if (generation === token) busy.value = false }
}

watch(sid, async session => {
  const token = ++generation
  clearTimeout(timer)
  runs.value = []; selected.value = null; error.value = ""; busy.value = false
  fileId.value = ""; sheet.value = ""
  group.value = ""; metric.value = ""; dateColumn.value = ""; x.value = ""; y.value = ""
  if (!session) return
  try {
    await Promise.all([extensions.load(session), fileStore.loadFiles(session), refresh(session, token)])
    if (generation === token) {
      fileId.value = files.value[0]?.id ?? ""
      if (runs.value[0]) await view(runs.value[0].id, session, token)
    }
  } catch (e) { if (generation === token) error.value = String(e) }
}, { immediate: true })
onBeforeUnmount(() => { generation++; clearTimeout(timer) })
</script>

<template>
  <div class="analysis-panel" data-testid="data-analysis-panel">
    <strong>Data Analysis</strong>
    <p v-if="!enabled">Enable Data Analysis under Workspace → extensions → Tools first.</p>
    <p>This form runs fixed local calculations. For custom Python, enable Python Data Analysis in Tools and ask in chat. Each execution requires your approval.</p>
    <form @submit.prevent="start">
      <label>Source<select v-model="fileId" data-testid="analysis-source" required>
        <option value="">Choose a table</option>
        <option v-for="file in files" :key="file.id" :value="file.id">{{ file.logical_path }}</option>
      </select></label>
      <label>Action<select v-model="action" data-testid="analysis-action">
        <option v-for="kind in ['inspect', 'profile', 'aggregate', 'timeseries', 'chart']" :key="kind">{{ kind }}</option>
      </select></label>
      <label>Sheet (XLSX only)<input v-model="sheet" placeholder="Default: first sheet" /></label>
      <details><summary>Input and result options</summary>
        <label>CSV encoding<select v-model="encoding"><option>utf-8-sig</option><option>utf-8</option><option>gb18030</option></select></label>
        <label>Header row (zero-based)<input v-model.number="headerRow" type="number" min="0" max="100" /></label>
        <label>Result row limit<input v-model.number="limit" type="number" min="1" max="5000" /></label>
      </details>
      <template v-if="action === 'aggregate' || action === 'timeseries'">
        <label>Group by (optional)<input v-model="group" placeholder="Exact column name" /></label>
        <label>Value column<input v-model="metric" required data-testid="analysis-metric" /></label>
        <label>Metric<select v-model="operation"><option v-for="op in ['sum', 'count', 'mean', 'min', 'max', 'median']" :key="op">{{ op }}</option></select></label>
      </template>
      <template v-if="action === 'timeseries'">
        <label>Date column<input v-model="dateColumn" required /></label>
        <label>Date format<input v-model="dateFormat" required /></label>
        <p>Monthly buckets in UTC. Use chat for other time periods.</p>
      </template>
      <template v-if="action === 'chart'">
        <label>Chart<select v-model="chartType"><option v-for="kind in ['bar', 'line', 'histogram', 'scatter']" :key="kind">{{ kind }}</option></select></label>
        <label>X column<input v-model="x" required data-testid="analysis-x" /></label>
        <label v-if="chartType !== 'histogram'">Y column<input v-model="y" required data-testid="analysis-y" /></label>
        <p>Bar/line: sum Y by X. Chart results show at most 50 rows, with any limitation reported.</p>
      </template>
      <button type="submit" :disabled="!enabled || busy || !!running || !fileId" data-testid="run-analysis">Analyze</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-if="selected" class="selected-run">
      <p>{{ selected.status }} <span v-if="selected.error_code">· {{ selected.error_code }}</span></p>
      <p v-if="selected.status === 'awaiting_approval'">Review the code in the chat approval card. No code has run yet.</p>
      <button v-if="running" type="button" :disabled="busy" @click="runAction(selected, 'cancel')">Cancel analysis</button>
      <AnalysisResult v-if="selected.result" :result="selected.result" />
    </div>
    <details open><summary>History · up to 20 runs per Workspace</summary>
      <p>Deleting history removes previews only, not exported Workspace files.</p>
      <div v-for="run in runs" :key="run.id" class="history-row">
        <button type="button" @click="view(run.id)">{{ new Date(run.created_at * 1000).toLocaleString() }} · {{ run.status }}</button>
        <span v-if="run.action === 'python' && ['failed', 'cancelled', 'interrupted'].includes(run.status)">Retry in chat with a new approval.</span>
        <button v-else-if="['failed', 'cancelled', 'interrupted'].includes(run.status)" :disabled="busy || !enabled" type="button" @click="runAction(run, 'retry')">Retry</button>
        <button :disabled="busy" type="button" @click="runAction(run, 'delete')">Delete</button>
      </div>
    </details>
  </div>
</template>

<style scoped>
.analysis-panel { padding: 12px; overflow: auto; font-size: 12px; }
form { display: grid; gap: 8px; margin: 12px 0; }
label { display: grid; gap: 4px; }
select, input { min-width: 0; width: 100%; box-sizing: border-box; }
.history-row { display: flex; gap: 4px; margin: 6px 0; flex-wrap: wrap; }
details { margin: 10px 0; }
[role="alert"] { color: var(--danger); }
</style>
