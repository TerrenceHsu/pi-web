<script setup lang="ts">
import { ref, watch } from "vue"
import * as api from "../api"
import type { McpResponse } from "../types"

const props = defineProps<{ refreshTick: number }>()

const mcp = ref<McpResponse | null>(null)
const error = ref<string | null>(null)

async function load() {
  try {
    mcp.value = await api.getMcp()
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
    <div v-if="!mcp" class="empty-state">loading...</div>
    <div v-else-if="!mcp.attached" class="empty-state">no MCP registry attached</div>
    <div v-else style="padding: 12px;">
      <h2>Servers</h2>
      <table v-if="mcp.servers.length" class="data">
        <thead>
          <tr>
            <th>name</th>
            <th>connected</th>
            <th>tools</th>
            <th>last_error</th>
            <th>metadata</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in mcp.servers" :key="s.name">
            <td>{{ s.name }}</td>
            <td>
              <span :class="['status-pill', s.connected ? 'idle' : 'error']">
                {{ s.connected ? "yes" : "no" }}
              </span>
            </td>
            <td>{{ s.tool_count }}</td>
            <td>
              <span v-if="s.last_error" class="error-text">{{ s.last_error }}</span>
              <span v-else style="color: var(--muted);">—</span>
            </td>
            <td>
              <details>
                <summary>show</summary>
                <pre>{{ JSON.stringify(s.metadata, null, 2) }}</pre>
              </details>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">no servers configured</div>

      <h2>Tools</h2>
      <table v-if="mcp.tools.length" class="data">
        <thead>
          <tr>
            <th>name</th>
            <th>server</th>
            <th>mcp_tool</th>
            <th>description</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in mcp.tools" :key="t.name">
            <td><code>{{ t.name }}</code></td>
            <td>{{ t.server }}</td>
            <td>{{ t.mcp_tool }}</td>
            <td>{{ t.description }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">no tools registered</div>

      <h2>Prompts</h2>
      <table v-if="mcp.prompts.length" class="data">
        <thead>
          <tr>
            <th>server</th>
            <th>name</th>
            <th>description</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(p, i) in mcp.prompts" :key="`${p.server}-${p.name}-${i}`">
            <td>{{ p.server }}</td>
            <td><code>{{ p.name }}</code></td>
            <td>{{ p.description }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">no prompts exposed</div>
    </div>
  </div>
</template>
