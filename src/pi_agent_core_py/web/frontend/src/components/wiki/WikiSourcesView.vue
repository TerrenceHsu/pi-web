<script setup lang="ts">
import { computed, ref } from "vue"

import { useWikiStore } from "../../stores/wikiStore"
import type { WikiParseMode, WikiSource } from "../../types/wiki"

const wikiStore = useWikiStore()
const fileInput = ref<HTMLInputElement | null>(null)
const parseMode = ref<Exclude<WikiParseMode, "builtin">>("auto")

const selectedRevision = computed(() =>
  wikiStore.parseRevisions.find(
    (revision) => revision.id === wikiStore.selectedSource?.selected_parse_revision_id,
  ),
)

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`
}

function statusLabel(source: WikiSource): string {
  if (source.status === "failed" && source.safe_error_code) {
    return `failed · ${source.safe_error_code}`
  }
  return source.status
}

async function uploadSelected(): Promise<void> {
  const file = fileInput.value?.files?.[0]
  if (!file) return
  const uploaded = await wikiStore.uploadSource(file, parseMode.value)
  if (uploaded && fileInput.value) fileInput.value.value = ""
}

async function retryParse(source: WikiSource): Promise<void> {
  await wikiStore.enqueueParse(source.id, parseMode.value)
}

async function deleteSource(source: WikiSource): Promise<void> {
  const confirmed = window.confirm(
    `Delete Raw content for “${source.display_name}”? ` +
      "The request cannot be undone and is blocked while approved pages still reference it.",
  )
  if (confirmed) await wikiStore.deleteSource(source.id)
}

const workflowLabel = computed(() => {
  if (wikiStore.sourceWorkflowStage === "summarizing") return "1/4 Agent summarizing…"
  if (wikiStore.sourceWorkflowStage === "entry_page") return "2/4 Creating entry proposal…"
  if (wikiStore.sourceWorkflowStage === "topic_pages") return "3/4 Creating topic proposals…"
  if (wikiStore.sourceWorkflowStage === "change_set") return "4/4 Freezing complete diff…"
  return "Prepare page Change Set"
})
</script>

<template>
  <section class="sources-view" data-testid="wiki-sources-view">
    <div class="source-column">
      <div class="section-heading">
        <div>
          <h2>Sources</h2>
          <p>Immutable PDF or single-file HTML originals.</p>
        </div>
        <button
          type="button"
          class="quiet-button"
          :disabled="wikiStore.loadingSources || !wikiStore.selectedSpaceId"
          @click="wikiStore.loadSources()"
        >
          Refresh
        </button>
      </div>

      <div class="upload-card">
        <input
          ref="fileInput"
          data-testid="wiki-source-file"
          type="file"
          accept=".pdf,.html,application/pdf,text/html"
          :disabled="wikiStore.mutating"
        />
        <select v-model="parseMode" aria-label="PDF parse mode" :disabled="wikiStore.mutating">
          <option value="auto">Auto</option>
          <option value="fast">Fast</option>
          <option value="accurate">Accurate</option>
        </select>
        <button
          type="button"
          data-testid="wiki-upload-source"
          :disabled="wikiStore.mutating || !wikiStore.selectedSpaceId"
          @click="uploadSelected"
        >
          {{ wikiStore.mutating ? "Working…" : "Upload" }}
        </button>
      </div>

      <div v-if="wikiStore.loadingSources" class="empty-card">Loading Sources…</div>
      <div v-else-if="wikiStore.sources.length === 0" class="empty-card">
        Upload the first PDF or HTML source for this Wiki Space.
      </div>
      <div v-else class="source-list">
        <article
          v-for="source in wikiStore.sources"
          :key="source.id"
          :class="['source-row', { selected: source.id === wikiStore.selectedSourceId }]"
          data-testid="wiki-source-row"
          @click="wikiStore.selectSource(source.id)"
        >
          <div class="source-main">
            <strong>{{ source.display_name }}</strong>
            <span>{{ formatBytes(source.size_bytes) }} · {{ source.mime_type }}</span>
          </div>
          <span :class="['status-pill', source.status]">{{ statusLabel(source) }}</span>
          <button
            v-if="source.status === 'failed' || source.status === 'parsed'"
            type="button"
            class="quiet-button"
            :disabled="wikiStore.mutating"
            @click.stop="retryParse(source)"
          >
            Parse again
          </button>
          <button
            v-if="source.status !== 'deleting'"
            type="button"
            class="danger-button"
            :disabled="wikiStore.mutating"
            @click.stop="deleteSource(source)"
          >
            Delete Raw
          </button>
        </article>
      </div>
    </div>

    <div class="artifact-column">
      <div v-if="!wikiStore.selectedSource" class="empty-card tall">
        Select a Source to inspect its current Raw parse bundle.
      </div>
      <template v-else>
        <div class="section-heading detail-heading">
          <div>
            <h2>{{ wikiStore.selectedSource.display_name }}</h2>
            <p>
              Raw is read-only to the Agent. Original and every parse revision remain immutable.
            </p>
          </div>
        </div>

        <div v-if="selectedRevision" class="revision-card">
          <span>Selected parse</span>
          <strong>{{ selectedRevision.parser }} {{ selectedRevision.parser_version }}</strong>
          <span>
            {{ selectedRevision.preset }} · {{ selectedRevision.page_count }} page(s) ·
            {{ selectedRevision.requested_mode }}
          </span>
        </div>

        <div v-if="wikiStore.selectedSource.status === 'deleting'" class="deletion-card">
          Raw access is disabled. Physical files are removed after the retention window; audit
          metadata remains.
        </div>

        <div v-if="wikiStore.selectedSource.status === 'parsed'" class="source-workflow-card">
          <div>
            <strong>Build approved Wiki pages</strong>
            <span>
              Agent summary → entry page → topic pages → one Change Set. No page is published until
              you approve its complete diff.
            </span>
          </div>
          <button
            type="button"
            data-testid="wiki-prepare-source-change-set"
            :disabled="wikiStore.mutating || !wikiStore.selectedSource.selected_parse_revision_id"
            @click="wikiStore.prepareSourceChangeSet(wikiStore.selectedSource.id)"
          >
            {{ workflowLabel }}
          </button>
        </div>

        <div v-if="wikiStore.loadingSourceDetails" class="empty-card">Loading Raw artifacts…</div>
        <div v-else-if="wikiStore.artifacts.length === 0" class="empty-card">
          No selected parse artifacts are available yet.
        </div>
        <div v-else class="artifact-layout">
          <div class="artifact-list" aria-label="Raw artifacts">
            <button
              v-for="artifact in wikiStore.artifacts"
              :key="artifact.id"
              type="button"
              :class="[
                'artifact-row',
                { selected: artifact.id === wikiStore.artifactPreview?.artifactId },
              ]"
              data-testid="wiki-artifact-row"
              @click="wikiStore.previewArtifact(artifact)"
            >
              <span>{{ artifact.kind }}</span>
              <small>{{ artifact.relpath }} · {{ formatBytes(artifact.size_bytes) }}</small>
            </button>
          </div>

          <div class="artifact-preview" data-testid="wiki-artifact-preview">
            <div v-if="wikiStore.loadingPreview" class="empty-card">Loading preview…</div>
            <div v-else-if="!wikiStore.artifactPreview" class="empty-card tall">
              Choose an artifact. Markdown and manifests are shown as plain text.
            </div>
            <pre v-else-if="wikiStore.artifactPreview.kind === 'text'">{{
              wikiStore.artifactPreview.text
            }}</pre>
            <img
              v-else-if="
                wikiStore.artifactPreview.kind === 'image' && wikiStore.artifactPreview.objectUrl
              "
              :src="wikiStore.artifactPreview.objectUrl"
              alt="Extracted PDF image"
            />
            <div v-else class="empty-card">{{ wikiStore.artifactPreview.text }}</div>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.sources-view {
  display: grid;
  grid-template-columns: minmax(320px, 0.8fr) minmax(420px, 1.2fr);
  min-height: 0;
  height: 100%;
}
.source-column,
.artifact-column {
  min-width: 0;
  overflow: auto;
  padding: 20px;
}
.source-column {
  border-right: 1px solid var(--border);
}
.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.section-heading h2 {
  margin: 0;
  color: var(--fg);
  font-size: 18px;
}
.section-heading p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
}
.upload-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 100px auto;
  gap: 8px;
  align-items: center;
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: #fff;
}
.upload-card input,
.upload-card select {
  min-width: 0;
  font-size: 12px;
}
.source-list,
.artifact-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.source-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: #fff;
  cursor: pointer;
}
.source-row:hover,
.source-row.selected {
  border-color: #93c5fd;
  background: #f8fbff;
}
.source-main {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 3px;
}
.source-main strong {
  overflow: hidden;
  color: var(--fg);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-main span,
.revision-card span,
.artifact-row small {
  color: var(--muted);
  font-size: 11px;
}
.status-pill {
  padding: 3px 7px;
  border-radius: 999px;
  background: #f1f5f9;
  color: #475569;
  font-size: 10px;
}
.status-pill.parsed {
  background: #dcfce7;
  color: #166534;
}
.status-pill.parsing {
  background: #dbeafe;
  color: #1d4ed8;
}
.status-pill.failed {
  background: #fee2e2;
  color: #b91c1c;
}
.quiet-button {
  padding: 5px 8px;
  border-color: var(--border);
  background: #fff;
  color: var(--muted);
  font-size: 11px;
}
.revision-card {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
  background: #eff6ff;
  font-size: 12px;
}
.source-workflow-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid #c7d2fe;
  border-radius: 8px;
  background: #eef2ff;
}
.source-workflow-card > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}
.source-workflow-card strong {
  color: #312e81;
  font-size: 12px;
}
.source-workflow-card span {
  color: #64748b;
  font-size: 10px;
  line-height: 1.45;
}
.source-workflow-card button {
  flex: 0 0 auto;
  border-color: #4338ca;
  background: #4f46e5;
  color: #fff;
  font-size: 11px;
}

.danger-button {
  border: 1px solid #dc2626;
  border-radius: 6px;
  background: transparent;
  color: #b91c1c;
  cursor: pointer;
  padding: 0.35rem 0.55rem;
}

.deletion-card {
  border: 1px solid #fca5a5;
  border-radius: 8px;
  background: #fef2f2;
  color: #991b1b;
  padding: 0.75rem;
}
.artifact-layout {
  display: grid;
  grid-template-columns: minmax(190px, 0.45fr) minmax(0, 1fr);
  gap: 12px;
  min-height: 460px;
}
.artifact-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  align-items: flex-start;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: #fff;
  text-align: left;
}
.artifact-row.selected {
  border-color: #60a5fa;
  background: #eff6ff;
}
.artifact-row small {
  overflow-wrap: anywhere;
}
.artifact-preview {
  min-width: 0;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: #fff;
}
.artifact-preview pre {
  min-height: 100%;
  margin: 0;
  padding: 16px;
  overflow-wrap: anywhere;
  color: #1e293b;
  font:
    12px/1.55 ui-monospace,
    SFMono-Regular,
    Consolas,
    monospace;
  white-space: pre-wrap;
}
.artifact-preview img {
  display: block;
  max-width: 100%;
  margin: 0 auto;
  padding: 16px;
}
.empty-card {
  padding: 18px;
  border: 1px dashed var(--border-strong);
  border-radius: 9px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.empty-card.tall {
  display: grid;
  min-height: 220px;
  place-items: center;
}
@media (max-width: 960px) {
  .sources-view {
    display: block;
    overflow: auto;
  }
  .source-column,
  .artifact-column {
    overflow: visible;
  }
  .source-column {
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
}
@media (max-width: 620px) {
  .upload-card,
  .artifact-layout {
    grid-template-columns: 1fr;
  }
}
</style>
