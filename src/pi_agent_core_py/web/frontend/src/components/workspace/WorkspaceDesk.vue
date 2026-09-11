<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import type { FileRef } from "../../types"
import { useWorkspaceDeskStore, type DeskTab } from "../../stores/workspaceDeskStore"
import WorkspaceFilePreview from "./WorkspaceFilePreview.vue"
import WorkspaceBrowser from "./WorkspaceBrowser.vue"
import * as browserApi from "../../api/browser"

const props = defineProps<{
  sessionId: string
  files: FileRef[]
  selectedFileId: string | null | undefined
  filesReady: boolean
  active?: boolean
  loadContent: (id: string) => Promise<string>
  saveContent: (id: string, text: string, sha: string) => Promise<FileRef>
  downloadFile?: (fileId: string) => Promise<void>
}>()
const emit = defineEmits<{ (event: "new-markdown"): void }>()
const store = useWorkspaceDeskStore()
const state = computed(() => store.desk(props.sessionId))
const error = ref("")
const creating = ref(false)
const closing = ref<string | null>(null)
let generation = 0
let browserRevision = 0
let poll: ReturnType<typeof setTimeout> | undefined
const fileOf = (tab: DeskTab) => props.files.find((file) => file.id === tab.resourceId)
function visible(id: string) {
  return (
    state.value.primary === id || (state.value.split !== "none" && state.value.secondary === id)
  )
}
// Keep the single account media slot on a visible browser. Focusing Markdown
// must not restart its neighboring browser; two browser panes follow focus.
const preferredMediaTab = computed(() => {
  const browsers = state.value.tabs.filter((tab) => tab.kind === "browser" && visible(tab.id))
  const focused = state.value[state.value.focused]
  return browsers.find((tab) => tab.id === focused)?.id ?? browsers[0]?.id ?? null
})
function select(id: string) {
  store.activate(props.sessionId, id)
}
watch(
  () => state.value[state.value.focused],
  async (id) => {
    await nextTick()
    if (id)
      document
        .getElementById(`desk-tab-${props.sessionId}-${id}`)
        ?.scrollIntoView?.({ block: "nearest", inline: "nearest" })
  },
)
function moveTab(event: KeyboardEvent, index: number) {
  const count = state.value.tabs.length
  let target: number
  switch (event.key) {
    case "ArrowRight":
      target = (index + 1) % count
      break
    case "ArrowLeft":
      target = (index - 1 + count) % count
      break
    case "Home":
      target = 0
      break
    case "End":
      target = count - 1
      break
    default:
      return
  }
  event.preventDefault()
  const list = (event.currentTarget as HTMLElement).closest('[role="tablist"]')
  select(state.value.tabs[target].id)
  void nextTick(() => {
    const button = list?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[target]
    button?.focus()
    button?.scrollIntoView?.({ block: "nearest", inline: "nearest" })
  })
}
watch(
  () => props.selectedFileId,
  (id) => {
    const file = props.files.find((item) => item.id === id)
    if (!file) return
    try {
      store.open(props.sessionId, {
        id: `file:${file.id}`,
        kind: "file",
        resourceId: file.id,
        title: file.name,
      })
    } catch (cause) {
      error.value = (cause as Error).message
    }
  },
  { immediate: true },
)
watch(
  () => props.files,
  () => {
    if (!props.filesReady) return
    for (const tab of [...state.value.tabs]) {
      if (tab.kind === "file" && !fileOf(tab)) store.close(props.sessionId, tab.id)
    }
  },
)
async function refreshBrowsers() {
  const version = generation
  const revision = browserRevision
  try {
    const result = await browserApi.list(props.sessionId)
    if (generation !== version || browserRevision !== revision) return
    for (const tab of [...state.value.tabs]) {
      if (tab.kind === "browser" && !result.pages.some((page) => page.id === tab.resourceId))
        store.close(props.sessionId, tab.id)
    }
    for (const page of result.pages) {
      const existing = state.value.tabs.find((tab) => tab.id === `browser:${page.id}`)
      if (existing) existing.title = page.title || "Browser"
      else if (state.value.tabs.length < 16)
        store.open(props.sessionId, {
          id: `browser:${page.id}`,
          resourceId: page.id,
          kind: "browser",
          title: page.title || "Browser",
        })
    }
  } catch {
    /* A browser component reports its own actionable connection error. */
  } finally {
    if (generation === version) poll = setTimeout(refreshBrowsers, 3000)
  }
}
async function newBrowser() {
  if (creating.value || state.value.tabs.length >= 16) return
  creating.value = true
  browserRevision++
  error.value = ""
  const version = generation
  try {
    const page = await browserApi.create(props.sessionId)
    if (generation !== version) return
    store.open(props.sessionId, {
      id: `browser:${page.id}`,
      kind: "browser",
      resourceId: page.id,
      title: "Browser",
    })
  } catch (cause) {
    if (generation === version) error.value = (cause as Error).message
  } finally {
    if (generation === version) {
      creating.value = false
      browserRevision++
    }
  }
}
async function closeTab(tab: DeskTab) {
  if (closing.value) return
  const draft = state.value.drafts[tab.resourceId]
  if (
    tab.kind === "file" &&
    draft &&
    draft.content !== draft.baseline &&
    !window.confirm(
      "Close this tab and discard its unsaved Markdown draft? The file will not be deleted.",
    )
  )
    return
  closing.value = tab.id
  browserRevision++
  error.value = ""
  const version = generation
  try {
    if (tab.kind === "browser") await browserApi.close(props.sessionId, tab.resourceId)
    if (generation === version) store.close(props.sessionId, tab.id)
  } catch (cause) {
    if (generation === version) error.value = (cause as Error).message
  } finally {
    if (generation === version) {
      closing.value = null
      browserRevision++
    }
  }
}
onMounted(() => {
  void refreshBrowsers()
})
onBeforeUnmount(() => {
  generation++
  clearTimeout(poll)
})
</script>

