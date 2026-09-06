<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"

import { downloadSandboxArtifactFile, readSandboxArtifactFile } from "../../api/codingSandbox"
import type { FileReadItem, FileRef } from "../../types"
import { buildSessionFileTree } from "../../utils/fileTree"
import { inferFileFormat } from "../../utils/files"
import { useChatStore } from "../../stores/chatStore"
import { useCodingSandboxStore } from "../../stores/codingSandboxStore"
import { useFileStore } from "../../stores/fileStore"
import { useSessionStore } from "../../stores/sessionStore"
import CodingSandboxModal from "../coding-sandbox/CodingSandboxModal.vue"
import FileTreeNode from "../chat/FileTreeNode.vue"
import WorkspaceFilePreview from "./WorkspaceFilePreview.vue"
import WorkspaceExtensions from "./WorkspaceExtensions.vue"
import DataAnalysisPanel from "./DataAnalysisPanel.vue"

type WorkspaceTab = "files" | "extensions" | "analysis" | "sandbox" | "changes"

interface AgentArtifactSignal {
  signature: string
  fileId: string
  logicalPath?: string
  revision?: number
}

const sessionStore = useSessionStore()
const chatStore = useChatStore()
const fileStore = useFileStore()
const sandboxStore = useCodingSandboxStore()

const activeTab = ref<WorkspaceTab>("files")
const sandboxModalOpen = ref(false)
const createOpen = ref(false)
const markdownPath = ref("docs/notes/new-note.md")
const markdownContent = ref("# New note\n")
const fileInput = ref<HTMLInputElement | null>(null)
const handledArtifactSignals = new Set<string>()
const handledPublishSignals = new Set<string>()
const selectedSandboxFileId = ref<string | null>(null)
const workspacePanel = ref<HTMLElement | null>(null)
const filesPane = ref<HTMLElement | null>(null)

const FILES_PANE_MIN = 120
const FILES_PANE_MAX = 700
const PREVIEW_MIN = 180
const FILES_RESIZER_HEIGHT = 7
const FILES_PANE_HEIGHT_KEY = "pi-agent-workspace-files-pane-height"

function storedFilesPaneHeight(): number {
  try {
    const value = Number.parseInt(window.localStorage.getItem(FILES_PANE_HEIGHT_KEY) ?? "", 10)
    return Number.isFinite(value) ? Math.min(Math.max(value, FILES_PANE_MIN), FILES_PANE_MAX) : 260
  } catch {
    return 260
  }
}

const filesPaneHeight = ref(storedFilesPaneHeight())
const filesResizing = ref(false)
let filesResizeStartY = 0
let filesResizeStartHeight = 0

const sessionId = computed(() => sessionStore.activeSessionId)
const files = computed(() => {
  const sid = sessionId.value
  return sid ? (fileStore.filesBySession[sid] ?? []) : []
})
const workspace = computed(() => {
  const sid = sessionId.value
  return sid ? fileStore.workspaceBySession[sid] : undefined
})
const codeContinuity = computed(() => workspace.value?.code_continuity)
const codeContinuityLabel = computed(() => {
  const status = codeContinuity.value?.status ?? "not_initialized"
  if (status === "current") return "Code docs current"
  if (status === "failed") return "Code docs failed"
  if (status === "stale") return "Code docs stale"
  return "Code docs pending"
})
const pendingSandboxFiles = computed<FileRef[]>(() => {
  const current = operation.value
  if (
    !current ||
    !["awaiting_approval", "publish_conflict"].includes(current.status) ||
    !current.diff
  )
    return []
  return current.diff.entries
    .filter((entry) => entry.status !== "deleted")
    .map((entry) => {
      const name = entry.path.split("/").at(-1) || entry.path
      return {
        id: `sandbox:${current.operation_id}:${entry.path}`,
        session_id: current.session_id,
        name,
        logical_path: entry.path,
        origin: "sandbox",
        purpose: "file",
        mime: "application/octet-stream",
        format: inferFileFormat(name),
        size: 0,
        sha256: entry.after_sha256 || "",
        category: "code",
        owner: "sandbox",
        content_editable: false,
        movable: false,
        deletable: false,
        agent_writable: false,
        sandbox_publishable: true,
        immutable: true,
      }
    })
})
const displayedFiles = computed(() => {
  const pendingPaths = new Set(
    pendingSandboxFiles.value.map((file) => file.logical_path?.toLocaleLowerCase()),
  )
  return [
    ...files.value.filter((file) => !pendingPaths.has(file.logical_path?.toLocaleLowerCase())),
    ...pendingSandboxFiles.value,
  ]
})
const selectedFileId = computed(() => {
  const sid = sessionId.value
  return selectedSandboxFileId.value || (sid ? fileStore.selectedFileIdBySession[sid] : null)
})
const selectedFile = computed(() =>
  displayedFiles.value.find((file) => file.id === selectedFileId.value),
)
const latestArtifact = computed(() => {
  const sid = sessionId.value
  return sid ? fileStore.latestArtifactBySession[sid] : null
})
const tree = computed(() => buildSessionFileTree(displayedFiles.value))
const operation = computed(() => sandboxStore.operation)
const selectedSandboxPath = computed(() => {
  if (selectedFile.value?.origin !== "sandbox") return null
  return selectedFile.value.logical_path || null
})

