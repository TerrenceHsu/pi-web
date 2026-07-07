<script setup lang="ts">
import { computed } from "vue"

import type { FileReadItem } from "../../types"
import CardDetails from "./CardDetails.vue"

const props = defineProps<{ item: FileReadItem }>()

const isImage = computed(() => props.item.format === "image_unsupported")
const isPdf = computed(() => props.item.format === "pdf")
const unsupportedNote = computed(() => {
  if (isImage.value) return "图片内容暂不支持分析（不做 OCR / 视觉理解）"
  if (isPdf.value) return "PDF 正文暂未解析（仅元信息）"
  return ""
})
</script>

<template>
  <div :class="['file-card', `status-${item.status}`]">
    <div class="file-row">
      <span class="file-icon">📄</span>
      <span class="file-label">{{ item.toolName }}</span>
      <code v-if="item.fileName" class="file-name">{{ item.fileName }}</code>
      <span v-if="item.format" class="file-format">{{ item.format }}</span>
      <span :class="['file-status', `status-${item.status}`]">{{ item.status }}</span>
    </div>
    <div v-if="unsupportedNote" class="file-unsupported">{{ unsupportedNote }}</div>
    <div v-else-if="item.preview" class="file-preview">
      <code>{{ item.preview }}</code>
    </div>
    <CardDetails :details="item.details" />
  </div>
</template>

<style scoped>
.file-card {
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--muted);
}
.file-card.status-error {
  border-color: #fecaca;
  background: #fef2f2;
}
.file-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.file-icon {
  font-size: 12px;
}
.file-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.file-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--fg);
  background: white;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--border);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-format {
  font-size: 10px;
  color: var(--muted);
  background: var(--border);
  padding: 1px 6px;
  border-radius: 8px;
}
.file-status {
  margin-left: auto;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--border);
}
.file-status.status-running {
  background: #fef3c7;
  color: #92400e;
}
.file-status.status-done {
  background: #dcfce7;
  color: #166534;
}
.file-status.status-error {
  background: #fee2e2;
  color: #991b1b;
}
.file-unsupported {
  margin-top: 4px;
  font-size: 11px;
  color: var(--warn);
}
.file-preview {
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.file-preview code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}
</style>
