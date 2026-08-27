<script setup lang="ts">
import { computed, ref, watch } from "vue"

import { useWikiStore } from "../../stores/wikiStore"
import { renderMarkdown } from "../../utils/markdown"

const wikiStore = useWikiStore()
const searchQuery = ref("")
const viewedRevisionId = ref<string | null>(null)

watch(
  () => [wikiStore.selectedPageId, wikiStore.pageRevisions] as const,
  () => {
    viewedRevisionId.value = wikiStore.selectedPage?.current_revision_id ?? null
  },
  { deep: true },
)

const viewedRevision = computed(
  () =>
    wikiStore.pageRevisions.find((revision) => revision.id === viewedRevisionId.value) ??
    wikiStore.selectedPageRevision,
)
const renderedPage = computed(() => renderMarkdown(viewedRevision.value?.markdown ?? ""))

async function runSearch(): Promise<void> {
  await wikiStore.searchPages(searchQuery.value)
}

async function openPage(pageId: string): Promise<void> {
  await wikiStore.selectPage(pageId)
  viewedRevisionId.value = wikiStore.selectedPage?.current_revision_id ?? null
}
</script>

<template>
  <section class="pages-view" data-testid="wiki-pages-view">
    <aside class="page-index">
      <div class="page-index-header">
        <div>
          <h2>Pages</h2>
          <span>Only active, approved current revisions</span>
        </div>
        <button type="button" :disabled="wikiStore.loadingPages" @click="wikiStore.loadPages()">
          Refresh
        </button>
      </div>
      <form class="page-search" role="search" @submit.prevent="runSearch">
        <input
          v-model="searchQuery"
          data-testid="wiki-page-search-input"
          maxlength="200"
          placeholder="Search approved pages"
        />
        <button
          type="submit"
          data-testid="wiki-page-search-button"
          :disabled="!searchQuery.trim() || wikiStore.searchingPages"
        >
          {{ wikiStore.searchingPages ? "…" : "Search" }}
        </button>
      </form>

      <div v-if="wikiStore.pageSearchResults.length" class="search-results">
        <div class="subheading">Search results</div>
        <button
          v-for="hit in wikiStore.pageSearchResults"
          :key="hit.page_id"
          type="button"
          class="search-hit"
          data-testid="wiki-page-search-result"
          @click="openPage(hit.page_id)"
        >
          <strong>{{ hit.title }}</strong>
          <span>{{ hit.snippet }}</span>
        </button>
      </div>

      <div class="subheading">All pages · {{ wikiStore.pages.length }}</div>
      <div v-if="wikiStore.loadingPages && wikiStore.pages.length === 0" class="page-empty">
        Loading pages…
      </div>
      <div v-else-if="wikiStore.pages.length === 0" class="page-empty">
        No approved pages yet. Generate and approve a Source Change Set first.
      </div>
      <template v-else>
        <button
          v-for="page in wikiStore.pages"
          :key="page.id"
          type="button"
          :class="['page-row', { selected: page.id === wikiStore.selectedPageId }]"
          data-testid="wiki-page-row"
          @click="openPage(page.id)"
        >
          <strong>{{ page.title }}</strong>
          <span>/{{ page.slug }} · v{{ page.version }}</span>
        </button>
      </template>
    </aside>

    <article class="page-reader">
      <div v-if="!wikiStore.selectedPage" class="reader-empty">
        Select a page to read its approved Markdown and revision history.
      </div>
      <template v-else>
        <header class="reader-header">
          <div>
            <h1>{{ viewedRevision?.title ?? wikiStore.selectedPage.title }}</h1>
            <span>
              /{{ wikiStore.selectedPage.slug }} · approved revision
              {{ viewedRevision?.version ?? wikiStore.selectedPage.version }}
            </span>
          </div>
          <div class="revision-picker" aria-label="Page revision history">
            <button
              v-for="revision in wikiStore.pageRevisions"
              :key="revision.id"
              type="button"
              :class="{ active: revision.id === viewedRevision?.id }"
              :title="`${revision.author_kind} · Change Set ${revision.change_set_id}`"
              @click="viewedRevisionId = revision.id"
            >
              v{{ revision.version }}
            </button>
          </div>
        </header>
        <!-- renderMarkdown disables raw HTML and hardens outbound links. -->
        <!-- eslint-disable vue/no-v-html -->
        <div
          v-if="viewedRevision"
          class="wiki-markdown"
          data-testid="wiki-page-markdown"
          v-html="renderedPage"
        ></div>
        <!-- eslint-enable vue/no-v-html -->
        <div v-else class="reader-empty">Loading revision…</div>
      </template>
    </article>
  </section>
</template>

<style scoped>
.pages-view {
  display: grid;
  height: 100%;
  min-height: 0;
  grid-template-columns: 300px minmax(0, 1fr);
}
.page-index {
  min-height: 0;
  overflow-y: auto;
  padding: 16px 12px;
  border-right: 1px solid var(--border);
  background: #f8fafc;
}
.page-index-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.page-index h2 {
  margin: 0;
  font-size: 17px;
}
.page-index-header span,
.page-row span {
  color: var(--muted);
  font-size: 10px;
}
.page-index-header button,
.revision-picker button {
  padding: 4px 7px;
  border-color: var(--border);
  background: #fff;
  color: var(--muted);
  font-size: 10px;
}
.page-search {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px;
  margin: 13px 0;
}
.page-search input {
  min-width: 0;
  padding: 7px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  font-size: 11px;
}
.subheading {
  margin: 13px 4px 6px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.search-results {
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border);
}
.search-hit,
.page-row {
  display: flex;
  width: 100%;
  flex-direction: column;
  gap: 3px;
  align-items: flex-start;
  margin-bottom: 5px;
  padding: 8px 9px;
  border: 1px solid transparent;
  background: transparent;
  text-align: left;
}
.search-hit {
  border-color: #dbeafe;
  background: #eff6ff;
}
.search-hit strong,
.page-row strong {
  color: var(--fg);
  font-size: 12px;
}
.search-hit span {
  display: -webkit-box;
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.page-row:hover,
.page-row.selected {
  border-color: #bfdbfe;
  background: #fff;
}
.page-empty,
.reader-empty {
  padding: 20px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
  text-align: center;
}
.page-reader {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  background: #fff;
}
.reader-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 22px 28px 14px;
  border-bottom: 1px solid var(--border);
}
.reader-header h1 {
  margin: 0 0 4px;
  color: var(--fg);
  font-size: 22px;
}
.reader-header span {
  color: var(--muted);
  font-size: 11px;
}
.revision-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.revision-picker button.active {
  border-color: #60a5fa;
  background: #eff6ff;
  color: #1d4ed8;
}
.wiki-markdown {
  max-width: 860px;
  margin: 0 auto;
  padding: 28px 36px 80px;
  color: #1e293b;
  font-size: 14px;
  line-height: 1.7;
}
.wiki-markdown :deep(pre) {
  overflow-x: auto;
  padding: 12px;
  border-radius: 7px;
  background: #f1f5f9;
}
.wiki-markdown :deep(table) {
  width: 100%;
  border-collapse: collapse;
}
.wiki-markdown :deep(th),
.wiki-markdown :deep(td) {
  padding: 6px 8px;
  border: 1px solid var(--border);
}
@media (max-width: 760px) {
  .pages-view {
    display: block;
    overflow-y: auto;
  }
  .page-index {
    max-height: 330px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
}
</style>
