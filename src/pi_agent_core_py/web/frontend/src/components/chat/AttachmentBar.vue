<script setup lang="ts">
import type { FileRef } from "../../types"
import FileChip from "./FileChip.vue"

withDefaults(
  defineProps<{
    files: FileRef[]
    sessionId?: string | null
    removable?: boolean
  }>(),
  {
    sessionId: null,
    removable: true,
  },
)

const emit = defineEmits<{ (e: "remove", fileId: string): void }>()
</script>

<template>
  <div v-if="files.length > 0" class="attachment-bar">
    <FileChip
      v-for="f in files"
      :key="f.id"
      :file="f"
      :session-id="sessionId"
      :removable="removable"
      @remove="emit('remove', $event)"
    />
  </div>
</template>

<style scoped>
.attachment-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 0;
  max-height: 160px;
  overflow-y: auto;
}
</style>