<template>
  <section class="workspace-desk" data-testid="workspace-desk">
    <div class="desk-toolbar">
      <button @click="emit('new-markdown')">New Markdown</button>
      <select
        aria-label="Preview layout"
        :value="state.split"
        @change="
          store.setSplit(
            sessionId,
            ($event.target as HTMLSelectElement).value as 'none' | 'columns' | 'rows',
          )
        "
      >
        <option value="none">Single pane</option>
        <option value="columns">Split left/right</option>
        <option value="rows">Split top/bottom</option>
      </select>
    </div>
    <p v-if="error" class="desk-error" role="alert">{{ error }}</p>
    <div class="desk-tab-bar">
      <div class="desk-tabs" role="tablist" aria-label="Open documents and browser pages">
        <div
          v-for="(tab, index) in state.tabs"
          :key="tab.id"
          class="desk-tab"
          :class="{ selected: state[state.focused] === tab.id, 'in-pane': visible(tab.id) }"
          @auxclick.middle.prevent="closeTab(tab)"
        >
          <button
            :id="`desk-tab-${sessionId}-${tab.id}`"
            role="tab"
            :aria-controls="`desk-view-${sessionId}-${tab.id}`"
            :aria-selected="state[state.focused] === tab.id"
            :tabindex="state[state.focused] === tab.id ? 0 : -1"
            :title="tab.title"
            :data-testid="`desk-tab-${tab.kind}`"
            @click="select(tab.id)"
            @keydown="moveTab($event, index)"
          >
            <span class="tab-icon" aria-hidden="true">{{
              tab.kind === "browser" ? "◎" : "▤"
            }}</span>
            <span class="tab-title">{{ tab.title }}</span>
            <span
              v-if="
                tab.kind === 'file' &&
                state.drafts[tab.resourceId]?.content !== state.drafts[tab.resourceId]?.baseline
              "
              class="tab-dirty"
              aria-label="Unsaved changes"
              >•</span
            >
          </button>
          <button
            class="tab-close"
            :disabled="closing !== null"
            :aria-label="`Close ${tab.title}`"
            :title="`Close ${tab.title}`"
            @click="closeTab(tab)"
          >
            ×
          </button>
        </div>
      </div>
      <button
        class="desk-new-tab"
        :disabled="creating || state.tabs.length >= 16"
        :aria-busy="creating"
        aria-label="New browser tab"
        :title="creating ? 'Starting browser…' : 'New browser tab'"
        data-testid="desk-new-browser"
        @click="newBrowser"
      >
        {{ creating ? "…" : "+" }}
      </button>
    </div>
    <div class="desk-panes" :class="state.split" data-testid="desk-panes">
      <section
        v-for="tab in state.tabs"
        v-show="visible(tab.id)"
        :id="`desk-view-${sessionId}-${tab.id}`"
        :key="tab.id"
        role="tabpanel"
        :aria-labelledby="`desk-tab-${sessionId}-${tab.id}`"
        class="desk-view"
        :class="{ secondary: state.secondary === tab.id, focused: state[state.focused] === tab.id }"
        @pointerdown.capture="state.focused = state.secondary === tab.id ? 'secondary' : 'primary'"
      >
        <WorkspaceFilePreview
          v-if="tab.kind === 'file' && fileOf(tab)"
          :file="fileOf(tab)!"
          :session-id="sessionId"
          :load-content="loadContent"
          :save-content="saveContent"
          :draft="state.drafts[tab.resourceId]"
          :download-file="
            fileOf(tab)?.origin === 'sandbox' && downloadFile
              ? () => downloadFile!(tab.resourceId)
              : undefined
          "
          @draft="store.setDraft(sessionId, tab.resourceId, $event)"
        />
        <WorkspaceBrowser
          v-else-if="tab.kind === 'browser'"
          :session-id="sessionId"
          :page-id="tab.resourceId"
          :active="active !== false && visible(tab.id)"
          :media-preferred="preferredMediaTab === tab.id"
        />
      </section>
      <div v-if="!state.primary" class="desk-placeholder">
        <strong>Agent results appear here</strong>
        <p>Select a file, create Markdown, or open a browser.</p>
      </div>
      <div
        v-if="state.split !== 'none' && !state.secondary"
        class="desk-placeholder secondary"
        @click="state.focused = 'secondary'"
      >
        Click here, then choose a different tab for this pane.
      </div>
    </div>
  </section>
