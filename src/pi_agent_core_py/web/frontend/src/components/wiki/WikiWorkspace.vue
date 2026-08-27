<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue"

import { useWikiStore } from "../../stores/wikiStore"
import type { WikiWorkspaceTab } from "../../types/wiki"
import WikiChangesView from "./WikiChangesView.vue"
import WikiConversationsView from "./WikiConversationsView.vue"
import WikiGraphView from "./WikiGraphView.vue"
import WikiPagesView from "./WikiPagesView.vue"
import WikiSourcesView from "./WikiSourcesView.vue"

const emit = defineEmits<{ (event: "close"): void }>()
const wikiStore = useWikiStore()
const newSpaceName = ref("")

const tabs: { id: WikiWorkspaceTab; label: string }[] = [
  { id: "sources", label: "Sources" },
  { id: "pages", label: "Pages" },
  { id: "graph", label: "Graph" },
  { id: "changes", label: "Changes" },
  { id: "conversations", label: "Conversations" },
]

async function createSpace(): Promise<void> {
  const created = await wikiStore.createSpace(newSpaceName.value)
  if (created) newSpaceName.value = ""
}

async function deleteSpace(spaceId: string, name: string): Promise<void> {
  if (
    window.confirm(
      `Delete Wiki Space “${name}”? All Sources must already be deleting, pages deleted, ` +
        "conversations archived, and pending Changes resolved.",
    )
  ) {
    await wikiStore.deleteSpace(spaceId)
  }
}

onMounted(() => void wikiStore.loadSpaces())
onBeforeUnmount(() => wikiStore.clearPreview())
</script>

