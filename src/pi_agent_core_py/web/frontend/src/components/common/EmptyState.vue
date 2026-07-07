<script setup lang="ts">
defineProps<{
  title?: string
  hint?: string
}>()
</script>

<template>
  <div class="empty-state">
    <div class="empty-title">{{ title ?? "Start a conversation" }}</div>
    <div v-if="hint" class="empty-hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 48px 24px;
  color: var(--muted);
  text-align: center;
}
.empty-title {
  font-size: 18px;
  font-weight: 500;
  color: var(--fg);
  margin-bottom: 8px;
}
.empty-hint {
  font-size: 13px;
  max-width: 360px;
}
</style>
