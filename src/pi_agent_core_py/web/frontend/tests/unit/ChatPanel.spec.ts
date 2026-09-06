// ChatPanel 集成测试——P1-E M2-3。
//
// 覆盖 spec §19 的 10 项测试：ProviderSelector 渲染、ChatInput 收到 providerReady、
// 各 binding 状态下 ChatInput 可发送性、header 结构保留、不调用真实网络。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

import type { SessionModelBindingView } from "../../src/types/providers"
import type { ContextBudgetResponse, ContextCompactionStatus } from "../../src/types"

// ----- Mock api -----
const { api, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number
    readonly detail: string
    readonly payload: unknown
    constructor(status: number, detail: string) {
      super(`${status} ${detail}`)
      this.name = "ApiError"
      this.status = status
      this.detail = detail
      this.payload = null
    }
  }
  return {
    api: {
      getProviderDefinitions: vi.fn(),
      listCredentials: vi.fn(),
      createCredential: vi.fn(),
      updateCredentialLabel: vi.fn(),
      rotateCredentialSecret: vi.fn(),
      deleteCredential: vi.fn(),
      listProviderProfiles: vi.fn(),
      createProviderProfile: vi.fn(),
      updateProviderProfile: vi.fn(),
      deleteProviderProfile: vi.fn(),
      getProviderProfileModels: vi.fn(),
      getSessionModelBinding: vi.fn(),
      putSessionModelBinding: vi.fn(),
    },
    FakeApiError,
  }
})

vi.mock("../../src/api/providers", () => api)
const contextApi = vi.hoisted(() => ({
  getContextBudget: vi.fn(),
  estimateContextBudget: vi.fn(),
  compactContext: vi.fn(),
  getContextCompaction: vi.fn(),
  setContextAutoCompaction: vi.fn(),
  getContextSource: vi.fn(),
}))
vi.mock("../../src/api/contextBudget", () => contextApi)
vi.mock("../../src/api/client", () => ({
  ApiError: FakeApiError,
  requestJson: vi.fn(),
}))

// mock abortRun——ChatPanel 用它做 stop
vi.mock("../../src/api/state", () => ({
  abortRun: vi.fn().mockResolvedValue(undefined),
  getState: vi.fn().mockResolvedValue({ plan_mode: { enabled: true } }),
}))
vi.mock("../../src/api/slashCommands", () => ({
  listSlashCommands: vi.fn().mockResolvedValue({
    count: 1,
    commands: [
      {
        name: "/checkpointer",
        description: "Save memory and clear this conversation.",
        requires_provider: true,
        accepts_arguments: false,
      },
    ],
  }),
}))

import ChatPanel from "../../src/components/chat/ChatPanel.vue"
import { getState } from "../../src/api/state"
import { useProviderStore } from "../../src/stores/providerStore"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useChatStore } from "../../src/stores/chatStore"
import { useContextBudgetStore } from "../../src/stores/contextBudgetStore"
import { useFileStore } from "../../src/stores/fileStore"

// ============================================================================
// Fixtures
// ============================================================================

function makeBinding(overrides: Partial<SessionModelBindingView> = {}): SessionModelBindingView {
  return {
    session_id: "sess-1",
    profile_id: "prof-1",
    model_id: "m",
    source: "explicit",
    created_at: 1,
    updated_at: 2,
    ...overrides,
  }
}

// ============================================================================
// Setup
// ============================================================================

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((fn) => fn.mockReset())
  Object.values(contextApi).forEach((fn) => fn.mockReset())
  vi.mocked(getState).mockResolvedValue({ plan_mode: { enabled: true } } as never)
})

async function flushAll() {
  await new Promise((r) => setTimeout(r, 0))
}

