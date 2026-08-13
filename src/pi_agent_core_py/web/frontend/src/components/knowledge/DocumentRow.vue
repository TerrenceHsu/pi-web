<script setup lang="ts">
import {
  categorizeDocumentStatus,
  isRetryableDocumentStatus,
  type Document,
} from "../../types/knowledge"
import { fetchDocumentMarkdown } from "../../api/knowledge"
import { useKnowledgeStore } from "../../stores/knowledgeStore"

const props = defineProps<{ doc: Document }>()
const knowledgeStore = useKnowledgeStore()

async function onRetry() {
  try {
    await knowledgeStore.retryDocument(props.doc.id)
  } catch {
    // surfaced via store.error
  }
}

async function onDelete() {
  if (!window.confirm(`Delete "${props.doc.source_name}"?`)) return
  try {
    await knowledgeStore.deleteDocument(props.doc.id)
  } catch {
    // surfaced via store.error (e.g. 409 ingestion_active)
  }
}

async function onViewMarkdown() {
  // P1-F owns the formal Markdown Workspace Panel; R5 ships a minimal
  // implementation that fetches raw MD and opens a new window with the text.
  try {
    const { blob } = await fetchDocumentMarkdown(props.doc.id)
    // Open raw MD in a new tab; browser renders as plain text.
    const url = URL.createObjectURL(blob)
    window.open(url, "_blank", "noopener,noreferrer")
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  } catch (e: any) {
    knowledgeStore.error = e?.detail ?? String(e?.message ?? e)
  }
}
</script>

<template>
  <div class="document-row" :data-testid="`document-row-${doc.id}`">
    <div class="doc-main">
      <div class="doc-name">{{ doc.source_name }}</div>
      <div class="doc-meta">
        <span :class="['status-pill', categorizeDocumentStatus(doc.status)]">
          {{ doc.status }}
        </span>
        <span v-if="doc.page_count > 0" class="doc-pages">
          {{ doc.page_count }}p
        </span>
        <span class="doc-size">{{ formatSize(doc.size_bytes) }}</span>
      </div>
    </div>
    <div class="doc-actions">
      <button
        v-if="isRetryableDocumentStatus(doc.status)"
        class="action-btn"
        data-testid="doc-retry-btn"
        title="Retry ingestion"
        @click="onRetry"
      >
        ↻ Retry
      </button>
      <button
        v-if="doc.status === 'ready' || doc.status === 'normalizing' || doc.status === 'chunking' || doc.status === 'indexing'"
        class="action-btn"
        data-testid="doc-view-md-btn"
        title="View Markdown"
        @click="onViewMarkdown"
      >
        MD
      </button>
      <button
        class="action-btn danger"
        data-testid="doc-delete-btn"
        title="Delete"
        @click="onDelete"
      >
        ×
      </button>
    </div>
  </div>
</template>

<script lang="ts">
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

export default {}
</script>

<style scoped>
.document-row {
  display: flex;
  align-items: center;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  gap: 8px;
  background: var(--bg);
}
.doc-main {
  flex: 1;
  min-width: 0;
}
.doc-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.doc-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--muted);
}
.status-pill {
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.status-pill.processing,
.status-pill.indexing {
  background: rgba(74, 158, 255, 0.15);
  color: #1976d2;
}
.status-pill.ready {
  background: rgba(76, 175, 80, 0.15);
  color: #2e7d32;
}
.status-pill.failed {
  background: rgba(244, 67, 54, 0.15);
  color: #c62828;
}
.status-pill.needs_ocr {
  background: rgba(255, 152, 0, 0.15);
  color: #ef6c00;
}
.status-pill.deleting {
  background: rgba(158, 158, 158, 0.15);
  color: #616161;
}
.status-pill.unknown {
  background: rgba(158, 158, 158, 0.15);
  color: #616161;
}
.doc-pages,
.doc-size {
  white-space: nowrap;
}
.doc-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.action-btn {
  padding: 3px 8px;
  font-size: 11px;
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 3px;
  cursor: pointer;
}
.action-btn:hover {
  background: var(--hover-bg, rgba(0, 0, 0, 0.04));
  color: var(--fg);
}
.action-btn.danger:hover {
  color: #f44336;
  border-color: #f44336;
}
</style>
