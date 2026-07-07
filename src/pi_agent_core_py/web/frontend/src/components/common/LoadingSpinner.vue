<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 16 })
</script>

<template>
  <span
    class="loading-spinner"
    :style="{ width: `${size}px`, height: `${size}px` }"
    aria-label="Loading"
  ></span>
</template>

<style scoped>
.loading-spinner {
  display: inline-block;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: middle;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