<template>
  <div class="wiki-workspace" data-testid="wiki-workspace">
    <header class="wiki-header">
      <div class="wiki-title">
        <span class="wiki-mark" aria-hidden="true">W</span>
        <div>
          <strong>LLM Wiki</strong>
          <span>Approved pages, source evidence and Knowledge Agent conversations</span>
        </div>
      </div>
      <button type="button" data-testid="wiki-back-to-chat" @click="emit('close')">
        Back to chat
      </button>
    </header>

    <div class="wiki-body">
      <aside class="space-sidebar">
        <div class="space-heading">
          <span>Wiki Spaces</span>
          <button
            type="button"
            class="icon-button"
            aria-label="Refresh Wiki Spaces"
            :disabled="wikiStore.loadingSpaces"
            @click="wikiStore.loadSpaces()"
          >
            ↻
          </button>
        </div>
        <form class="new-space" @submit.prevent="createSpace">
          <input
            v-model="newSpaceName"
            data-testid="wiki-new-space-name"
            maxlength="120"
            placeholder="New Wiki Space"
            :disabled="wikiStore.mutating"
          />
          <button
            type="submit"
            data-testid="wiki-create-space"
            :disabled="!newSpaceName.trim() || wikiStore.mutating"
          >
            +
          </button>
        </form>
        <div v-if="wikiStore.loadingSpaces && wikiStore.spaces.length === 0" class="space-empty">
          Loading…
        </div>
        <div v-else-if="wikiStore.spaces.length === 0" class="space-empty">
          Create a Space before uploading sources.
        </div>
        <div
          v-for="space in wikiStore.spaces"
          :key="space.id"
          class="space-row-wrap"
        >
          <button
            type="button"
            :class="['space-row', { selected: space.id === wikiStore.selectedSpaceId }]"
            :disabled="space.status !== 'active'"
            data-testid="wiki-space-row"
            @click="wikiStore.selectSpace(space.id)"
          >
            <span>{{ space.name }}</span>
            <small>{{ space.status }} · graph r{{ space.graph_revision }}</small>
          </button>
          <div v-if="space.status === 'archived'" class="space-row-actions">
            <button
              type="button"
              :disabled="wikiStore.mutating"
              @click="wikiStore.setSpaceStatus(space.id, 'active')"
            >
              Restore
            </button>
            <button
              type="button"
              class="danger-text"
              :disabled="wikiStore.mutating"
              @click="deleteSpace(space.id, space.name)"
            >
              Delete
            </button>
          </div>
        </div>
      </aside>

      <main class="wiki-main">
        <div v-if="wikiStore.error" class="wiki-error" role="alert">
          <span>{{ wikiStore.error }}</span>
          <button type="button" aria-label="Dismiss Wiki error" @click="wikiStore.error = null">
            ×
          </button>
        </div>
        <div v-if="!wikiStore.selectedSpace" class="no-space">
          <strong>No active Wiki Space selected</strong>
          <span>Create or select a Space to begin.</span>
        </div>
        <template v-else>
          <div class="space-bar">
            <div>
              <strong>{{ wikiStore.selectedSpace.name }}</strong>
              <span>{{ wikiStore.selectedSpace.description || "No description" }}</span>
            </div>
            <div class="space-lifecycle-actions">
              <button
                type="button"
                :disabled="wikiStore.mutating"
                @click="wikiStore.setSpaceStatus(wikiStore.selectedSpace.id, 'archived')"
              >
                Archive
              </button>
              <button
                type="button"
                class="danger-text"
                :disabled="wikiStore.mutating"
                @click="deleteSpace(wikiStore.selectedSpace.id, wikiStore.selectedSpace.name)"
              >
                Delete
              </button>
            </div>
            <nav aria-label="Wiki sections">
              <button
                v-for="tab in tabs"
                :key="tab.id"
                type="button"
                :class="{ active: wikiStore.activeTab === tab.id }"
                :data-testid="`wiki-tab-${tab.id}`"
                @click="wikiStore.setActiveTab(tab.id)"
              >
                {{ tab.label }}
              </button>
            </nav>
          </div>
          <div class="wiki-content">
            <WikiSourcesView v-if="wikiStore.activeTab === 'sources'" />
            <WikiPagesView v-else-if="wikiStore.activeTab === 'pages'" />
            <WikiGraphView v-else-if="wikiStore.activeTab === 'graph'" />
            <WikiChangesView v-else-if="wikiStore.activeTab === 'changes'" />
            <WikiConversationsView v-else />
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.wiki-workspace {
  display: flex;
  width: 100vw;
  height: 100vh;
  flex-direction: column;
  overflow: hidden;
  background: #f8fafc;
}
.wiki-header {
  display: flex;
  min-height: 62px;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: #fff;
}
.wiki-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 11px;
}
.wiki-mark {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 9px;
  background: #1d4ed8;
  color: #fff;
  font-weight: 700;
}
.wiki-title div {
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.wiki-title strong {
  color: var(--fg);
  font-size: 15px;
}
.wiki-title span {
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wiki-body {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: 230px minmax(0, 1fr);
}
.space-sidebar {
  display: flex;
  min-height: 0;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
  padding: 14px 10px;
  border-right: 1px solid var(--border);
  background: #f1f5f9;
}
.space-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 5px;
  color: #64748b;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.icon-button {
  padding: 3px 6px;
  border: 0;
  background: transparent;
}
.new-space {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 34px;
  gap: 5px;
  margin-bottom: 6px;
}
.new-space input {
  min-width: 0;
  padding: 7px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  font-size: 12px;
}
.space-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  align-items: flex-start;
  padding: 9px 10px;
  border: 1px solid transparent;
  background: transparent;
  text-align: left;
}
.space-row-wrap {
  display: grid;
  gap: 4px;
}
.space-row-wrap > .space-row {
  width: 100%;
}
.space-row-actions,
.space-lifecycle-actions {
  display: flex;
  flex-direction: row;
  gap: 5px;
}
.space-row-actions {
  justify-content: flex-end;
  padding: 0 4px 5px;
}
.space-row-actions button,
.space-lifecycle-actions button {
  padding: 4px 7px;
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: #fff;
  font-size: 10px;
}
.space-row-actions .danger-text,
.space-lifecycle-actions .danger-text {
  border-color: #fca5a5;
  color: #b91c1c;
}
.space-row:hover:not(:disabled),
.space-row.selected {
  border-color: #bfdbfe;
  background: #fff;
}
.space-row span {
  overflow: hidden;
  width: 100%;
  color: var(--fg);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.space-row small,
.space-empty {
  color: var(--muted);
  font-size: 10px;
}
.space-empty {
  padding: 16px 8px;
  line-height: 1.5;
}
.wiki-main {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
}
.wiki-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  background: #fee2e2;
  color: #991b1b;
  font-size: 12px;
}
.wiki-error button {
  border: 0;
  background: transparent;
  color: inherit;
}
.space-bar {
  display: flex;
  min-height: 64px;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
  padding: 10px 18px 0;
  border-bottom: 1px solid var(--border);
  background: #fff;
}
.space-bar > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding-bottom: 10px;
}
.space-bar > .space-lifecycle-actions {
  min-width: auto;
  flex-direction: row;
  align-items: center;
  align-self: center;
  margin-left: auto;
  padding-bottom: 0;
}
.space-bar strong {
  color: var(--fg);
  font-size: 15px;
}
.space-bar span {
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.space-bar nav {
  display: flex;
  align-self: stretch;
  gap: 3px;
}
.space-bar nav button {
  padding: 0 10px;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 12px;
}
.space-bar nav button.active {
  border-bottom-color: #2563eb;
  color: #1d4ed8;
}
.wiki-content {
  min-height: 0;
  flex: 1;
  overflow: hidden;
}
.no-space {
  display: grid;
  height: 100%;
  place-content: center;
  gap: 6px;
  color: var(--muted);
  text-align: center;
}
.no-space strong {
  color: var(--fg);
}
@media (max-width: 800px) {
  .wiki-body {
    grid-template-columns: 170px minmax(0, 1fr);
  }
  .space-bar {
    display: block;
    overflow-x: auto;
  }
  .space-bar nav {
    min-height: 38px;
  }
}
@media (max-width: 560px) {
  .wiki-title span {
    display: none;
  }
  .wiki-body {
    display: block;
    overflow: auto;
  }
  .space-sidebar {
    max-height: 210px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .wiki-main {
    min-height: 600px;
  }
}
</style>