function setupStores(opts: {
  binding?: SessionModelBindingView | null
  state?: "idle" | "loading" | "loaded" | "error"
  profileStatus?: string
  activeSession?: string | null
}) {
  const sessionStore = useSessionStore()
  const chatStore = useChatStore()
  const providerStore = useProviderStore()

  sessionStore.activeSessionId = opts.activeSession ?? "sess-1"
  sessionStore.sessions = [
    {
      id: "sess-1",
      title: "S1",
      created_at: 0,
      updated_at: 0,
      is_current: true,
    } as any,
  ]
  chatStore.sending = false
  chatStore.streaming = false
  chatStore.currentRequestId = null

  const effectiveState = opts.state ?? "loaded"
  ;(providerStore as any).bindingLoadState = effectiveState
  if (effectiveState === "loaded") {
    ;(providerStore as any).bindingSessionId = "sess-1"
    ;(providerStore as any).currentBinding = opts.binding ?? null
  }
  if (opts.binding) {
    providerStore.profiles = [
      {
        id: opts.binding.profile_id,
        name: "P",
        provider_id: "glm",
        provider_display_name: "GLM",
        credential_id: "c",
        credential_masked_value: null,
        default_model: opts.binding.model_id,
        enabled: true,
        is_default: false,
        status: opts.profileStatus ?? "ready",
        created_at: 1,
        updated_at: 1,
      } as any,
    ]
  }
  return { sessionStore, chatStore, providerStore }
}

function mountPanel(mode: "default" | "knowledge" = "default") {
  return mount(ChatPanel, { props: { mode } })
}

function compactionStatus(overrides: Partial<ContextCompactionStatus> = {}): ContextCompactionStatus {
  return {
    auto_compact: true,
    status: "committed",
    active_projection_id: "ctx-1",
    covered_message_count: 4,
    token_stats: { estimated_input_tokens_before: 14000, estimated_input_tokens_after: 7000 },
    error_code: null,
    summary_text: "<script>alert('summary')</script>",
    source_entry_ids: ["entry-1"],
    ...overrides,
  }
}

function blockedBudget(compaction: ContextCompactionStatus | null): ContextBudgetResponse {
  return {
    session_id: "sess-1", provider_id: "glm", model_id: "m", capability_source: "user",
    workspace_context: null, intent: null, compaction,
    estimate: {
      system_prompt_tokens: 100, message_tokens: 1000, tool_definition_tokens: 100,
      estimated_input_tokens: 1200, reserved_output_tokens: 100, projected_tokens: 1300,
      context_window: 1000, input_ratio: 1.2, projected_ratio: 1.3,
      effective_input_budget: 700, effective_ratio: 1.7,
      level: "blocked", can_send: false, approximate: true, estimator_version: "test",
    },
  }
}

// ============================================================================
// Header + Selector + ChatInput
// ============================================================================

