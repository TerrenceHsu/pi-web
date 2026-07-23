// ChatPanel 集成测试——P1-E M2-3。
//
// 覆盖 spec §19 的 10 项测试：ProviderSelector 渲染、ChatInput 收到 providerReady、
// 各 binding 状态下 ChatInput 可发送性、header 结构保留、不调用真实网络。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

import type { SessionModelBindingView } from "../../src/types/providers"

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
vi.mock("../../src/api/client", () => ({
  ApiError: FakeApiError,
  requestJson: vi.fn(),
}))

// mock abortRun——ChatPanel 用它做 stop
vi.mock("../../src/api", () => ({
  abortRun: vi.fn().mockResolvedValue(undefined),
}))

import ChatPanel from "../../src/components/chat/ChatPanel.vue"
import { useProviderStore } from "../../src/stores/providerStore"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useChatStore } from "../../src/stores/chatStore"

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

function mountPanel() {
  return mount(ChatPanel)
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
})
