<script setup lang="ts">
import { ref, watch } from "vue"
import { requestJson } from "../../api/client"

const props = defineProps<{ sessionId: string }>()
interface Run { run_id: string; status: string; cwd: string; created_at_ms: number }
const runs = ref<Run[]>([])
const detail = ref<string | null>(null)
const error = ref("")
let generation = 0
const path = () => `/api/workspaces/${encodeURIComponent(props.sessionId)}/bash-runs`
async function refresh() {
  const token = ++generation
  runs.value = []
  detail.value = null
  error.value = ""
  try {
    const result = await requestJson<{ runs: Run[] }>(path())
    if (token === generation) runs.value = result.runs
  } catch {
    if (token === generation) error.value = "Bash history is unavailable."
  }
}
async function open(run: Run) {
  const token = ++generation
  detail.value = null
  error.value = ""
  try {
    const result = await requestJson<Record<string, unknown>>(`${path()}/${encodeURIComponent(run.run_id)}`)
    if (token === generation) detail.value = JSON.stringify(result, null, 2)
  } catch {
    if (token === generation) error.value = "Run is unavailable or has expired."
  }
}
watch(() => props.sessionId, refresh, { immediate: true })
</script>

<template>
  <section data-testid="bash-history" class="bash-history">
    <h3>Standalone Bash runs <button type="button" @click="refresh">Refresh runs</button></h3>
    <p>Private script/output history: up to 200 runs per account, 30 days (pruned on new runs).
      Refresh only reads history; it never executes a script. Frozen successful output is available
      in Changes for separate publication approval. Integrity does not mean functional validation.</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!runs.length">No retained Bash runs.</p>
    <ul>
      <li v-for="run in runs" :key="run.run_id">
        <button type="button" @click="open(run)">{{ run.status }} · {{ run.cwd }} · {{ new Date(run.created_at_ms).toLocaleString() }}</button>
      </li>
    </ul>
    <pre v-if="detail !== null" data-testid="bash-run-detail">{{ detail }}</pre>
  </section>
</template>

<style scoped>
.bash-history { padding: 10px 0; border-bottom: 1px solid var(--border); }
h3 { font-size: 13px; }
p, button { font-size: 11px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 320px; overflow: auto; font-size: 11px; }
</style>