describe("ChatPanel integration", () => {
  it("header renders ProviderSelector", async () => {
    setupStores({ binding: null })
    const wrapper = mountPanel()
    await flushAll()
    expect(wrapper.find('[data-testid="provider-selector"]').exists()).toBe(true)
  })

  it("ChatInput receives provider-ready from providerStore.canSendPrompt", async () => {
    setupStores({ binding: null })
    const wrapper = mountPanel()
    await flushAll()
    // binding=loaded+null → canSendPrompt=true → ChatInput provider-ready=true
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(true)
  })

  it("binding loading disables ChatInput send", async () => {
    setupStores({ state: "loading", binding: null })
    const wrapper = mountPanel()
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(false)
  })

  it("binding error disables ChatInput send", async () => {
    setupStores({ state: "error", binding: null })
    const wrapper = mountPanel()
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(false)
  })

  it("loaded + null Binding enables ChatInput send", async () => {
    setupStores({ state: "loaded", binding: null })
    const wrapper = mountPanel()
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(true)
  })

  it("loaded + ready Profile enables ChatInput send", async () => {
    setupStores({
      state: "loaded",
      binding: makeBinding({ profile_id: "prof-1" }),
      profileStatus: "ready",
    })
    const wrapper = mountPanel()
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(true)
  })

  it("loaded + invalid Profile disables ChatInput send", async () => {
    setupStores({
      state: "loaded",
      binding: makeBinding({ profile_id: "prof-1" }),
      profileStatus: "needs_key",
    })
    const wrapper = mountPanel()
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(false)
  })

  it("ProviderSelector does not replace session title / status pill", async () => {
    setupStores({ binding: null })
    const wrapper = mountPanel()
    await flushAll()
    // session title 仍是 Conversation
    expect(wrapper.find(".header-title").text()).toContain("S1")
    // status pill 仍存在
    expect(wrapper.find(".header-status").exists()).toBe(true)
  })

  it("narrow container does not throw", async () => {
    setupStores({ binding: null })
    const wrapper = mountPanel()
    await flushAll()
    // 模拟窄容器——只是验证渲染不抛异常
    wrapper.element.style.width = "200px"
    await flushAll()
    expect(wrapper.find('[data-testid="provider-selector"]').exists()).toBe(true)
  })

  it("does not call real provider network", async () => {
    const fetchSpy = vi.spyOn(window as any, "fetch")
    setupStores({ binding: null })
    mountPanel()
    await flushAll()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })

  it("uses the locked-down ChatInput contract in Knowledge mode", async () => {
    setupStores({ binding: null })
    const wrapper = mountPanel("knowledge")
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("attachmentsEnabled")).toBe(false)
    expect(chatInput.props("knowledgeMode")).toBe(true)
    expect(chatInput.props("slashCommands")).toEqual([])
    expect(wrapper.find('[data-testid="context-details-button"]').exists()).toBe(false)
  })

  it("allows blocked sends only when the server explicitly marks auto-recovery possible", async () => {
    setupStores({ binding: null })
    const store = useContextBudgetStore()
    const state = compactionStatus({ active_projection_id: null, can_auto_compact: false })
    store.applyEvent("sess-1", blockedBudget(state))
    const wrapper = mountPanel()
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("contextBlocked")).toBe(true)
    store.applyEvent("sess-1", blockedBudget({ ...state, can_auto_compact: true }))
    await flushAll()
    expect(chatInput.props("contextBlocked")).toBe(false)
    store.applyEvent("sess-1", blockedBudget({ ...state, can_auto_compact: undefined }))
    await flushAll()
    expect(chatInput.props("contextBlocked")).toBe(true)
  })

  it("sends a recoverable blocked preview to the backend without discarding the draft first", async () => {
    const { chatStore } = setupStores({ binding: null })
    const state = compactionStatus({ active_projection_id: null, can_auto_compact: true })
    contextApi.estimateContextBudget.mockResolvedValue(blockedBudget(state))
    const send = vi.spyOn(chatStore, "sendPrompt").mockResolvedValue(undefined)
    vi.spyOn(useFileStore(), "loadFiles").mockResolvedValue(undefined)
    const wrapper = mountPanel()
    await flushAll()
    wrapper.findComponent({ name: "ChatInput" }).vm.$emit("submit", "Recover this turn")
    await flushAll()
    expect(send).toHaveBeenCalledWith(expect.objectContaining({ text: "Recover this turn" }))
  })

  it("keeps an unrecoverable blocked preview in the draft", async () => {
    const { chatStore } = setupStores({ binding: null })
    contextApi.estimateContextBudget.mockResolvedValue(blockedBudget(null))
    const send = vi.spyOn(chatStore, "sendPrompt").mockResolvedValue(undefined)
    const wrapper = mountPanel()
    await flushAll()
    wrapper.findComponent({ name: "ChatInput" }).vm.$emit("submit", "Keep this draft")
    await flushAll()
    expect(send).not.toHaveBeenCalled()
    expect((wrapper.get("textarea").element as HTMLTextAreaElement).value).toBe("Keep this draft")
  })

  it("shows a separate plain-text working summary and pages through original source text", async () => {
    setupStores({ binding: null })
    const store = useContextBudgetStore()
    const state = compactionStatus()
    contextApi.getContextCompaction.mockResolvedValue(state)
    store.applyEvent("sess-1", blockedBudget(state))
    const wrapper = mountPanel()
    await flushAll()
    await wrapper.get('[data-testid="context-details-button"]').trigger("click")
    await flushAll()
    const card = wrapper.get('[data-testid="context-summary-card"]')
    expect(card.get('[data-testid="context-summary-text"]').text()).toBe(state.summary_text)
    expect(card.find("script").exists()).toBe(false)
    expect(card.text()).toContain("Original chat and Memory are unchanged")
    expect(card.get('[data-testid="context-token-change"]').text()).toContain("14,000 → ~7,000")
    contextApi.getContextSource.mockResolvedValue({ entry_id: "entry-1", text: "<img src=x onerror=alert(1)>", offset: 0, next_offset: 6000, total_chars: 6100 })
    await card.get('[data-testid="context-source-button"]').trigger("click")
    await flushAll()
    expect(contextApi.getContextSource).toHaveBeenCalledWith("sess-1", "entry-1", 0)
    const source = card.get('[data-testid="context-source-preview"]')
    expect(source.find("img").exists()).toBe(false)
    expect(source.get("pre").text()).toContain("<img")
    contextApi.getContextSource.mockResolvedValue({ entry_id: "entry-1", text: "Last page", offset: 6000, next_offset: null, total_chars: 6100 })
    await source.findAll("button").find((button) => button.text() === "Next page")!.trigger("click")
    await flushAll()
    expect(contextApi.getContextSource).toHaveBeenLastCalledWith("sess-1", "entry-1", 6000)
    expect(source.get("pre").text()).toBe("Last page")
  })

  it("compacts without replacing or reloading the original chat transcript", async () => {
    const { chatStore } = setupStores({ binding: null })
    const store = useContextBudgetStore()
    const state = compactionStatus()
    contextApi.getContextCompaction.mockResolvedValue(state)
    contextApi.compactContext.mockResolvedValue({ session_id: "sess-1", budget: blockedBudget(state) })
    store.applyEvent("sess-1", blockedBudget(state))
    const loadMessages = vi.spyOn(chatStore, "loadMessages").mockResolvedValue(undefined)
    const wrapper = mountPanel()
    await flushAll()
    await wrapper.get('[data-testid="context-compact-button"]').trigger("click")
    await flushAll()
    expect(contextApi.compactContext).toHaveBeenCalledWith("sess-1")
    expect(loadMessages).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="context-summary-text"]').exists()).toBe(true)
  })

  it("reflects a pending auto-toggle click and restores the confirmed setting if saving fails", async () => {
    setupStores({ binding: null })
    const state = compactionStatus()
    contextApi.getContextCompaction.mockResolvedValue(state)
    useContextBudgetStore().applyEvent("sess-1", blockedBudget(state))
    const wrapper = mountPanel()
    await flushAll()
    let reject!: (cause: unknown) => void
    contextApi.setContextAutoCompaction.mockReturnValue(new Promise((_resolve, fail) => { reject = fail }))
    const toggle = wrapper.get('[data-testid="context-auto-compact-toggle"]')
    await toggle.setValue(false)
    expect((toggle.element as HTMLInputElement).checked).toBe(false)
    expect(toggle.attributes("disabled")).toBeDefined()
    reject(new Error("offline"))
    await flushAll()
    expect((toggle.element as HTMLInputElement).checked).toBe(true)
    expect(toggle.attributes("disabled")).toBeUndefined()
  })

  it("does not show a source response after switching sessions", async () => {
    const { sessionStore } = setupStores({ binding: null })
    const store = useContextBudgetStore()
    const state = compactionStatus()
    contextApi.getContextCompaction.mockResolvedValue(state)
    store.applyEvent("sess-1", blockedBudget(state))
    const wrapper = mountPanel()
    await flushAll()
    await wrapper.get('[data-testid="context-details-button"]').trigger("click")
    await flushAll()
    let resolve!: (value: unknown) => void
    contextApi.getContextSource.mockReturnValue(new Promise((done) => { resolve = done }))
    await wrapper.get('[data-testid="context-source-button"]').trigger("click")
    sessionStore.activeSessionId = "sess-2"
    await flushAll()
    resolve({ entry_id: "entry-1", text: "Private source from sess-1", offset: 0, next_offset: null, total_chars: 26 })
    await flushAll()
    expect(wrapper.find('[data-testid="context-source-preview"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain("Private source from sess-1")
  })
})
