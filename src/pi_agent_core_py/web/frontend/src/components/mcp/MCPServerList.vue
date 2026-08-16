<script setup lang="ts">
import { computed } from "vue"

import { useMcpStore } from "../../stores/mcpStore"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import DDGSSettingsForm from "./DDGSSettingsForm.vue"

const mcpStore = useMcpStore()

const servers = computed(() => mcpStore.servers)
const testingName = computed(() => mcpStore.testingServerName)

function lastTest(name: string) {
  return mcpStore.lastTestResultByServer[name]
}

async function onTest(name: string) {
  try {
    await mcpStore.testServer(name)
  } catch {
    // store 已写 lastTestResult / error
  }
}

async function onEnable(name: string) {
  try {
    await mcpStore.enableServer(name)
  } catch {
    // store 已写 error
  }
}

async function onDisable(name: string) {
  try {
    await mcpStore.disableServer(name)
  } catch {
    // store 已写 error
  }
}

async function onDelete(name: string) {
  if (
    !window.confirm(
      `Delete MCP server "${name}"? This will detach and clean up disabled tool settings.`,
    )
  ) {
    return
  }
  try {
    await mcpStore.deleteServer(name)
  } catch {
    // store 已写 error
  }
}
</script>

<template>
  <div class="mcp-server-list">
    <h3 class="block-title">Servers</h3>
    <p v-if="servers.length === 0" class="empty">No MCP servers added yet.</p>
    <div
      v-for="s in servers"
      :key="s.name"
      class="server-card"
      :data-testid="'mcp-server-card'"
      :data-server-name="s.name"
    >
      <div class="server-head">
        <span class="server-name">{{ s.name }}</span>
        <span v-if="s.builtin" class="badge badge-builtin">built-in · fixed</span>
        <span
          class="badge"
          :class="s.enabled ? 'badge-on' : 'badge-off'"
          data-testid="mcp-server-status-badge"
        >{{ s.enabled ? "enabled" : "disabled" }}</span>
        <span
          v-if="s.enabled && !s.last_error"
          class="badge badge-success"
          data-testid="mcp-server-attached-badge"
        >attached · {{ s.tool_count }} tool{{ s.tool_count === 1 ? "" : "s" }}</span>
        <span
          v-else-if="s.enabled && s.last_error"
          class="badge badge-error"
        >attach error</span>
        <span v-else class="badge badge-muted">off</span>
      </div>
      <div v-if="!s.builtin" class="server-cmd">
        <code>{{ s.command }}</code>
        <span v-if="s.args && s.args.length" class="server-args">
          {{ s.args.join(" ") }}
        </span>
      </div>
      <div v-if="s.env_keys && s.env_keys.length" class="server-env">
        env keys: <code>{{ s.env_keys.join(", ") }}</code>
        <span class="muted">(values hidden)</span>
      </div>
      <p v-if="s.last_error" class="server-error">{{ s.last_error }}</p>
      <DDGSSettingsForm v-if="s.builtin && s.name === 'ddgs'" :server="s" />
      <div v-if="lastTest(s.name)" class="server-test" data-testid="mcp-server-test-result">
        <span
          v-if="lastTest(s.name)?.error == null && lastTest(s.name)?.toolCount != null"
          class="test-ok"
        >
          ✓ last test: {{ lastTest(s.name)?.toolCount }} tool{{ (lastTest(s.name)?.toolCount ?? 0) === 1 ? "" : "s" }} detected
        </span>
        <span v-else class="server-error">
          ✗ last test failed: {{ lastTest(s.name)?.error }}
        </span>
      </div>
      <div class="server-actions">
        <button
          type="button"
          :disabled="testingName === s.name"
          data-testid="mcp-server-test-btn"
          @click="onTest(s.name)"
        >
          <LoadingSpinner v-if="testingName === s.name" :size="12" />
          <span v-else>Test</span>
        </button>
        <button
          v-if="!s.enabled"
          type="button"
          class="primary"
          data-testid="mcp-server-enable-btn"
          @click="onEnable(s.name)"
        >Enable</button>
        <button
          v-else
          type="button"
          data-testid="mcp-server-disable-btn"
          @click="onDisable(s.name)"
        >Disable</button>
        <button
          v-if="s.deletable !== false"
          type="button"
          class="danger"
          data-testid="mcp-server-delete-btn"
          @click="onDelete(s.name)"
        >Delete</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mcp-server-list {
  margin-bottom: 14px;
}
.block-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg);
}
.empty {
  padding: 12px;
  color: var(--muted);
  text-align: center;
  font-style: italic;
  font-size: 12px;
}
.server-card {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: white;
  margin-bottom: 8px;
}
.server-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.server-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--fg);
  word-break: break-all;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 500;
}
.badge-on {
  background: var(--success);
  color: white;
}
.badge-off {
  background: var(--code-bg);
  color: var(--muted);
}
.badge-success {
  background: #dcfce7;
  color: #166534;
}
.badge-error {
  background: #fee2e2;
  color: #991b1b;
}
.badge-muted {
  background: var(--code-bg);
  color: var(--muted);
}
.badge-builtin {
  background: #dbeafe;
  color: #1d4ed8;
}
.server-cmd {
  font-size: 12px;
  color: var(--fg);
  word-break: break-all;
  margin-bottom: 4px;
}
.server-cmd code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.server-args {
  color: var(--muted);
  margin-left: 4px;
}
.server-env {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 4px;
}
.server-env .muted {
  margin-left: 4px;
  font-style: italic;
}
.server-error {
  margin: 4px 0;
  padding: 6px 8px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 4px;
  color: #b91c1c;
  font-size: 11px;
  word-break: break-word;
}
.server-test {
  font-size: 11px;
  margin-bottom: 4px;
}
.test-ok {
  color: var(--success);
}
.server-actions {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.server-actions button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 12px;
}
</style>
