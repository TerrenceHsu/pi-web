<script setup lang="ts">
import { ref, watch } from "vue"

import { requestJson } from "../../api/client"

type Backend = "disabled" | "e2b" | "local_docker"
interface Capability {
  backend: Backend
  revision: number
  backends: { id: Backend; label: string; available: boolean }[]
}
const props = defineProps<{ sessionId: string }>()
const emit = defineEmits<{ changed: [] }>()
const state = ref<Capability | null>(null)
const busy = ref(false)
const error = ref("")
let generation = 0
const path = (id: string) => `/api/workspaces/${encodeURIComponent(id)}/execution`

watch(() => props.sessionId, async (id) => {
  const token = ++generation
  state.value = null
  error.value = ""
  busy.value = true
  try {
    const value = await requestJson<Capability>(path(id))
    if (token === generation) state.value = value
  } catch {
    if (token === generation) error.value = "Execution capability is unavailable."
  } finally {
    if (token === generation) busy.value = false
  }
}, { immediate: true })

async function select(event: Event) {
  if (busy.value || !state.value) return
  const backend = (event.target as HTMLSelectElement).value
  const token = generation
  busy.value = true
  error.value = ""
  try {
    const value = await requestJson<Capability>(path(props.sessionId), {
      method: "PUT", body: { backend, expected_revision: state.value.revision },
    })
    if (token === generation) {
      state.value = value
      emit("changed")
    }
  } catch {
    if (token === generation) error.value = "Selection changed or backend is unavailable. Reload before retrying."
  } finally {
    if (token === generation) {
      busy.value = false
      if (state.value) (event.target as HTMLSelectElement).value = state.value.backend
    }
  }
}
</script>

<template>
  <section class="execution" data-testid="workspace-execution">
    <h3>Coding / Plan execution</h3>
    <select
      v-if="state?.backends.length" :value="state.backend" :disabled="busy"
      aria-label="Coding execution backend" data-testid="execution-backend" @change="select">
      <option
        v-for="backend in state.backends" :key="backend.id" :value="backend.id"
        :disabled="!backend.available">{{ backend.label }}</option>
    </select>
    <p v-else>Execution is not configured.</p>
    <p>Selection does not authorize execution. Each request needs approval before creating a copy.
      Changing this selection revokes current execution. Publishing needs separate confirmation.
      Python Analysis permission is independent. For standalone Bash, choose Local Docker,
      select the optional Bash tool, then ask “run_bash …” in normal chat with Code/Plan off.
      Each script needs confirmation. Copy changes are discarded; output may reach your model.</p>
    <p>With the Bash tool selected, Local Docker Coding/Plan Executor also gets run_bash.
      It shares the approved task's copy and budget with coding_run, with no extra script prompt.
      Planner/Verifier cannot execute it. Task files still require validation and freeze;
      Docker publication is not available yet.</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.execution { padding: 10px 0; border-bottom: 1px solid var(--border); }
h3 { font-size: 13px; }
p { font-size: 11px; color: var(--muted); line-height: 1.5; }
select { max-width: 100%; padding: 6px; font-size: 12px; }
[role="alert"] { color: #b91c1c; }
</style>
