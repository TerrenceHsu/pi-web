<script setup lang="ts">
import { ref } from "vue"

import { useKnowledgeStore } from "../../stores/knowledgeStore"
import EmptyState from "../common/EmptyState.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"

const knowledgeStore = useKnowledgeStore()

const newName = ref("")
const creating = ref(false)

async function onCreate() {
  const name = newName.value.trim()
  if (!name) return
  creating.value = true
  try {
    await knowledgeStore.createLibrary(name)
    newName.value = ""
  } catch {
    // error surfaced via store.error
  } finally {
    creating.value = false
  }
}

async function onDelete(libraryId: string, name: string) {
  if (!window.confirm(`Delete library "${name}"? All documents will be lost.`)) return
  try {
    await knowledgeStore.deleteLibrary(libraryId)
  } catch {
    // surfaced via store.error (e.g. 409 ingestion_active)
  }
}

async function onRename(libraryId: string, currentName: string) {
  const name = window.prompt("Rename library", currentName)
  if (!name || !name.trim() || name === currentName) return
  try {
    await knowledgeStore.renameLibrary(libraryId, name.trim())
  } catch {
    // surfaced via store.error
  }
}
</script>

<template>
  <div class="library-list" data-testid="library-list">
    <div class="library-list-header">
      <span class="column-title">Libraries</span>
      <span v-if="knowledgeStore.loadingLibraries" class="loading-hint">
        <LoadingSpinner :size="11" />
      </span>
    </div>

    <div class="create-row">
      <input
        v-model="newName"
        class="name-input"
        placeholder="New library name"
        maxlength="200"
        data-testid="library-name-input"
        @keyup.enter="onCreate"
      />
      <button
        class="create-btn"
        data-testid="library-create-btn"
        :disabled="creating || !newName.trim()"
        @click="onCreate"
      >
        +
      </button>
    </div>

    <div v-if="!knowledgeStore.loadingLibraries && knowledgeStore.libraries.length === 0" class="empty">
      <EmptyState title="No libraries" hint="Create your first library above." />
    </div>

    <div v-else class="library-items">
      <div
        v-for="lib in knowledgeStore.libraries"
        :key="lib.id"
        :class="['library-item', { selected: lib.id === knowledgeStore.selectedLibraryId }]"
        data-testid="library-item"
        @click="knowledgeStore.selectLibrary(lib.id)"
      >
        <div class="library-item-main">
          <div class="library-item-name">{{ lib.name }}</div>
          <div class="library-item-meta">
            <span :class="['status-dot', lib.status]"></span>
            <span>{{ lib.document_count }} doc{{ lib.document_count === 1 ? "" : "s" }}</span>
          </div>
        </div>
        <div class="library-item-actions" @click.stop>
          <button
            class="icon-btn"
            data-testid="library-rename-btn"
            title="Rename"
            aria-label="Rename"
            @click="onRename(lib.id, lib.name)"
          >
            ✎
          </button>
          <button
            class="icon-btn danger"
            data-testid="library-delete-btn"
            title="Delete"
            aria-label="Delete"
            @click="onDelete(lib.id, lib.name)"
          >
            ×
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.library-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.library-list-header {
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
.create-row {
  display: flex;
  gap: 4px;
}
.name-input {
  flex: 1;
  padding: 6px 8px;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--fg);
  min-width: 0;
}
.name-input:focus {
  outline: none;
  border-color: var(--accent, #4a9eff);
}
.create-btn {
  padding: 6px 10px;
  font-size: 14px;
  background: var(--accent, #4a9eff);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.create-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.empty {
  padding: 12px 0;
}
.library-items {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.library-item {
  display: flex;
  align-items: center;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  min-width: 0;
}
.library-item:hover {
  background: var(--hover-bg, rgba(0, 0, 0, 0.04));
}
.library-item.selected {
  background: var(--selected-bg, rgba(74, 158, 255, 0.12));
}
.library-item-main {
  flex: 1;
  min-width: 0;
}
.library-item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.library-item-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--muted);
}
.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
  background: var(--muted);
}
.status-dot.active {
  background: #4caf50;
}
.status-dot.archived {
  background: #9e9e9e;
}
.status-dot.deleting {
  background: #ff9800;
}
.status-dot.failed {
  background: #f44336;
}
.library-item-actions {
  display: none;
  gap: 2px;
}
.library-item:hover .library-item-actions,
.library-item.selected .library-item-actions {
  display: flex;
}
.icon-btn {
  padding: 2px 6px;
  font-size: 12px;
  background: transparent;
  color: var(--muted);
  border: none;
  border-radius: 3px;
  cursor: pointer;
}
.icon-btn:hover {
  background: var(--hover-bg, rgba(0, 0, 0, 0.06));
  color: var(--fg);
}
.icon-btn.danger:hover {
  color: #f44336;
}
</style>