function recordOf(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : null
}

function artifactFromItem(item: FileReadItem): AgentArtifactSignal | null {
  if (item.toolName !== "write_file" || item.status !== "done") return null
  const root = recordOf(item.details)
  const nested = recordOf(root?.details)
  const details = nested ?? root
  const fileId = details?.file_id
  if (typeof fileId !== "string" || !fileId) return null
  const revision = details?.workspace_revision
  const logicalPath = details?.logical_path
  return {
    signature: `${item.id}:${fileId}:${String(revision ?? "")}`,
    fileId,
    logicalPath: typeof logicalPath === "string" ? logicalPath : undefined,
    revision: typeof revision === "number" ? revision : undefined,
  }
}

const latestAgentArtifactSignal = computed(() => {
  for (let index = chatStore.streamItems.length - 1; index >= 0; index -= 1) {
    const item = chatStore.streamItems[index]
    if (item.kind !== "file_read") continue
    const artifact = artifactFromItem(item)
    if (artifact) return artifact
  }
  return null
})

watch(
  () => latestAgentArtifactSignal.value?.signature,
  async () => {
    const sid = sessionId.value
    const artifact = latestAgentArtifactSignal.value
    if (!sid || !artifact || handledArtifactSignals.has(`${sid}:${artifact.signature}`)) return
    const handled = await fileStore.revealAgentArtifact(sid, artifact)
    if (!handled || sessionId.value !== sid) return
    handledArtifactSignals.add(`${sid}:${artifact.signature}`)
    activeTab.value = "files"
  },
  { immediate: true },
)

watch(sessionId, () => {
  activeTab.value = "files"
  createOpen.value = false
  selectedSandboxFileId.value = null
})

watch(
  () =>
    [
      sessionId.value,
      operation.value?.status,
      operation.value?.published_workspace_revision,
    ] as const,
  async ([sid, status, revision]) => {
    const current = operation.value
    if (!sid || status !== "published" || revision == null || !current) return
    const signal = `${sid}:${current.operation_id}:${revision}`
    if (handledPublishSignals.has(signal)) return
    await fileStore.revealPublishedWorkspace(
      sid,
      current.changed_paths,
      revision,
      current.operation_id,
    )
    if (fileStore.workspaceBySession[sid]?.revision !== revision) return
    handledPublishSignals.add(signal)
    activeTab.value = "files"
  },
  { immediate: true },
)

watch(
  () => operation.value?.status,
  (status) => {
    if (status === "awaiting_approval" || status === "publish_conflict") {
      activeTab.value = "files"
    } else if (status !== "publishing" && status !== "freezing") {
      selectedSandboxFileId.value = null
    }
  },
  { immediate: true },
)

async function refresh(): Promise<void> {
  if (sessionId.value) await fileStore.loadFiles(sessionId.value)
}

function selectFile(fileId: string): void {
  if (!sessionId.value) return
  if (pendingSandboxFiles.value.some((file) => file.id === fileId)) {
    selectedSandboxFileId.value = fileId
    return
  }
  selectedSandboxFileId.value = null
  fileStore.selectFile(sessionId.value, fileId)
}

async function deleteFile(fileId: string): Promise<void> {
  if (!sessionId.value) return
  if (!window.confirm("Delete this file from the Workspace?")) return
  await fileStore.deleteFile(sessionId.value, fileId)
}

