<script setup lang="ts">
import { computed } from "vue"

import { useWikiStore } from "../../stores/wikiStore"

const wikiStore = useWikiStore()
const orderedChangeSets = computed(() => [...wikiStore.changeSets].reverse())

function formatTime(value: number): string {
  return new Date(value).toLocaleString()
}

async function decide(decision: "approve" | "reject"): Promise<void> {
  const changeSet = wikiStore.selectedChangeSet
  if (!changeSet || changeSet.status !== "awaiting_approval") return
  const verb = decision === "approve" ? "publish every operation in" : "reject"
  if (!window.confirm(`Do you want to ${verb} this Change Set?`)) return
  await wikiStore.decideChangeSet(changeSet.id, decision)
}
</script>

<template>
  <section class="changes-view" data-testid="wiki-changes-view">
    <aside class="change-list">
      <header>
        <div>
          <h2>Change Sets</h2>
          <span>One approval publishes the complete diff or nothing.</span>
        </div>
        <button
          type="button"
          :disabled="wikiStore.loadingChanges"
          @click="wikiStore.loadChangeSets()"
        >
          Refresh
        </button>
      </header>
      <div v-if="wikiStore.loadingChanges && !wikiStore.changeSets.length" class="change-empty">
        Loading Change Sets…
      </div>
      <div v-else-if="!wikiStore.changeSets.length" class="change-empty">
        No proposed Wiki changes yet.
      </div>
      <template v-else>
        <button
          v-for="changeSet in orderedChangeSets"
          :key="changeSet.id"
          type="button"
          :class="['change-row', { selected: changeSet.id === wikiStore.selectedChangeSetId }]"
          data-testid="wiki-change-set-row"
          @click="wikiStore.selectChangeSet(changeSet.id)"
        >
          <span :class="['change-status', changeSet.status]">{{ changeSet.status }}</span>
          <strong>{{ changeSet.summary || "Wiki change proposal" }}</strong>
          <small>
            {{ formatTime(changeSet.created_at_ms) }} · base graph r{{
              changeSet.base_graph_revision
            }}
          </small>
        </button>
      </template>
    </aside>

    <main class="diff-panel">
      <div v-if="!wikiStore.selectedChangeSet" class="change-empty tall">
        Select a Change Set to review every operation before approval.
      </div>
      <template v-else>
        <header class="diff-header">
          <div>
            <h2>{{ wikiStore.selectedChangeSet.summary }}</h2>
            <span>
              {{ wikiStore.selectedChangeSet.id }} ·
              {{
                wikiStore.selectedChangeSet.conversation_id ? "Knowledge Agent" : "Source workflow"
              }}
            </span>
          </div>
          <div
            v-if="wikiStore.selectedChangeSet.status === 'awaiting_approval'"
            class="decision-actions"
          >
            <button
              type="button"
              class="reject"
              data-testid="wiki-reject-change-set"
              :disabled="wikiStore.mutating"
              @click="decide('reject')"
            >
              Reject
            </button>
            <button
              type="button"
              class="approve"
              data-testid="wiki-approve-change-set"
              :disabled="wikiStore.mutating"
              @click="decide('approve')"
            >
              Approve all
            </button>
          </div>
          <span v-else :class="['change-status', wikiStore.selectedChangeSet.status]">
            {{ wikiStore.selectedChangeSet.status }}
          </span>
        </header>

        <div
          v-if="wikiStore.loadingChanges && !wikiStore.changeSetItems.length"
          class="change-empty"
        >
          Loading complete diff…
        </div>
        <div v-else class="diff-items">
          <article
            v-for="item in wikiStore.changeSetItems"
            :key="item.id"
            class="diff-item"
            data-testid="wiki-change-set-item"
          >
            <div class="diff-item-header">
              <strong>{{ item.ordinal + 1 }}. {{ item.operation_kind }}</strong>
              <span>{{ item.target_id || "new object" }}</span>
            </div>
            <pre>{{ item.unified_diff }}</pre>
          </article>
        </div>
      </template>
    </main>
  </section>
</template>

<style scoped>
.changes-view {
  display: grid;
  height: 100%;
  min-height: 0;
  grid-template-columns: 330px minmax(0, 1fr);
}
.change-list {
  min-height: 0;
  overflow-y: auto;
  padding: 16px 12px;
  border-right: 1px solid var(--border);
  background: #f8fafc;
}
.change-list > header,
.diff-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.change-list h2,
.diff-header h2 {
  margin: 0;
  font-size: 17px;
}
.change-list header span,
.diff-header span {
  color: var(--muted);
  font-size: 10px;
}
.change-list header button {
  padding: 4px 7px;
  border-color: var(--border);
  background: #fff;
  font-size: 10px;
}
.change-row {
  display: flex;
  width: 100%;
  flex-direction: column;
  gap: 5px;
  align-items: flex-start;
  margin-top: 8px;
  padding: 10px;
  border: 1px solid var(--border);
  background: #fff;
  text-align: left;
}
.change-row:hover,
.change-row.selected {
  border-color: #60a5fa;
}
.change-row strong {
  color: var(--fg);
  font-size: 11px;
  line-height: 1.4;
}
.change-row small {
  color: var(--muted);
  font-size: 9px;
}
.change-status {
  display: inline-flex;
  padding: 3px 7px;
  border-radius: 999px;
  background: #e2e8f0;
  color: #475569 !important;
  font-size: 9px !important;
  font-weight: 600;
}
.change-status.awaiting_approval {
  background: #fef3c7;
  color: #92400e !important;
}
.change-status.approved {
  background: #dcfce7;
  color: #166534 !important;
}
.change-status.rejected,
.change-status.stale,
.change-status.failed {
  background: #fee2e2;
  color: #991b1b !important;
}
.diff-panel {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  background: #fff;
}
.diff-header {
  position: sticky;
  z-index: 2;
  top: 0;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  background: #fff;
}
.diff-header > div:first-child {
  min-width: 0;
}
.diff-header h2 {
  overflow: hidden;
  max-width: 680px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.decision-actions {
  display: flex;
  gap: 7px;
}
.decision-actions .reject {
  border-color: #fecaca;
  background: #fff;
  color: #b91c1c;
}
.decision-actions .approve {
  border-color: #1d4ed8;
  background: #2563eb;
  color: #fff;
}
.diff-items {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
}
.diff-item {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 9px;
}
.diff-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: #f8fafc;
  font-size: 11px;
}
.diff-item-header span {
  overflow: hidden;
  color: var(--muted);
  font-size: 9px;
  text-overflow: ellipsis;
}
.diff-item pre {
  margin: 0;
  padding: 13px;
  overflow-x: auto;
  color: #334155;
  font:
    11px/1.55 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
  white-space: pre;
}
.change-empty {
  padding: 24px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.change-empty.tall {
  display: grid;
  height: 100%;
  place-items: center;
}
@media (max-width: 760px) {
  .changes-view {
    display: block;
    overflow-y: auto;
  }
  .change-list {
    max-height: 340px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
}
</style>
