<script setup lang="ts">
import { ref } from "vue"

import { useKnowledgeStore } from "../../stores/knowledgeStore"
import ErrorBanner from "../common/ErrorBanner.vue"
import EmptyState from "../common/EmptyState.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"

const knowledgeStore = useKnowledgeStore()

const queryInput = ref("")
const limitInput = ref(10)

async function onSearch() {
  const q = queryInput.value.trim()
  if (!q) {
    knowledgeStore.searchError = "Enter a search query first."
    return
  }
  await knowledgeStore.runSearch(q, limitInput.value)
}

function onClear() {
  queryInput.value = ""
  knowledgeStore.clearSearch()
}

function onPageRange(hit: { page_start: number; page_end: number }): string {
  if (hit.page_start === hit.page_end) return `p.${hit.page_start}`
  return `pp.${hit.page_start}–${hit.page_end}`
}
</script>

<template>
  <div class="search-panel" data-testid="search-panel">
    <div class="search-header">
      <span class="column-title">Search this library</span>
    </div>

    <div class="search-row">
      <input
        v-model="queryInput"
        class="search-input"
        placeholder="Search indexed content (BM25)..."
        maxlength="512"
        data-testid="search-query-input"
        @keyup.enter="onSearch"
      />
      <select
        v-model.number="limitInput"
        class="search-limit"
        data-testid="search-limit-select"
      >
        <option :value="5">5</option>
        <option :value="10">10</option>
        <option :value="20">20</option>
        <option :value="50">50</option>
      </select>
      <button
        class="search-btn"
        data-testid="search-run-btn"
        :disabled="knowledgeStore.searching || !queryInput.trim()"
        @click="onSearch"
      >
        {{ knowledgeStore.searching ? "Searching…" : "Search" }}
      </button>
      <button
        v-if="knowledgeStore.searchResults.length > 0 || knowledgeStore.searchQuery"
        class="clear-btn"
        data-testid="search-clear-btn"
        @click="onClear"
      >
        Clear
      </button>
    </div>

    <ErrorBanner
      v-if="knowledgeStore.searchError"
      :message="knowledgeStore.searchError"
      dismissible
      @dismiss="knowledgeStore.searchError = null"
    />

    <div class="search-results" data-testid="search-results">
      <div v-if="knowledgeStore.searching" class="loading-row">
        <LoadingSpinner :size="12" />
        <span>Searching…</span>
      </div>

      <div
        v-else-if="knowledgeStore.searchQuery && knowledgeStore.searchResults.length === 0"
        class="empty-results"
      >
        <EmptyState title="No results">
          <template #default>
            <div class="empty-hint">No matches for "{{ knowledgeStore.searchQuery }}".</div>
          </template>
        </EmptyState>
      </div>

      <div
        v-for="(hit, idx) in knowledgeStore.searchResults"
        v-else
        :key="hit.chunk_id"
        class="search-hit"
        :data-testid="`search-hit-${idx}`"
      >
        <div class="hit-header">
          <span class="hit-source">{{ hit.source_name || "(unknown)" }}</span>
          <span class="hit-pages">{{ onPageRange(hit) }}</span>
        </div>
        <div v-if="hit.heading_path.length > 0" class="hit-heading">
          {{ hit.heading_path.join(" > ") }}
        </div>
        <div class="hit-content">{{ hit.content }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.search-header {
  display: flex;
  align-items: center;
}
.column-title {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}
.search-row {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.search-input {
  flex: 1;
  min-width: 200px;
  padding: 6px 8px;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--fg);
}
.search-input:focus {
  outline: none;
  border-color: var(--accent, #4a9eff);
}
.search-limit {
  padding: 6px 4px;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--fg);
}
.search-btn {
  padding: 6px 14px;
  font-size: 13px;
  background: var(--accent, #4a9eff);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.search-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.clear-btn {
  padding: 6px 10px;
  font-size: 13px;
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
}
.loading-row,
.empty-results {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px 0;
  color: var(--muted);
  font-size: 12px;
}
.empty-hint {
  font-size: 13px;
  max-width: 360px;
}
.search-results {
  max-height: 240px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.search-hit {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  font-size: 12px;
}
.hit-header {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 2px;
}
.hit-source {
  font-weight: 500;
  color: var(--fg);
}
.hit-pages {
  color: var(--muted);
  font-size: 11px;
}
.hit-heading {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 4px;
}
.hit-content {
  color: var(--fg);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.4;
  max-height: 80px;
  overflow-y: hidden;
}
</style>