</template>

<style scoped>
.workspace-desk {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 180px;
  min-width: 0;
  overflow: hidden;
}
.desk-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--border, #ddd);
  flex-wrap: wrap;
}
.desk-toolbar button,
.desk-toolbar select {
  background: white;
  border: 1px solid var(--border, #ddd);
  border-radius: 5px;
  padding: 5px 8px;
  font-size: 11px;
}
.desk-tab-bar {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  min-width: 0;
  padding: 5px 6px 0;
  background: #f3f4f6;
  border-bottom: 1px solid var(--border, #ddd);
  min-height: 39px;
}
.desk-tabs {
  display: flex;
  align-self: stretch;
  min-width: 0;
  overflow-x: auto;
  gap: 3px;
  scrollbar-width: thin;
}
.desk-tab {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  width: 170px;
  border: 1px solid transparent;
  border-bottom: 0;
  border-radius: 9px 9px 0 0;
  color: var(--muted, #737789);
}
.desk-tab:hover {
  background: #e8eaf0;
}
.desk-tab.selected {
  background: white;
  border-color: var(--border, #ddd);
  color: var(--text, #242836);
  box-shadow: inset 0 2px var(--accent, #356cf6);
}
.desk-tab.in-pane:not(.selected) {
  background: #e9edf7;
}
.desk-tab button {
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.desk-tab [role="tab"] {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 6px 9px 9px;
  flex: 1;
  min-width: 0;
  font-size: 12px;
}
.tab-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tab-icon,
.tab-dirty {
  flex-shrink: 0;
}
.tab-dirty {
  color: var(--accent, #356cf6);
}
.desk-tab .tab-close {
  width: 23px;
  height: 23px;
  flex-shrink: 0;
  padding: 0;
  margin-right: 5px;
  border-radius: 5px;
  font-size: 17px;
}
.desk-new-tab {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 7px;
  margin: 0 0 4px 5px;
  background: transparent;
  font-size: 23px;
  color: var(--text, #242836);
  cursor: pointer;
}
.desk-tab .tab-close:hover,
.desk-new-tab:hover {
  background: #dfe3ed;
}
.desk-tab button:focus-visible,
.desk-new-tab:focus-visible {
  outline: 2px solid var(--accent, #356cf6);
  outline-offset: -2px;
}
.desk-new-tab:disabled {
  cursor: wait;
  opacity: 0.5;
}
.desk-panes {
  display: grid;
  flex: 1;
  min-height: 0;
  min-width: 0;
  grid-template: minmax(0, 1fr) / minmax(0, 1fr);
  gap: 4px;
  background: var(--border, #ddd);
}
.desk-panes.columns {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.desk-panes.rows {
  grid-template-rows: repeat(2, minmax(0, 1fr));
}
.desk-view {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: #fbfbfc;
  grid-area: 1 / 1;
}
.columns .secondary {
  grid-area: 1 / 2;
}
.rows .secondary {
  grid-area: 2 / 1;
}
.desk-view.focused {
  box-shadow: inset 0 2px var(--accent);
}
.desk-placeholder {
  display: flex;
  flex-direction: column;
  justify-content: center;
  text-align: center;
  padding: 20px;
  font-size: 12px;
  color: var(--muted);
  background: #fbfbfc;
}
.desk-error {
  color: var(--danger);
  padding: 6px 10px;
  margin: 0;
  font-size: 12px;
}
</style>
