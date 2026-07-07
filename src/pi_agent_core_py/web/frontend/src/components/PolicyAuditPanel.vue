<script setup lang="ts">
import { ref, watch } from "vue"
import * as api from "../api"
import type { PolicyAuditResponse } from "../types"

const props = defineProps<{ refreshTick: number }>()

const data = ref<PolicyAuditResponse | null>(null)
const limit = ref(100)
const error = ref<string | null>(null)

async function load() {
  try {
    data.value = await api.getPolicyAudit(limit.value)
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

watch(
  () => props.refreshTick,
  () => load(),
  { immediate: true },
)

watch(limit, () => load())

function decisionClass(d: string): string {
  if (d === "allow") return "idle"
  if (d === "deny") return "error"
  return "running"
}
</script>

<template>
  <div>
    <div v-if="error" class="error-text">{{ error }}</div>
    <div v-if="!data" class="empty-state">loading...</div>
    <div v-else style="padding: 12px;">
      <div class="row" style="margin-bottom: 8px;">
        <label>limit:</label>
        <input v-model="limit" type="text" style="width: 80px;" />
        <button @click="load">Refresh</button>
        <div class="grow"></div>
        <strong>policy:</strong>
        <code>{{ data.policy_name ?? "(none)" }}</code>
      </div>

      <table v-if="data.records.length" class="data">
        <thead>
          <tr>
            <th>timestamp</th>
            <th>tool_call_id</th>
            <th>tool_name</th>
            <th>decision</th>
            <th>reason</th>
            <th>metadata</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in data.records" :key="i">
            <td>{{ new Date(r.timestamp).toLocaleString() }}</td>
            <td><code>{{ r.tool_call_id }}</code></td>
            <td><code>{{ r.tool_name }}</code></td>
            <td>
              <span :class="['status-pill', decisionClass(r.decision)]">
                {{ r.decision }}
              </span>
            </td>
            <td>{{ r.reason || "—" }}</td>
            <td>
              <details>
                <summary>show</summary>
                <pre>{{ JSON.stringify(r.metadata, null, 2) }}</pre>
              </details>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">no audit records</div>
    </div>
  </div>
</template>
