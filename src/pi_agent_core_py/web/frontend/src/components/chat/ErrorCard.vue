<script setup lang="ts">
import type { ErrorItem } from "../../types"
import CardDetails from "./CardDetails.vue"

defineProps<{ item: ErrorItem }>()
</script>

<template>
  <div class="error-card">
    <div class="error-row">
      <span class="error-icon">!</span>
      <span class="error-label">error</span>
    </div>
    <div class="error-message">{{ item.message }}</div>
    <CardDetails :details="item.details" />
  </div>
</template>

<style scoped>
.error-card {
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: #b91c1c;
}
.error-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.error-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  background: #dc2626;
  color: white;
  border-radius: 50%;
  font-size: 10px;
  font-weight: bold;
}
.error-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.error-message {
  margin-top: 4px;
  font-size: 12px;
  word-break: break-word;
}
</style>
