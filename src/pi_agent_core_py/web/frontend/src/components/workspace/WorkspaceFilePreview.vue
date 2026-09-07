<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue"

import { downloadFileUrl } from "../../api/files"
import type { FileRef } from "../../types"
import { formatFileSize, isSupported, refFormat } from "../../utils/files"
import { renderMarkdown } from "../../utils/markdown"
import { highlightPython } from "../../utils/pythonHighlight"

const props = defineProps<{
  file: FileRef
  sessionId: string
  loadContent: (fileId: string) => Promise<string>
  saveContent: (fileId: string, content: string, expectedSha256: string) => Promise<FileRef>
  downloadFile?: () => Promise<void>
}>()

const emit = defineEmits<{
  (event: "saved", file: FileRef): void
}>()

const content = ref("")
const baseline = ref("")
const busy = ref(false)
const error = ref("")
const editing = ref(false)
const downloading = ref(false)
const fileRevision = computed(() =>
  JSON.stringify([props.sessionId, props.file.id, props.file.sha256]),
)
let viewGeneration = 0

function isCurrent(generation: number, revision: string): boolean {
  return generation === viewGeneration && revision === fileRevision.value
}

const format = computed(() => refFormat(props.file))
const logicalPath = computed(() => props.file.logical_path || props.file.name)
const isMarkdown = computed(() => format.value === "markdown")
const isPython = computed(() => /\.py(?:i|w)?$/i.test(logicalPath.value))
const isReadOnly = computed(
  () =>
    props.file.content_editable === false ||
    props.file.purpose === "document_conversion" ||
    props.file.origin === "sandbox",
)
const readOnlyMessage = computed(() => {
  if (props.file.purpose === "document_conversion") {
    return "Generated document output is read-only."
  }
  if (props.file.origin === "sandbox") {
    return "Pending Sandbox output is read-only until published."
  }
  return "This Workspace file is read-only."
})
const canEditMarkdown = computed(() => isMarkdown.value && !isReadOnly.value)
const canRead = computed(() => isSupported(format.value))
const renderedMarkdown = computed(() => renderMarkdown(content.value))
const pythonTokens = computed(() => (isPython.value ? highlightPython(content.value) : []))
const dirty = computed(() => content.value !== baseline.value)
const originLabel = computed(() => {
  if (props.file.purpose === "document_conversion") return "Generated document"
  if (props.file.purpose === "document_original") return "Immutable original"
  if (props.file.origin === "sandbox") return "Pending approval"
  if (props.file.origin === "agent") return "Agent result"
  if (props.file.origin === "upload") return "Uploaded"
  if (props.file.purpose === "memory") return "Session memory"
  if (props.file.purpose === "agent_instructions") return "Agent instructions"
  return props.file.origin || "Workspace file"
})

async function load(): Promise<void> {
  const generation = ++viewGeneration
  const revision = fileRevision.value
  const file = props.file
  editing.value = false
  busy.value = false
  downloading.value = false
  error.value = ""
  content.value = ""
  baseline.value = ""
  if (!canRead.value) return
  busy.value = true
  try {
    const loaded = await props.loadContent(file.id)
    if (!isCurrent(generation, revision)) return
    content.value = loaded
    baseline.value = loaded
  } catch (cause: any) {
    if (isCurrent(generation, revision)) {
      error.value = String(cause?.message || `Unable to load ${file.name}`)
    }
  } finally {
    if (isCurrent(generation, revision)) busy.value = false
  }
}

async function save(): Promise<void> {
  if (!canEditMarkdown.value || !dirty.value || busy.value) return
  const generation = viewGeneration
  const revision = fileRevision.value
  const file = props.file
  const submittedContent = content.value
  busy.value = true
  error.value = ""
  try {
    const updated = await props.saveContent(file.id, submittedContent, file.sha256)
    if (!isCurrent(generation, revision)) return
    baseline.value = submittedContent
    editing.value = false
    emit("saved", updated)
  } catch (cause: any) {
    if (isCurrent(generation, revision)) {
      error.value = String(cause?.message || `Unable to save ${file.name}`)
    }
  } finally {
    if (isCurrent(generation, revision)) busy.value = false
  }
}

async function download(): Promise<void> {
  if (!props.downloadFile || downloading.value) return
  const generation = viewGeneration
  const revision = fileRevision.value
  const name = props.file.name
  downloading.value = true
  error.value = ""
  try {
    await props.downloadFile()
  } catch (cause: any) {
    if (isCurrent(generation, revision)) {
      error.value = String(cause?.message || `Unable to download ${name}`)
    }
  } finally {
    if (isCurrent(generation, revision)) downloading.value = false
  }
}

// A late read/save from another file, version or Session cannot update this view.
watch(fileRevision, load, { immediate: true })
onBeforeUnmount(() => { viewGeneration += 1 })
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
      <button
        v-if="downloadFile"
        type="button"
        class="preview-download preview-download-button"
        :disabled="downloading"
        :aria-label="`Download ${file.name}`"
        @click="download"
      >
        {{ downloading ? "Downloading…" : "Download" }}
      </button>
      <a
        v-else
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
        <div v-if="isReadOnly" class="preview-readonly">{{ readOnlyMessage }}</div>
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
      </template>
      <pre
        v-else-if="isPython"
        class="code-preview python-preview"
        :aria-label="`${file.name} code`"
        data-testid="python-syntax-preview"
      ><code><span
        v-for="(token, index) in pythonTokens"
        :key="index"
        :class="`python-${token.kind}`"
      >{{ token.text }}</span></code></pre>
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
.preview-download-button {
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
}
.preview-download-button:disabled {
  cursor: default;
  opacity: 0.6;
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
.python-preview {
  border-color: #d0d7de;
  background: #fff;
  color: #24292f;
  font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  tab-size: 4;
}
.python-keyword {
  color: #0000ff;
}
.python-literal {
  color: #0000ff;
}
.python-builtin,
.python-class {
  color: #267f99;
}
.python-function {
  color: #795e26;
}
.python-self {
  color: #001080;
}
.python-number {
  color: #098658;
}
.python-string {
  color: #a31515;
}
.python-comment {
  color: #008000;
}
.python-decorator {
  color: #795e26;
}
.python-operator {
  color: #24292f;
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
