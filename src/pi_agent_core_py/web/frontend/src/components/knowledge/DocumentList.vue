<script setup lang="ts">
import { useKnowledgeStore } from "../../stores/knowledgeStore"
import EmptyState from "../common/EmptyState.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import DocumentRow from "./DocumentRow.vue"

const knowledgeStore = useKnowledgeStore()
</script>

<template>
  <div class="document-list" data-testid="document-list">
    <div class="document-list-header">
      <span class="column-title">
        Documents ({{ knowledgeStore.documents.length }})
      </span>
      <span v-if="knowledgeStore.loadingDocuments" class="loading-hint">
        <LoadingSpinner :size="11" />
      </span>
    </div>

    <div
      v-if="!knowledgeStore.loadingDocuments && knowledgeStore.documents.length === 0"
      class="empty"
    >
      <EmptyState
        title="No documents"
        hint="Upload a PDF above to start ingestion."
      />
    </div>

    <div v-else class="document-items">
      <DocumentRow
        v-for="doc in knowledgeStore.documents"
        :key="doc.id"
        :doc="doc"
      />
    </div>
  </div>
</template>

<style scoped>
.document-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 120px;
}
.document-list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.column-title {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}
.loading-hint {
  display: inline-flex;
}
.empty {
  padding: 24px 0;
}
.document-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 280px;
  overflow-y: auto;
}
</style>
