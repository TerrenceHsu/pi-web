<script setup lang="ts">
import { computed, ref, watch } from "vue"

import { downloadFileUrl } from "../../api/files"
import type { FileRef } from "../../types"
import { formatFileSize, isSupported, refFormat } from "../../utils/files"
import { renderMarkdown } from "../../utils/markdown"

const props = defineProps<{
  file: FileRef
  sessionId: string
  loadContent: (fileId: string) => Promise<string>
  saveContent: (fileId: string, content: string, expectedSha256: string) => Promise<FileRef>
}>()

const emit = defineEmits<{
  (event: "saved", file: FileRef): void
}>()

const content = ref("")
const baseline = ref("")
const busy = ref(false)
const error = ref("")
const editing = ref(false)

const format = computed(() => refFormat(props.file))
const isMarkdown = computed(() => format.value === "markdown")
const isDocumentOutput = computed(() => props.file.purpose === "document_conversion")
const canEditMarkdown = computed(() => isMarkdown.value && !isDocumentOutput.value)
const canRead = computed(() => isSupported(format.value))
const renderedMarkdown = computed(() => renderMarkdown(content.value))
const dirty = computed(() => content.value !== baseline.value)
const logicalPath = computed(() => props.file.logical_path || props.file.name)
const originLabel = computed(() => {
  if (props.file.purpose === "document_conversion") return "Generated document"
  if (props.file.purpose === "document_original") return "Immutable original"
  if (props.file.origin === "agent") return "Agent result"
  if (props.file.origin === "upload") return "Uploaded"
  if (props.file.purpose === "memory") return "Session memory"
  if (props.file.purpose === "agent_instructions") return "Agent instructions"
  return props.file.origin || "Workspace file"
})

async function load(): Promise<void> {
  editing.value = false
  error.value = ""
  content.value = ""
  baseline.value = ""
  if (!canRead.value) return
  busy.value = true
  try {
    const loaded = await props.loadContent(props.file.id)
    content.value = loaded
    baseline.value = loaded
  } catch (cause: any) {
    error.value = String(cause?.message || `Unable to load ${props.file.name}`)
  } finally {
    busy.value = false
  }
}

async function save(): Promise<void> {
  if (!canEditMarkdown.value || !dirty.value) return
  busy.value = true
  error.value = ""
  try {
    const updated = await props.saveContent(props.file.id, content.value, props.file.sha256)
    baseline.value = content.value
    editing.value = false
    emit("saved", updated)
  } catch (cause: any) {
    error.value = String(cause?.message || `Unable to save ${props.file.name}`)
  } finally {
    busy.value = false
  }
}

watch(() => [props.file.id, props.file.sha256], load, { immediate: true })
</script>

<template>
  <article class="workspace-preview" data-testid="workspace-file-preview">
    <header class="preview-header">
      <div class="preview-title">
        <span class="preview-kicker">{{ originLabel }}</span>
        <strong :title="logicalPath">{{ logicalPath }}</strong>
        <span class="preview-meta">
          {{ format
          }}<template v-if="formatFileSize(file.size)"> · {{ formatFileSize(file.size) }}</template>
        </span>
      </div>
      <a
        :href="downloadFileUrl(sessionId, file.id)"
        :download="file.name"
        class="preview-download"
        :aria-label="`Download ${file.name}`"
        >Download</a
      >
    </header>

    <div v-if="canEditMarkdown" class="preview-mode" role="tablist" aria-label="Markdown view mode">
      <button
        type="button"
        :class="{ active: !editing }"
        role="tab"
        :aria-selected="!editing"
        @click="editing = false"
      >
        Preview
      </button>
      <button
        type="button"
        :class="{ active: editing }"
        role="tab"
        :aria-selected="editing"
        @click="editing = true"
      >
        Edit
      </button>
    </div>

    <div v-if="busy && !content" class="preview-state">Loading result…</div>
    <div v-else-if="error" class="preview-error" role="alert">{{ error }}</div>
    <template v-else-if="canRead">
      <template v-if="isMarkdown">
        <div v-if="!editing" class="markdown-preview markdown-body">
          <!-- markdown-it disables raw HTML; only parser output is rendered. -->
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div v-html="renderedMarkdown"></div>
        </div>
        <div v-else-if="canEditMarkdown" class="markdown-editor">
          <textarea
            v-model="content"
            :aria-label="`${file.name} content`"
            spellcheck="false"
            :disabled="busy"
          />
          <div class="editor-actions">
            <span>{{ dirty ? "Unsaved changes" : "Saved" }}</span>
            <button type="button" :disabled="busy || !dirty" @click="save">
              {{ busy ? "Saving…" : "Save" }}
            </button>
          </div>
        </div>
        <div v-else class="preview-readonly">Generated document output is read-only.</div>
      </template>
      <pre
        v-else
        class="code-preview"
        :aria-label="`${file.name} code`"
      ><code>{{ content }}</code></pre>
    </template>
    <div v-else class="preview-state">
      Preview is unavailable for this file type. Download the original file to inspect it.
    </div>
  </article>
