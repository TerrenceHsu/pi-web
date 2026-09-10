import { defineStore } from "pinia"
import { ref } from "vue"

export interface FileDraft {
  content: string
  baseline: string
  sha: string
  editing: boolean
}
export interface DeskTab {
  id: string
  kind: "file" | "browser"
  resourceId: string
  title: string
}
export interface DeskState {
  tabs: DeskTab[]
  primary: string | null
  secondary: string | null
  split: "none" | "columns" | "rows"
  focused: "primary" | "secondary"
  drafts: Record<string, FileDraft>
}

export const useWorkspaceDeskStore = defineStore("workspaceDesk", () => {
  const sessions = ref<Record<string, DeskState>>({})
  function desk(sid: string): DeskState {
    return (sessions.value[sid] ??= {
      tabs: [],
      primary: null,
      secondary: null,
      split: "none",
      focused: "primary",
      drafts: {},
    })
  }
  function activate(sid: string, id: string) {
    const state = desk(sid)
    if (!state.tabs.some((tab) => tab.id === id)) return
    // A tab has only one mounted view; selecting it focuses its existing pane.
    if (state.primary === id) state.focused = "primary"
    else if (state.secondary === id && state.split !== "none") state.focused = "secondary"
    else state[state.focused] = id
  }
  function open(sid: string, tab: DeskTab) {
    const state = desk(sid)
    const existing = state.tabs.find((item) => item.id === tab.id)
    if (existing) Object.assign(existing, tab)
    else {
      if (state.tabs.length >= 16) throw new Error("Close a tab before opening more (limit 16).")
      state.tabs.push(tab)
    }
    activate(sid, tab.id)
  }
  function close(sid: string, id: string) {
    const state = desk(sid)
    const index = state.tabs.findIndex((item) => item.id === id)
    if (index < 0) return
    const tab = state.tabs.find((item) => item.id === id)
    state.tabs = state.tabs.filter((item) => item.id !== id)
    const neighbors = [...state.tabs.slice(index), ...state.tabs.slice(0, index).reverse()]
    if (tab?.kind === "file") delete state.drafts[tab.resourceId]
    if (state.primary === id)
      state.primary = neighbors.find((item) => item.id !== state.secondary)?.id ?? null
    if (state.secondary === id)
      state.secondary = neighbors.find((item) => item.id !== state.primary)?.id ?? null
    if (!state.primary && state.secondary) {
      state.primary = state.secondary
      state.secondary = null
    }
    if (!state.secondary) {
      state.split = "none"
      state.focused = "primary"
    }
  }
  function setSplit(sid: string, split: DeskState["split"]) {
    const state = desk(sid)
    state.split = split
    state.focused = "primary"
    if (split === "none") state.secondary = null
    else state.secondary ??= state.tabs.find((tab) => tab.id !== state.primary)?.id ?? null
  }
  function setDraft(sid: string, id: string, draft: FileDraft) {
    if (sessions.value[sid]?.tabs.some((tab) => tab.kind === "file" && tab.resourceId === id)) {
      sessions.value[sid].drafts[id] = draft
    }
  }
  function hasDrafts(): boolean {
    return Object.values(sessions.value).some((state) =>
      Object.values(state.drafts).some((draft) => draft.content !== draft.baseline),
    )
  }
  function forgetSession(sid: string) {
    delete sessions.value[sid]
  }
  function resetWorkspace() {
    sessions.value = {}
  }
  return {
    sessions,
    desk,
    open,
    activate,
    close,
    setSplit,
    setDraft,
    hasDrafts,
    forgetSession,
    resetWorkspace,
  }
})
