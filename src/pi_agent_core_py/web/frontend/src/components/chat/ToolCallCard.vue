<script setup lang="ts">
import { computed } from "vue"

import type { ToolCallItem } from "../../types"
import CardDetails from "./CardDetails.vue"

const props = defineProps<{ item: ToolCallItem }>()

const durationText = computed(() => {
  const ms = props.item.durationMs
  if (!ms) return ""
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
})
</script>

<template>
  <div :class="['tool-card', `status-${item.status}`]">
    <div class="tool-row">
      <span class="tool-icon">
        <span v-if="item.status === 'running'" class="dot-running"></span>
        <span v-else-if="item.status === 'done'">✓</span>
        <span v-else>!</span>
      </span>
      <span class="tool-label">tool</span>
      <code class="tool-name">{{ item.toolName }}</code>
      <span v-if="durationText" class="tool-duration">{{ durationText }}</span>
      <span :class="['tool-status', `status-${item.status}`]">{{ item.status }}</span>
    </div>
    <div v-if="item.argsPreview" class="tool-args">
      <span class="args-label">args:</span>
      <code>{{ item.argsPreview }}</code>
    </div>
    <CardDetails :details="item.details" />
  </div>
</template>

<style scoped>
.tool-card {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--muted);
}
.tool-card.status-error {
  border-color: #fecaca;
  background: #fef2f2;
}
.tool-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.tool-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
}
.status-done .tool-icon {
  color: var(--success);
}
.status-error .tool-icon {
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
.tool-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.tool-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--fg);
  background: white;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
.tool-duration {
  font-size: 10px;
  color: var(--muted);
}
.tool-status {
  margin-left: auto;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--border);
}
.tool-status.status-running {
  background: #fef3c7;
  color: #92400e;
}
.tool-status.status-done {
  background: #dcfce7;
  color: #166534;
}
.tool-status.status-error {
  background: #fee2e2;
  color: #991b1b;
}
.tool-args {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.args-label {
  margin-right: 4px;
}
.tool-args code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}
</style>
