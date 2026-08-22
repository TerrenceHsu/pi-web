<script setup lang="ts">
import type { ToolResultItem } from "../../types"
import CardDetails from "./CardDetails.vue"

defineProps<{ item: ToolResultItem }>()
</script>

<template>
  <div
    :class="['result-card', `status-${item.status}`]"
    data-testid="tool-result-card"
    :data-tool-call-id="item.toolCallId ?? ''"
    :data-tool-name="item.toolName"
  >
    <div class="result-row">
      <span class="result-icon">{{ item.status === 'error' ? '!' : '↳' }}</span>
      <span class="result-label">result</span>
      <code class="result-tool">{{ item.toolName }}</code>
      <span :class="['result-status', `status-${item.status}`]">{{ item.status }}</span>
    </div>
    <div v-if="item.resultPreview" class="result-preview">
      <code>{{ item.resultPreview }}</code>
    </div>
    <CardDetails :details="item.details" />
  </div>
</template>

<style scoped>
.result-card {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--muted);
  margin-left: 12px;
}
.result-card.status-error {
  border-color: #fecaca;
  background: #fef2f2;
}
.result-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.result-icon {
  display: inline-flex;
  width: 14px;
  font-size: 11px;
  color: var(--muted);
}
.status-error .result-icon {
  color: #b91c1c;
}
.result-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.result-tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--fg);
  background: white;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
.result-status {
  margin-left: auto;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 8px;
}
.result-status.status-done {
  background: #dcfce7;
  color: #166534;
}
.result-status.status-error {
  background: #fee2e2;
  color: #991b1b;
}
.result-preview {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.result-preview code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}
</style>