async function loadContent(fileId: string): Promise<string> {
  if (!sessionId.value) throw new Error("No active session")
  const pending = pendingSandboxFiles.value.find((file) => file.id === fileId)
  if (pending?.logical_path && operation.value) {
    return readSandboxArtifactFile(operation.value.operation_id, pending.logical_path)
  }
  return fileStore.readTextFile(sessionId.value, fileId)
}

async function saveContent(
  fileId: string,
  content: string,
  expectedSha256: string,
): Promise<FileRef> {
  if (!sessionId.value) throw new Error("No active session")
  return fileStore.updateTextFile(sessionId.value, fileId, content, expectedSha256)
}

async function downloadSelectedSandboxFile(): Promise<void> {
  const current = operation.value
  const path = selectedSandboxPath.value
  if (!current || !path) throw new Error("Frozen Sandbox file is unavailable")
  await downloadSandboxArtifactFile(current.operation_id, path)
}

function openUpload(): void {
  fileInput.value?.click()
}

async function uploadSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  if (!input.files?.length) return
  try {
    await uploadFiles(input.files)
  } finally {
    input.value = ""
  }
}

async function uploadFiles(files: FileList): Promise<void> {
  if (!sessionId.value || fileStore.uploading || !files.length) return
  try {
    await fileStore.uploadFiles(sessionId.value, files, { attachToPrompt: false })
  } catch {
    // The store exposes the upload error in this panel.
  }
}

async function onFilesDrop(event: DragEvent): Promise<void> {
  if (event.dataTransfer?.files.length) await uploadFiles(event.dataTransfer.files)
}

async function createMarkdown(): Promise<void> {
  if (!sessionId.value) return
  await fileStore.createMarkdownFile(sessionId.value, markdownPath.value, markdownContent.value)
  createOpen.value = false
  markdownPath.value = "docs/notes/new-note.md"
  markdownContent.value = "# New note\n"
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ")
}

function filesPaneMaximum(): number {
  const panelRect = workspacePanel.value?.getBoundingClientRect()
  const paneRect = filesPane.value?.getBoundingClientRect()
  const panelHeight = panelRect?.height || window.innerHeight
  const paneOffset = panelRect && paneRect ? Math.max(paneRect.top - panelRect.top, 0) : 0
  return Math.max(
    FILES_PANE_MIN,
    Math.min(FILES_PANE_MAX, panelHeight - paneOffset - PREVIEW_MIN - FILES_RESIZER_HEIGHT),
  )
}

function setFilesPaneHeight(nextHeight: number): void {
  filesPaneHeight.value = Math.min(Math.max(nextHeight, FILES_PANE_MIN), filesPaneMaximum())
}

function persistFilesPaneHeight(): void {
  try {
    window.localStorage.setItem(FILES_PANE_HEIGHT_KEY, String(filesPaneHeight.value))
  } catch {
    // Layout persistence is optional.
  }
}

function onFilesResizeMove(event: PointerEvent): void {
  if (!filesResizing.value) return
  setFilesPaneHeight(filesResizeStartHeight + event.clientY - filesResizeStartY)
}

function stopFilesResize(): void {
  if (!filesResizing.value) return
  filesResizing.value = false
  persistFilesPaneHeight()
  window.removeEventListener("pointermove", onFilesResizeMove)
  window.removeEventListener("pointerup", stopFilesResize)
  window.removeEventListener("pointercancel", stopFilesResize)
}

function beginFilesResize(event: PointerEvent): void {
  event.preventDefault()
  filesResizing.value = true
  filesResizeStartY = event.clientY
  filesResizeStartHeight = filesPane.value?.getBoundingClientRect().height || filesPaneHeight.value
  window.addEventListener("pointermove", onFilesResizeMove)
  window.addEventListener("pointerup", stopFilesResize)
  window.addEventListener("pointercancel", stopFilesResize)
}

function resizeFilesWithKeyboard(event: KeyboardEvent): void {
  if (!["ArrowUp", "ArrowDown"].includes(event.key)) return
  event.preventDefault()
  setFilesPaneHeight(filesPaneHeight.value + (event.key === "ArrowDown" ? 16 : -16))
  persistFilesPaneHeight()
}

function resetFilesPaneHeight(): void {
  setFilesPaneHeight(260)
  persistFilesPaneHeight()
}

function normalizeFilesPaneHeight(): void {
  setFilesPaneHeight(filesPaneHeight.value)
}