</template>

<style scoped>
.workspace-preview {
  display: flex;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  overflow: hidden;
  background: var(--chat-bg);
}
.preview-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
}
.preview-title {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 1px;
}
.preview-title strong {
  overflow: hidden;
  color: var(--fg);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.preview-kicker,
.preview-meta {
  color: var(--muted);
  font-size: 10px;
}
.preview-kicker {
  color: var(--accent);
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.preview-download {
  flex: 0 0 auto;
  color: var(--accent);
  font-size: 11px;
  text-decoration: none;
}
.preview-mode {
  display: flex;
  gap: 4px;
  padding: 8px 14px 0;
}
.preview-mode button {
  padding: 3px 8px;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 11px;
}
.preview-mode button.active {
  background: var(--code-bg);
  color: var(--fg);
}
.markdown-preview,
.code-preview,
.markdown-editor,
.preview-state,
.preview-error {
  min-height: 0;
  flex: 1;
  margin: 10px 14px 14px;
  overflow: auto;
}
.markdown-preview {
  color: var(--fg);
  font-size: 13px;
}
.markdown-preview :deep(> div > :first-child) {
  margin-top: 0;
}
.markdown-preview :deep(> div > :last-child) {
  margin-bottom: 0;
}
.markdown-preview :deep(h1),
.markdown-preview :deep(h2),
.markdown-preview :deep(h3) {
  margin: 0.9em 0 0.45em;
  line-height: 1.25;
}
.markdown-preview :deep(h1) {
  font-size: 1.45em;
}
.markdown-preview :deep(h2) {
  font-size: 1.25em;
}
.markdown-preview :deep(h3) {
  font-size: 1.1em;
}
.markdown-preview :deep(p) {
  margin: 0.55em 0;
}
.markdown-preview :deep(ul),
.markdown-preview :deep(ol) {
  padding-left: 1.5em;
}
.markdown-preview :deep(pre) {
  overflow: auto;
  border: 1px solid var(--border);
  white-space: pre;
}
.markdown-preview :deep(table) {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
.markdown-preview :deep(th),
.markdown-preview :deep(td) {
  padding: 5px;
  border: 1px solid var(--border);
}
.code-preview {
  padding: 12px;
  border: 1px solid var(--border);
  background: #f8fafc;
  font-size: 11px;
  line-height: 1.55;
  white-space: pre;
}
.code-preview code {
  padding: 0;
  background: transparent;
}
.markdown-editor {
  display: flex;
  flex-direction: column;
}
.markdown-editor textarea {
  min-height: 260px;
  flex: 1;
  resize: none;
  font:
    11px/1.55 ui-monospace,
    SFMono-Regular,
    Menlo,
    Consolas,
    monospace;
}
.editor-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 8px;
  color: var(--muted);
  font-size: 10px;
}
.editor-actions button {
  padding: 4px 10px;
  font-size: 11px;
}
.preview-state,
.preview-error {
  display: grid;
  place-items: center;
  padding: 18px;
  color: var(--muted);
  text-align: center;
  font-size: 12px;
}
.preview-error {
  color: var(--danger);
}
</style>
