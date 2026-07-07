<script setup lang="ts">
import { computed } from "vue"

import { downloadFileUrl } from "../../api/files"
import type { FileRef } from "../../types"
import { fileSupportLabel, formatFileSize, refFormat } from "../../utils/files"

const props = withDefaults(
  defineProps<{
    file: FileRef
    sessionId?: string | null
    removable?: boolean
    downloadable?: boolean
  }>(),
  {
    removable: true,
    downloadable: true,
    sessionId: null,
  },
)

const emit = defineEmits<{ (e: "remove", fileId: string): void }>()

const fmt = computed(() => refFormat(props.file))
const sizeText = computed(() => formatFileSize(props.file.size))
const label = computed(() => fileSupportLabel(fmt.value))
const labelClass = computed(() => {
  if (label.value === "supported") return "label-supported"
  if (label.value === "not parsed") return "label-pdf"
  return "label-unsupported"
})
const downloadHref = computed(() => {
  if (!props.sessionId || !props.downloadable) return ""
  return downloadFileUrl(props.sessionId, props.file.id)
})

function onRemove() {
  emit("remove", props.file.id)
}
</script>

<template>
  <div :class="['file-chip', labelClass]" data-testid="file-chip">
    <span class="chip-icon">📄</span>
    <div class="chip-body">
      <div class="chip-name" :title="file.name">{{ file.name }}</div>
      <div class="chip-meta">
        <span v-if="sizeText">{{ sizeText }}</span>
        <span v-if="fmt" class="chip-format">{{ fmt }}</span>
        <span :class="['chip-label', labelClass]">{{ label }}</span>
      </div>
    </div>
    <div class="chip-actions">
      <a
        v-if="downloadHref"
        :href="downloadHref"
        :download="file.name"
        class="chip-btn"
        title="Download"
        aria-label="Download"
      >↓</a>
      <button
        v-if="removable"
        class="chip-btn"
        title="Remove"
        aria-label="Remove"
        @click="onRemove"
      >×</button>
    </div>
  </div>
</template>

<style scoped>
.file-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: white;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  max-width: 280px;
  min-width: 0;
}
.file-chip:hover {
  border-color: var(--muted);
}
.chip-icon {
  font-size: 14px;
  flex-shrink: 0;
}
.chip-body {
  flex: 1;
  min-width: 0;
}
.chip-name {
  color: var(--fg);
  font-weight: 500;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  font-size: 10px;
  color: var(--muted);
}
.chip-format {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--code-bg);
  padding: 1px 5px;
  border-radius: 8px;
}
.chip-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 5px;
  border-radius: 8px;
}
.label-supported {
  background: #dcfce7;
  color: #166534;
}
.label-pdf {
  background: #fef3c7;
  color: #92400e;
}
.label-unsupported {
  background: var(--border);
  color: var(--muted);
}
.chip-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
.chip-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  background: transparent;
  border: none;
  color: var(--muted);
  cursor: pointer;
  border-radius: 4px;
  font-size: 13px;
  text-decoration: none;
  line-height: 1;
}
.chip-btn:hover {
  background: var(--border);
  color: var(--fg);
}
</style>