onMounted(() => {
  normalizeFilesPaneHeight()
  window.addEventListener("resize", normalizeFilesPaneHeight)
})
onBeforeUnmount(() => {
  stopFilesResize()
  window.removeEventListener("resize", normalizeFilesPaneHeight)
})
</script>

<template>
  <section
    ref="workspacePanel"
    class="workspace-panel"
    :class="{ 'resizing-files': filesResizing }"
    data-testid="workspace-panel"
  >
    <header class="workspace-header">
      <div>
        <span class="workspace-eyebrow">Agent deliverables</span>
        <strong>Workspace</strong>
      </div>
      <div class="workspace-state-badges">
        <span
          :class="['code-continuity', `status-${codeContinuity?.status ?? 'not_initialized'}`]"
          data-testid="code-continuity-status"
          :title="codeContinuity?.error_code || undefined"
        >
          {{ codeContinuityLabel }}
        </span>
        <span class="workspace-revision" data-testid="workspace-revision">
          r{{ workspace?.revision ?? 0 }}
        </span>
      </div>
    </header>

    <nav class="workspace-tabs" aria-label="Workspace views">
      <button
        v-for="tab in ['files', 'extensions', 'analysis', 'sandbox', 'changes'] as WorkspaceTab[]"
        :key="tab"
        type="button"
        :class="{ active: activeTab === tab }"
        :data-testid="`workspace-tab-${tab}`"
        @click="activeTab = tab"
      >
        {{ tab }}
      </button>
    </nav>

    <template v-if="activeTab === 'files'">
      <div
        ref="filesPane"
        class="workspace-files-pane"
        :style="{ height: `${filesPaneHeight}px` }"
        data-testid="workspace-files-pane"
        @dragover.prevent
        @drop.prevent="onFilesDrop"
      >
        <div class="workspace-toolbar">
          <button type="button" :disabled="fileStore.loading" @click="refresh">
            {{ fileStore.loading ? "Refreshing…" : "Refresh" }}
          </button>
          <button type="button" @click="createOpen = !createOpen">New Markdown</button>
          <button type="button" :disabled="fileStore.uploading" @click="openUpload">
            {{ fileStore.uploading ? "Uploading…" : "Upload" }}
          </button>
          <input
            ref="fileInput"
            class="visually-hidden"
            type="file"
            multiple
            data-testid="workspace-upload-input"
            @change="uploadSelected"
          />
        </div>

        <p class="upload-hint">Drop files here to save originals in upload/.</p>
        <p v-if="fileStore.error" class="upload-error" role="alert">{{ fileStore.error }}</p>

        <form v-if="createOpen" class="create-markdown" @submit.prevent="createMarkdown">
          <label>
            Path
            <input v-model="markdownPath" type="text" aria-label="Markdown logical path" />
          </label>
          <label>
            Initial content
            <textarea v-model="markdownContent" aria-label="Initial Markdown content" />
          </label>
          <div>
            <button type="button" @click="createOpen = false">Cancel</button>
            <button type="submit" class="primary">Create</button>
          </div>
        </form>

        <div v-if="latestArtifact" class="artifact-notice" data-testid="workspace-latest-artifact">
          <span>Latest Agent result</span>
          <button type="button" @click="selectFile(latestArtifact.fileId)">
            {{ latestArtifact.logicalPath || "Open result" }}
          </button>
        </div>

        <div
          v-if="pendingSandboxFiles.length"
          class="pending-sandbox-notice"
          data-testid="workspace-pending-sandbox"
        >
          <span>
            {{ pendingSandboxFiles.length }}
            {{
              operation?.status === "publish_conflict"
                ? "frozen file(s) retained after conflict"
                : "file(s) pending approval"
            }}
          </span>
          <button type="button" @click="activeTab = 'changes'">Review changes</button>
        </div>

        <div class="workspace-files" data-testid="session-folder">
          <ul v-if="tree.length" class="workspace-tree" role="tree" aria-label="Workspace files">
            <FileTreeNode
              v-for="node in tree"
              :key="node.kind === 'file' ? node.file?.id : node.path"
              :node="node"
              :session-id="sessionId || ''"
              :selected-file-id="selectedFileId"
              :latest-artifact-id="latestArtifact?.fileId"
              @select-file="selectFile"
              @delete="deleteFile"
            />
          </ul>
          <div v-else class="workspace-empty">Initializing Workspace…</div>
        </div>
      </div>

      <div
        class="workspace-row-resizer"
        role="separator"
        aria-label="Resize Workspace file list and preview"
        aria-orientation="horizontal"
        :aria-valuemin="FILES_PANE_MIN"
        :aria-valuemax="Math.round(filesPaneMaximum())"
        :aria-valuenow="Math.round(filesPaneHeight)"
        tabindex="0"
        data-testid="workspace-row-resizer"
        title="Drag to resize; double-click to reset"
        @pointerdown="beginFilesResize"
        @keydown="resizeFilesWithKeyboard"
        @dblclick="resetFilesPaneHeight"
      ></div>

      <WorkspaceFilePreview
        v-if="selectedFile && sessionId"
        :file="selectedFile"
        :session-id="sessionId"
        :load-content="loadContent"
        :save-content="saveContent"
        :download-file="selectedFile.origin === 'sandbox' ? downloadSelectedSandboxFile : undefined"
      />
      <div v-else class="workspace-placeholder">
        <span>⌘</span>
        <strong>Agent results appear here</strong>
        <p>Select a file, or ask the Agent to create Markdown or code.</p>
      </div>
    </template>

    <WorkspaceExtensions v-else-if="activeTab === 'extensions'" />

    <DataAnalysisPanel v-else-if="activeTab === 'analysis'" />

    <div v-else-if="activeTab === 'sandbox'" class="workspace-tab-body">
      <div v-if="!sandboxStore.available" class="workspace-empty">Managed Sandbox unavailable.</div>
      <div v-else-if="!operation" class="workspace-empty">
        <p>No Sandbox operation for this Session.</p>
        <button type="button" @click="sandboxModalOpen = true">Open Sandbox</button>
      </div>
      <template v-else>
        <div class="sandbox-summary">
          <span :class="['sandbox-status', `status-${operation.status}`]">
            {{ statusLabel(operation.status) }}
          </span>
          <span>revision {{ operation.workspace_revision }}</span>
        </div>
        <div v-if="operation.validation" class="sandbox-card">
          <strong>Validation {{ operation.validation.passed ? "passed" : "failed" }}</strong>
          <span>{{ operation.validation.duration_ms }} ms</span>
        </div>
        <ol v-if="sandboxStore.events.length" class="sandbox-events">
          <li v-for="event in sandboxStore.events.slice(-8)" :key="event.sequence">
            <span>{{ event.event_type }}</span>
          </li>
        </ol>
        <button type="button" class="full-sandbox" @click="sandboxModalOpen = true">
          Open Sandbox controls
        </button>
      </template>
    </div>

    <div v-else class="workspace-tab-body">
      <div v-if="!operation?.diff" class="workspace-empty">
        <p>No Sandbox changes captured.</p>
        <button type="button" :disabled="!operation" @click="sandboxStore.refreshDiff">
          Refresh changes
        </button>
      </div>
      <template v-else-if="operation.diff.entries.length">
        <ul class="change-list">
          <li v-for="entry in operation.diff.entries" :key="entry.path">
            <span :class="`change-${entry.status}`">{{ entry.status }}</span>
            <code>{{ entry.path }}</code>
          </li>
        </ul>
        <pre v-if="operation.diff.patch" class="change-patch">{{ operation.diff.patch }}</pre>
        <button type="button" class="full-sandbox" @click="sandboxModalOpen = true">
          Review and publish
        </button>
      </template>
      <div v-else class="workspace-empty">No Workspace changes.</div>
    </div>

    <CodingSandboxModal :open="sandboxModalOpen" @close="sandboxModalOpen = false" />
  </section>
