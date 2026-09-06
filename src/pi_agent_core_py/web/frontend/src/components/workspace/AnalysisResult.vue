<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { analysisRunUrl, saveAnalysis, type AnalysisResult } from "../../api/dataAnalysis"
import { useFileStore } from "../../stores/fileStore"
import { useSessionStore } from "../../stores/sessionStore"

const props = defineProps<{ result: AnalysisResult }>()
const fileStore = useFileStore()
const sessionStore = useSessionStore()
const saving = ref(false)
const saved = ref(false)
const error = ref("")
let saveVersion = 0
const current = computed(() => props.result.session_id === sessionStore.activeSessionId)
const chartUrl = computed(() => `${analysisRunUrl(props.result.session_id, props.result.run_id)}/chart`)

watch(() => props.result.run_id, () => {
  saveVersion++; saving.value = false; saved.value = false; error.value = ""
})

async function save() {
  if (!current.value || saving.value) return
  const { session_id: sid, run_id: runId } = props.result
  const version = ++saveVersion
  saving.value = true
  error.value = ""
  try {
    await saveAnalysis(sid, runId)
    if (props.result.run_id !== runId || sessionStore.activeSessionId !== sid) return
    saved.value = true
    await fileStore.loadFiles(sid)
  } catch (e) {
    if (props.result.run_id === runId && sessionStore.activeSessionId === sid) error.value = String(e)
  } finally {
    if (version === saveVersion) saving.value = false
  }
}
</script>

<template>
  <section class="analysis-result" data-testid="analysis-result">
    <strong>{{ result.action }} · {{ result.source_name }}</strong>
    <p class="muted">{{ result.source_logical_path }} · {{ result.source.sheet || "table" }}</p>
    <details v-if="result.code"><summary>Executed Python code</summary><pre>{{ result.code }}</pre></details>
    <pre v-if="result.stdout" data-testid="analysis-stdout">{{ result.stdout }}</pre>
    <pre v-if="result.python_error" role="alert">{{ result.python_error }}</pre>
    <p>
      Source {{ result.source_rows }} rows · Analyzed {{ result.analyzed_rows }} ·
      Result {{ result.result_rows }} · Export {{ result.exported_rows }} · Preview {{ result.preview_rows }}
    </p>
    <p v-if="result.limited || result.preview_limited" class="warning">
      {{ result.limited ? "Result/export is limited." : "Table preview is limited; export includes more rows." }}
    </p>
    <ul v-if="result.warnings.length"><li v-for="warning in result.warnings" :key="warning">{{ warning }}</li></ul>
    <p v-if="result.source.sheets?.length">Sheets: {{ result.source.sheets.join(", ") }}</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th v-for="column in result.columns" :key="column">{{ column }}</th></tr></thead>
        <tbody><tr v-for="(row, index) in result.rows" :key="index">
          <td v-for="(value, cell) in row" :key="cell">{{ value === null ? "—" : String(value).slice(0, 300) }}</td>
        </tr></tbody>
      </table>
    </div>
    <img v-if="result.has_chart && current" :src="chartUrl" alt="Computed data analysis chart" />
    <details><summary>Calculation policy and source</summary>
      <p>{{ result.null_policy }}</p><p>{{ result.numeric_policy }}</p>
      <code>SHA-256: {{ result.source_sha256 }}</code>
    </details>
    <button v-if="!result.python_error" type="button" :disabled="saving || saved || !current" data-testid="save-analysis" @click="save">
      {{ saving ? "Saving…" : saved ? "Saved to Workspace" : "Save results to Workspace" }}
    </button>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.analysis-result { padding: 10px; font-size: 12px; min-width: 0; border: 1px solid var(--border); border-radius: 7px; }
p { margin: 7px 0; overflow-wrap: anywhere; }
.table-scroll { overflow: auto; max-height: 320px; margin: 10px 0; }
table { border-collapse: collapse; width: 100%; }
td, th { padding: 6px; border: 1px solid var(--border); text-align: left; white-space: nowrap; }
img { width: 100%; height: auto; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 300px; overflow: auto; }
details { margin: 10px 0; overflow-wrap: anywhere; }
.warning, [role="alert"] { color: var(--danger); }
</style>
