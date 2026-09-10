import { createPinia, setActivePinia } from "pinia"
import { flushPromises, shallowMount } from "@vue/test-utils"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { sessionsApi } = vi.hoisted(() => ({
  sessionsApi: {
    listSessions: vi.fn(), createSession: vi.fn(), renameSession: vi.fn(),
    deleteSession: vi.fn(), exportMarkdown: vi.fn(),
  },
}))

vi.mock("../../src/api/sessions", () => sessionsApi)

import { ApiError } from "../../src/api/client"
import * as filesApi from "../../src/api/files"
import * as contextApi from "../../src/api/contextBudget"
import * as providersApi from "../../src/api/providers"
import App from "../../src/App.vue"
import SessionSidebar from "../../src/components/layout/SessionSidebar.vue"
import { useAuthStore } from "../../src/stores/authStore"
import { useChatStore } from "../../src/stores/chatStore"
import { useCodingSandboxStore } from "../../src/stores/codingSandboxStore"
import { useContextBudgetStore } from "../../src/stores/contextBudgetStore"
import { useFileStore } from "../../src/stores/fileStore"
import { useMcpStore } from "../../src/stores/mcpStore"
import { useProviderStore } from "../../src/stores/providerStore"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useSkillStore } from "../../src/stores/skillStore"
import { useWorkspaceExtensionStore } from "../../src/stores/workspaceExtensionStore"
import type { ContextBudgetResponse, ContextCompactionStatus, FileListResponse } from "../../src/types"

