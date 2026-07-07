<script setup lang="ts">
import { computed } from "vue"

import type { TurnInfoItem } from "../../types"
import CardDetails from "./CardDetails.vue"

const props = defineProps<{ item: TurnInfoItem }>()

const icon = computed(() => {
  const s = props.item.status
  if (s === "done") return "✓"
  if (s === "error") return "!"
  if (s === "queued") return "⋯"
  return "›"
})
</script>

<template>
  <div :class="['turn-card', `status-${item.status || 'running'}`]">
    <div class="turn-row">
      <span class="turn-icon">{{ icon }}</span>
      <span class="turn-title">{{ item.title }}</span>
      <span v-if="item.status" :class="['turn-status', `status-${item.status}`]">
        {{ item.status }}
      </span>
    </div>
    <div class="turn-summary">{{ item.summary }}</div>
    <CardDetails :details="item.details" />
  </div>
</template>

<style scoped>
.turn-card {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--muted);
}
.turn-card.status-error {
  border-color: #fecaca;
  background: #fef2f2;
}
.turn-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.turn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  font-size: 12px;
  font-weight: 600;
}
.status-error .turn-icon {
  color: #b91c1c;
}
.status-done .turn-icon {
  color: var(--success);
}
.turn-title {
  font-weight: 500;
  color: var(--fg);
  flex: 1;
  font-size: 12px;
}
.turn-status {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--border);
  color: var(--muted);
}
.turn-status.status-running {
  background: #fef3c7;
  color: #92400e;
}
.turn-status.status-done {
  background: #dcfce7;
  color: #166534;
}
.turn-status.status-error {
  background: #fee2e2;
  color: #991b1b;
}
.turn-summary {
  margin-top: 4px;
  color: var(--muted);
  font-size: 11px;
}
</style>
