<script setup lang="ts">
import type { MCPToolCallItem } from "../../types"
import CardDetails from "./CardDetails.vue"

defineProps<{ item: MCPToolCallItem }>()
</script>

<template>
  <div
    :class="['mcp-card', `status-${item.status}`]"
    data-testid="mcp-tool-card"
    :data-tool-call-id="item.toolCallId ?? ''"
    :data-server-name="item.serverName ?? ''"
    :data-tool-name="item.toolName"
  >
    <div class="mcp-row">
      <span class="mcp-icon">
        <span v-if="item.status === 'running'" class="dot-running"></span>
        <span v-else-if="item.status === 'done'">✓</span>
        <span v-else>!</span>
      </span>
      <span class="mcp-label">mcp</span>
      <span v-if="item.serverName" class="mcp-server">{{ item.serverName }}</span>
      <span class="mcp-sep">/</span>
      <code class="mcp-tool">{{ item.toolName }}</code>
      <span :class="['mcp-status', `status-${item.status}`]">{{ item.status }}</span>
    </div>
    <div v-if="item.argsPreview" class="mcp-args">
      <span class="args-label">args:</span>
      <code>{{ item.argsPreview }}</code>
    </div>
    <div v-if="item.resultPreview" class="mcp-result">
      <span class="args-label">result:</span>
      <code>{{ item.resultPreview }}</code>
    </div>
    <CardDetails :details="item.details" />
  </div>
</template>

<style scoped>
.mcp-card {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--muted);
}
.mcp-card.status-error {
  border-color: #fecaca;
  background: #fef2f2;
}
.mcp-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.mcp-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
}
.status-done .mcp-icon {
  color: var(--success);
}
.status-error .mcp-icon {
  color: #b91c1c;
}
.dot-running {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--muted);
  animation: pulse 1.2s infinite ease-in-out;
}
@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}
.mcp-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  background: #dbeafe;
  color: #1e40af;
  padding: 1px 6px;
  border-radius: 8px;
}
.mcp-server {
  font-size: 11px;
  color: var(--fg);
  font-weight: 500;
}
.mcp-sep {
  color: var(--muted);
}
.mcp-tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--fg);
  background: white;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
.mcp-status {
  margin-left: auto;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--border);
}
.mcp-status.status-running {
  background: #fef3c7;
  color: #92400e;
}
.mcp-status.status-done {
  background: #dcfce7;
  color: #166534;
}
.mcp-status.status-error {
  background: #fee2e2;
  color: #991b1b;
}
.mcp-args,
.mcp-result {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.args-label {
  margin-right: 4px;
}
.mcp-args code,
.mcp-result code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}
</style>