const makeSession = (id: string) => ({
  id, title: id, created_at: 1, updated_at: 1, is_current: false,
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function seed(ids = ["sess-A", "sess-B"]) {
  const store = useSessionStore()
  store.sessions = ids.map(makeSession)
  store.activeSessionId = ids[0] ?? null
  return store
}

function files(sessionId: string): FileListResponse {
  return {
    count: 1,
    files: [{ id: `file-${sessionId}`, name: "fixture.txt", format: "text", mime: "text/plain",
      sha256: "a".repeat(64), size: 1, session_id: sessionId }],
    workspace: { schema_version: 1, session_id: sessionId, revision: 1, created_at: 1, updated_at: 1 },
  }
}

function compaction(): ContextCompactionStatus {
  return { auto_compact: true, status: "committed", active_projection_id: "ctx-1",
    covered_message_count: 2, token_stats: {}, error_code: null, can_auto_compact: true,
    circuit_open: false, summary: "Private fixture summary" }
}

function budget(sessionId: string): ContextBudgetResponse {
  return {
    session_id: sessionId, provider_id: "fixture", model_id: "fixture", capability_source: "unknown",
    workspace_context: null, intent: null, compaction: compaction(),
    estimate: { system_prompt_tokens: 1, message_tokens: 1, tool_definition_tokens: 1,
      estimated_input_tokens: 3, reserved_output_tokens: 0, projected_tokens: 3,
      context_window: null, input_ratio: null, projected_ratio: null, level: "unknown",
      can_send: true, approximate: true, estimator_version: "fixture" },
  }
}

async function mountAppForDeletion() {
  const auth = useAuthStore()
  const chat = useChatStore()
  const sandbox = useCodingSandboxStore()
  const provider = useProviderStore()
  const fileStore = useFileStore()
  const context = useContextBudgetStore()
  sessionsApi.listSessions.mockResolvedValue({ sessions: [makeSession("sess-A"), makeSession("sess-B")] })
  vi.spyOn(auth, "restoreSession").mockImplementation(async () => {
    auth.user = { id: "fixture-user", name: "fixture", is_admin: false }
    auth.status = "authenticated"
  })
  vi.spyOn(chat, "connectEvents").mockImplementation(() => undefined)
  vi.spyOn(chat, "disconnectEvents").mockImplementation(() => undefined)
  vi.spyOn(chat, "loadMessages").mockResolvedValue(undefined)
  vi.spyOn(chat, "findActiveRequest").mockResolvedValue(null)
  vi.spyOn(sandbox, "connectEvents").mockImplementation(() => undefined)
  vi.spyOn(sandbox, "disconnectEvents").mockImplementation(() => undefined)
  vi.spyOn(sandbox, "restoreSession").mockResolvedValue(undefined)
  vi.spyOn(fileStore, "loadFiles").mockResolvedValue(undefined)
  vi.spyOn(context, "load").mockResolvedValue(null)
  vi.spyOn(useWorkspaceExtensionStore(), "load").mockRejectedValue(new Error("fixture unavailable"))
  vi.spyOn(useSkillStore(), "loadSkills").mockResolvedValue(undefined)
  vi.spyOn(useMcpStore(), "loadServers").mockResolvedValue(undefined)
  vi.spyOn(useMcpStore(), "loadTools").mockResolvedValue(undefined)
  vi.spyOn(provider, "initialize").mockResolvedValue(undefined)
  const bindings = vi.spyOn(providersApi, "getSessionModelBinding").mockResolvedValue({ binding: null })
  window.history.replaceState({}, "", "/chat/sess-A")
  const wrapper = shallowMount(App)
  await flushPromises()
  return { wrapper, chat, provider, fileStore, context, bindings, sessions: useSessionStore() }
}

describe("session deletion", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    sessionsApi.deleteSession.mockResolvedValue({ ok: true, deleted_files: 0 })
    sessionsApi.createSession.mockResolvedValue(makeSession("sess-new"))
    vi.spyOn(window, "confirm").mockReturnValue(true)
  })

  afterEach(() => { vi.restoreAllMocks() })

  it("removes the active session only after server success and selects a remaining session", async () => {
    const store = seed()
    const pending = deferred<{ ok: boolean; deleted_files: number }>()
    sessionsApi.deleteSession.mockReturnValue(pending.promise)
    const deletion = store.deleteSession("sess-A")
    expect(store.sessions.map((session) => session.id)).toEqual(["sess-A", "sess-B"])
    expect(store.activeSessionId).toBe("sess-A")
    pending.resolve({ ok: true, deleted_files: 1 })
    await deletion
    expect(store.sessions.map((session) => session.id)).toEqual(["sess-B"])
    expect(store.activeSessionId).toBe("sess-B")
  })

  it("does not change the active session when deleting a different one", async () => {
    const store = seed()
    await store.deleteSession("sess-B")
    expect(store.activeSessionId).toBe("sess-A")
    expect(store.sessions.map((session) => session.id)).toEqual(["sess-A"])
  })

  it("ignores repeated deletes and exposes pending state", async () => {
    const store = seed()
    const pending = deferred<{ ok: boolean; deleted_files: number }>()
    sessionsApi.deleteSession.mockReturnValue(pending.promise)
    const first = store.deleteSession("sess-A")
    const second = store.deleteSession("sess-A")
    expect(sessionsApi.deleteSession).toHaveBeenCalledTimes(1)
    expect(store.deletingSessionIds).toEqual(["sess-A"])
    pending.resolve({ ok: true, deleted_files: 0 })
    await Promise.all([first, second])
    expect(store.deletingSessionIds).toEqual([])
  })

  it("does not remove a session when a success-status response reports failure", async () => {
    const store = seed()
    sessionsApi.deleteSession.mockResolvedValue({ ok: false, deleted_files: 0 })
    await expect(store.deleteSession("sess-A")).rejects.toThrow()
    expect(store.sessions).toHaveLength(2)
    expect(store.activeSessionId).toBe("sess-A")
    expect(store.error).toBe("Session could not be deleted. Please try again.")
  })

  it("does not resurrect a deleted session from an older list response", async () => {
    const store = seed()
    const pending = deferred<{ sessions: ReturnType<typeof makeSession>[] }>()
    sessionsApi.listSessions.mockReturnValue(pending.promise)
    const loading = store.loadSessions("sess-A")
    await store.deleteSession("sess-A")
    pending.resolve({ sessions: [makeSession("sess-A"), makeSession("sess-B")] })
    await loading
    expect(store.sessions.map((session) => session.id)).toEqual(["sess-B"])
    expect(store.activeSessionId).toBe("sess-B")
    expect(store.loading).toBe(false)
  })

  it("does not apply an old account deletion to a new account's same-named session", async () => {
    const store = seed()
    const pending = deferred<{ ok: boolean; deleted_files: number }>()
    sessionsApi.deleteSession.mockReturnValue(pending.promise)
    const deletion = store.deleteSession("sess-A")
    store.resetWorkspace()
    store.sessions = [makeSession("sess-A")]
    store.activeSessionId = "sess-A"
    pending.resolve({ ok: true, deleted_files: 0 })
    await deletion
    expect(store.sessions.map((session) => session.id)).toEqual(["sess-A"])
    expect(store.activeSessionId).toBe("sess-A")
    expect(store.deletingSessionIds).toEqual([])
  })

  it("does not expose an old account's deletion failure after workspace reset", async () => {
    const store = seed()
    const pending = deferred<never>()
    sessionsApi.deleteSession.mockReturnValue(pending.promise)
    const deletion = store.deleteSession("sess-A")
    store.resetWorkspace()
    pending.reject(new Error("private-error-marker"))
    await expect(deletion).rejects.toThrow()
    expect(store.error).toBeNull()
    expect(store.deletingSessionIds).toEqual([])
  })

  it("respects cancel without a server request", async () => {
    seed()
    vi.mocked(window.confirm).mockReturnValue(false)
    const wrapper = shallowMount(SessionSidebar)
    await wrapper.findAll('[data-testid="session-delete-btn"]')[0]!.trigger("click")
    expect(sessionsApi.deleteSession).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it("disables the pending X and leaves other sessions usable", async () => {
    seed()
    const pending = deferred<{ ok: boolean; deleted_files: number }>()
    sessionsApi.deleteSession.mockReturnValue(pending.promise)
    const wrapper = shallowMount(SessionSidebar)
    const buttons = wrapper.findAll('[data-testid="session-delete-btn"]')
    await buttons[0]!.trigger("click")
    expect((buttons[0]!.element as HTMLButtonElement).disabled).toBe(true)
    expect((buttons[1]!.element as HTMLButtonElement).disabled).toBe(false)
    await buttons[0]!.trigger("click")
    expect(sessionsApi.deleteSession).toHaveBeenCalledTimes(1)
    pending.resolve({ ok: true, deleted_files: 0 })
    await flushPromises()
    expect(wrapper.findAll('[data-testid="session-item"]')).toHaveLength(1)
    wrapper.unmount()
  })

  it("shows a safe visible error and keeps the row on failure", async () => {
    const store = seed()
    sessionsApi.deleteSession.mockRejectedValue(new Error("private-error-marker"))
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined)
    const wrapper = shallowMount(SessionSidebar)
    await wrapper.findAll('[data-testid="session-delete-btn"]')[0]!.trigger("click")
    await flushPromises()
    const alert = wrapper.get('[data-testid="session-error"]')
    expect(alert.attributes("role")).toBe("alert")
    expect(alert.text()).toBe("Session could not be deleted. Please try again.")
    expect(wrapper.text()).not.toContain("private-error-marker")
    expect(consoleError).not.toHaveBeenCalled()
    expect(store.sessions).toHaveLength(2)
    wrapper.unmount()
  })

  it("explains a backend deletion conflict without rendering its raw detail", async () => {
    seed()
    sessionsApi.deleteSession.mockRejectedValue(new ApiError(409, "private-error-marker", null))
    const wrapper = shallowMount(SessionSidebar)
    await wrapper.findAll('[data-testid="session-delete-btn"]')[0]!.trigger("click")
    await flushPromises()
    expect(wrapper.get('[data-testid="session-error"]').text()).toContain("still busy")
    expect(wrapper.text()).not.toContain("private-error-marker")
    wrapper.unmount()
  })

  it("creates a fresh session when the last session was successfully deleted", async () => {
    const store = seed(["sess-A"])
    const wrapper = shallowMount(SessionSidebar)
    await wrapper.get('[data-testid="session-delete-btn"]').trigger("click")
    await flushPromises()
    expect(sessionsApi.deleteSession).toHaveBeenCalledWith("sess-A")
    expect(sessionsApi.createSession).toHaveBeenCalledTimes(1)
    expect(store.sessions.map((session) => session.id)).toEqual(["sess-new"])
    expect(store.activeSessionId).toBe("sess-new")
    wrapper.unmount()
  })

  it("shows replacement-session creation failure without restoring the deleted row", async () => {
    const store = seed(["sess-A"])
    sessionsApi.createSession.mockRejectedValue(new Error("private-error-marker"))
    const wrapper = shallowMount(SessionSidebar)
    await wrapper.get('[data-testid="session-delete-btn"]').trigger("click")
    await flushPromises()
    expect(store.sessions).toEqual([])
    expect(store.activeSessionId).toBeNull()
    expect(wrapper.get('[data-testid="session-error"]').text()).toBe("A new session could not be created.")
    expect(wrapper.text()).not.toContain("private-error-marker")
    wrapper.unmount()
  })

  it("forgets only deleted-session files and ignores a late list response", async () => {
    const store = useFileStore()
    const pending = deferred<FileListResponse>()
    const list = vi.spyOn(filesApi, "listFiles").mockReturnValue(pending.promise)
    for (const id of ["sess-A", "sess-B"]) {
      store.filesBySession[id] = files(id).files
      store.workspaceBySession[id] = files(id).workspace
      store.selectedFileIdBySession[id] = `file-${id}`
      store.latestArtifactBySession[id] = { fileId: `file-${id}`, unseen: true }
    }
    store.pendingAttachments = [...files("sess-A").files, ...files("sess-B").files]
    const loading = store.loadFiles("sess-A")
    store.forgetSession("sess-A")
    pending.resolve(files("sess-A"))
    expect(await loading).toBeUndefined()
    expect(store.filesBySession).toEqual({ "sess-B": files("sess-B").files })
    expect(store.workspaceBySession).toEqual({ "sess-B": files("sess-B").workspace })
    expect(store.selectedFileIdBySession).toEqual({ "sess-B": "file-sess-B" })
    expect(Object.keys(store.latestArtifactBySession)).toEqual(["sess-B"])
    expect(store.pendingAttachments).toEqual(files("sess-B").files)
    store.selectFile("sess-A", "file-sess-A")
    await store.revealAgentArtifact("sess-A", { fileId: "file-sess-A" })
    await store.loadFiles("sess-A")
    expect(list).toHaveBeenCalledTimes(1)
    expect(store.selectedFileIdBySession["sess-A"]).toBeUndefined()
  })

  it("does not repopulate files or attachments from a late upload", async () => {
    const store = useFileStore()
    const pending = deferred<FileListResponse>()
    vi.spyOn(filesApi, "uploadFiles").mockReturnValue(pending.promise)
    const list = vi.spyOn(filesApi, "listFiles")
    const upload = store.uploadFiles("sess-A", [new File(["fixture"], "fixture.txt")])
    store.forgetSession("sess-A")
    pending.resolve(files("sess-A"))
    await upload
    expect(store.filesBySession).toEqual({})
    expect(store.workspaceBySession).toEqual({})
    expect(store.pendingAttachments).toEqual([])
    expect(list).not.toHaveBeenCalled()
  })

  it("forgets budget summaries and rejects late requests and events for the deleted session", async () => {
    const store = useContextBudgetStore()
    const pendingBudget = deferred<ContextBudgetResponse>()
    const pendingStatus = deferred<ContextCompactionStatus>()
    const get = vi.spyOn(contextApi, "getContextBudget").mockReturnValue(pendingBudget.promise)
    vi.spyOn(contextApi, "getContextCompaction").mockReturnValue(pendingStatus.promise)
    vi.spyOn(contextApi, "estimateContextBudget").mockReturnValue(pendingBudget.promise)
    store.budgetsBySession = { "sess-A": budget("sess-A"), "sess-B": budget("sess-B") }
    store.compactionsBySession = { "sess-A": compaction(), "sess-B": compaction() }
    const loading = store.load("sess-A")
    const preview = store.preview("sess-A", { prompt: "fixture" })
    const summary = store.loadCompaction("sess-A")
    store.forgetSession("sess-A")
    pendingBudget.resolve(budget("sess-A"))
    pendingStatus.resolve(compaction())
    expect(await loading).toBeNull()
    expect(await preview).toBeNull()
    expect(await summary).toBeNull()
    store.applyEvent("sess-A", { estimate: budget("sess-A").estimate, compaction: compaction() })
    await store.load("sess-A")
    expect(get).toHaveBeenCalledTimes(1)
    expect(store.budgetsBySession).toEqual({ "sess-B": budget("sess-B") })
    expect(store.compactionsBySession).toEqual({ "sess-B": compaction() })
    expect(store.loading).toBe(false)
  })

  it("coordinates active deletion through App: replace URL, reset chat and refresh binding", async () => {
    const { wrapper, sessions, provider, bindings, chat, fileStore, context } = await mountAppForDeletion()
    const replace = vi.spyOn(window.history, "replaceState")
    const push = vi.spyOn(window.history, "pushState")
    fileStore.filesBySession["sess-A"] = files("sess-A").files
    context.budgetsBySession["sess-A"] = budget("sess-A")
    provider.bindingSessionId = "sess-A"
    provider.currentBinding = { session_id: "sess-A", profile_id: "fixture-profile", model_id: "old",
      source: "explicit", created_at: 1, updated_at: 1 }
    const pending = deferred<Awaited<ReturnType<typeof providersApi.getSessionModelBinding>>>()
    bindings.mockReturnValueOnce(pending.promise)
    const oldBinding = provider.currentBinding
    const staleRefresh = provider.refreshForSession("sess-A")
    bindings.mockClear()
    await sessions.deleteSession("sess-A")
    await flushPromises()
    expect(window.location.pathname).toBe("/chat/sess-B")
    expect(replace).toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
    expect(chat.activeSessionId).toBe("sess-B")
    expect(bindings).toHaveBeenCalledWith("sess-B")
    expect(provider.bindingSessionId).toBe("sess-B")
    expect(provider.currentBinding).toBeNull()
    pending.resolve({ binding: oldBinding })
    await staleRefresh
    expect(provider.bindingSessionId).toBe("sess-B")
    expect(provider.currentBinding).toBeNull()
    expect(fileStore.filesBySession["sess-A"]).toBeUndefined()
    expect(context.budgetsBySession["sess-A"]).toBeUndefined()
    wrapper.unmount()
  })

  it("coordinates inactive deletion without changing the current binding or URL", async () => {
    const { wrapper, sessions, provider, bindings, fileStore, context } = await mountAppForDeletion()
    fileStore.filesBySession["sess-A"] = files("sess-A").files
    fileStore.filesBySession["sess-B"] = files("sess-B").files
    context.budgetsBySession = { "sess-A": budget("sess-A"), "sess-B": budget("sess-B") }
    bindings.mockClear()
    await sessions.deleteSession("sess-B")
    await flushPromises()
    expect(window.location.pathname).toBe("/chat/sess-A")
    expect(bindings).not.toHaveBeenCalled()
    expect(provider.bindingSessionId).toBe("sess-A")
    expect(fileStore.filesBySession).toEqual({ "sess-A": files("sess-A").files })
    expect(context.budgetsBySession).toEqual({ "sess-A": budget("sess-A") })
    wrapper.unmount()
  })
})