</template>

<style scoped>
.upload-hint,
.upload-error {
  margin: 4px 12px 8px;
  color: var(--muted);
  font-size: 11px;
}
.upload-error {
  color: var(--danger);
}
.workspace-panel {
  display: flex;
  min-width: 0;
  height: 100%;
  flex-direction: column;
  overflow: hidden;
  background: #fbfbfc;
}
.workspace-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 14px 10px;
}
.workspace-header > div {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}
.workspace-state-badges {
  align-items: flex-end;
  gap: 4px;
}
.code-continuity {
  color: var(--muted);
  font-size: 9px;
}
.code-continuity.status-current {
  color: #15803d;
}
.code-continuity.status-stale,
.code-continuity.status-failed {
  color: #b45309;
}
.workspace-header strong {
  font-size: 15px;
}
.workspace-eyebrow {
  color: var(--accent);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.workspace-revision {
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--muted);
  font:
    10px ui-monospace,
    SFMono-Regular,
    Menlo,
    Consolas,
    monospace;
}
.workspace-tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  padding: 0 10px;
  border-bottom: 1px solid var(--border);
}
.workspace-tabs button {
  padding: 7px 4px;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.workspace-tabs button.active {
  border-bottom-color: var(--accent);
  color: var(--fg);
}
.workspace-files-pane {
  min-height: 0;
  flex: 0 0 auto;
  overflow: auto;
}
.workspace-toolbar {
  display: flex;
  gap: 5px;
  padding: 9px 10px;
}
.workspace-toolbar button {
  flex: 1;
  padding: 4px 5px;
  font-size: 10px;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  clip-path: inset(50%);
}
.create-markdown {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin: 0 10px 9px;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: white;
}
.create-markdown label {
  color: var(--muted);
  font-size: 10px;
}
.create-markdown input,
.create-markdown textarea {
  margin-top: 3px;
  padding: 6px 7px;
  font-size: 11px;
}
.create-markdown textarea {
  min-height: 82px;
  resize: vertical;
}
.create-markdown > div {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}
.create-markdown button {
  padding: 4px 9px;
  font-size: 10px;
}
.artifact-notice {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 10px 8px;
  padding: 7px 9px;
  border: 1px solid #bfdbfe;
  border-radius: 7px;
  background: #eff6ff;
  color: #1e40af;
  font-size: 10px;
}
.pending-sandbox-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 7px;
  margin: 0 10px 8px;
  padding: 7px 9px;
  border: 1px solid #fde68a;
  border-radius: 7px;
  background: #fffbeb;
  color: #92400e;
  font-size: 10px;
}
.pending-sandbox-notice button {
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: inherit;
  font-weight: 700;
}
.artifact-notice span {
  flex: 0 0 auto;
  font-weight: 700;
}
.artifact-notice button {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.workspace-files {
  overflow: visible;
  padding: 0 9px 9px;
}
.workspace-row-resizer {
  position: relative;
  z-index: 3;
  height: 7px;
  flex: 0 0 7px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: #f8fafc;
  cursor: row-resize;
  touch-action: none;
  transition: background 120ms ease;
}
.workspace-row-resizer::after {
  position: absolute;
  top: 2px;
  left: 50%;
  width: 28px;
  height: 2px;
  border-radius: 999px;
  background: #94a3b8;
  content: "";
  transform: translateX(-50%);
}
.workspace-row-resizer:hover,
.workspace-row-resizer:focus-visible,
.workspace-panel.resizing-files .workspace-row-resizer {
  background: #dbeafe;
  outline: none;
}
.workspace-panel.resizing-files {
  cursor: row-resize;
  user-select: none;
}
.workspace-tree {
  margin: 0;
  padding: 0;
}
.workspace-empty,
.workspace-placeholder {
  display: grid;
  place-items: center;
  padding: 24px 16px;
  color: var(--muted);
  text-align: center;
  font-size: 11px;
}
.workspace-placeholder {
  min-height: 0;
  flex: 1;
  align-content: center;
}
.workspace-placeholder > span {
  font-size: 20px;
}
.workspace-placeholder strong {
  color: var(--fg);
}
.workspace-placeholder p,
.workspace-empty p {
  margin: 4px 0 8px;
}
.workspace-tab-body {
  min-height: 0;
  flex: 1;
  overflow: auto;
  padding: 12px;
}
.sandbox-summary,
.sandbox-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 9px;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: white;
  color: var(--muted);
  font-size: 10px;
}
.sandbox-status {
  color: var(--fg);
  font-weight: 700;
  text-transform: capitalize;
}
.sandbox-events,
.change-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.sandbox-events li,
.change-list li {
  display: flex;
  gap: 7px;
  padding: 5px 2px;
  border-bottom: 1px solid var(--border);
  color: var(--muted);
  font-size: 10px;
}
.change-list code {
  min-width: 0;
  overflow: hidden;
  background: transparent;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.change-added {
  color: #15803d;
}
.change-modified {
  color: #a16207;
}
.change-deleted {
  color: #b91c1c;
}
.change-patch {
  max-height: 320px;
  margin-top: 10px;
  border: 1px solid var(--border);
  font-size: 9px;
  white-space: pre;
}
.full-sandbox {
  width: 100%;
  margin-top: 12px;
  font-size: 11px;
}
</style>
