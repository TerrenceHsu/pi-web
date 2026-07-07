<script setup lang="ts">
import type { SkillUsedItem } from "../../types"

defineProps<{ item: SkillUsedItem }>()
</script>

<template>
  <div class="skill-card">
    <div class="skill-row">
      <span class="skill-icon">★</span>
      <span class="skill-label">Skills used</span>
      <span class="skill-count">{{ item.skillNames.length }}</span>
    </div>
    <div class="skill-names">
      <code v-for="name in item.skillNames" :key="name" class="skill-name">{{ name }}</code>
    </div>
    <div v-if="item.summary" class="skill-summary">{{ item.summary }}</div>
  </div>
</template>

<style scoped>
.skill-card {
  background: #fefce8;
  border: 1px solid #fef3c7;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--muted);
}
.skill-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.skill-icon {
  font-size: 11px;
  color: var(--warn);
}
.skill-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #92400e;
}
.skill-count {
  margin-left: auto;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #fef3c7;
  color: #92400e;
}
.skill-names {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.skill-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--fg);
  background: white;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
.skill-summary {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
</style>
