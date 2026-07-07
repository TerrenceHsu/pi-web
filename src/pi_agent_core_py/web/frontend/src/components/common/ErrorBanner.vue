<script setup lang="ts">
defineProps<{
  message: string
  /** 可选关闭按钮 */
  dismissible?: boolean
}>()

const emit = defineEmits<{ (e: "dismiss"): void }>()
</script>

<template>
  <div class="error-banner" role="alert">
    <span class="error-icon">!</span>
    <span class="error-message">{{ message }}</span>
    <button
      v-if="dismissible"
      class="error-dismiss"
      aria-label="Dismiss"
      @click="emit('dismiss')"
    >×</button>
  </div>
</template>

<style scoped>
.error-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
  border-radius: 6px;
  font-size: 13px;
}
.error-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  background: #dc2626;
  color: white;
  border-radius: 50%;
  font-size: 11px;
  font-weight: bold;
  flex-shrink: 0;
}
.error-message {
  flex: 1;
  word-break: break-word;
}
.error-dismiss {
  background: none;
  border: none;
  color: #b91c1c;
  font-size: 18px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
.error-dismiss:hover {
  color: #7f1d1d;
}
</style>
