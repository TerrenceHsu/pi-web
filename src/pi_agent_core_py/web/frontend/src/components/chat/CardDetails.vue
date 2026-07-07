<script setup lang="ts">
/**
 * CardDetails —— inline card 共用的折叠 details 块。
 *
 * - 默认折叠
 * - JSON.stringify 限制 max-height + overflow
 * - 不默认展开 raw JSON
 */
defineProps<{
  details?: unknown
  label?: string
}>()
</script>

<template>
  <details v-if="details !== undefined" class="card-details">
    <summary>{{ label ?? "Details" }}</summary>
    <pre class="card-details-json">{{ JSON.stringify(details, null, 2) }}</pre>
  </details>
</template>

<style scoped>
.card-details {
  margin-top: 6px;
  font-size: 11px;
  color: var(--muted);
}
.card-details summary {
  cursor: pointer;
  user-select: none;
  padding: 2px 0;
}
.card-details summary:hover {
  color: var(--fg);
}
.card-details-json {
  margin: 4px 0 0;
  padding: 6px 8px;
  background: white;
  border: 1px solid var(--border);
  border-radius: 3px;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fg);
}
</style>
