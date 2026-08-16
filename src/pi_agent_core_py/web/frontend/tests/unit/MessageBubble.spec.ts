// MessageBubble 单元测试——P1-E M2-3。
//
// 覆盖 spec §18 的 10 项测试：canSendPrompt 门控、各 binding 状态、
// 既有 regeneration guard 保留、其它 action 不受影响。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

import type { ChatStreamItem } from "../../src/types"
import type { SessionModelBindingView } from "../../src/types/providers"

// ----- Mock api/providers -----
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

import MessageBubble from "../../src/components/chat/MessageBubble.vue"
import { useProviderStore } from "../../src/stores/providerStore"
import { useChatStore } from "../../src/stores/chatStore"

// ============================================================================
// Fixtures
// ============================================================================

function makeAssistantItem(overrides: Partial<ChatStreamItem & any> = {}): ChatStreamItem & any {
  return {
    kind: "assistant_message",
    messageId: "msg-1",
    persisted: true,
    streaming: false,
    content: "Hello",
    isRegenerationDraft: false,
    ...overrides,
  }
}

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

function setupProviderState(opts: {
  binding?: SessionModelBindingView | null
  state?: "idle" | "loading" | "loaded" | "error"
}) {
  const providerStore = useProviderStore()
  const chatStore = useChatStore()
  const effectiveState = opts.state ?? "loaded"
  ;(providerStore as any).bindingLoadState = effectiveState
  ;(providerStore as any).bindingSessionId = "sess-1"
  if (effectiveState === "loaded") {
    ;(providerStore as any).currentBinding = opts.binding ?? null
  }
  // 让 chatStore.latestPersistedAssistantMessageId 指向 msg-1
  // chatStore 默认 computed 需要读 streamItems——构造 streamItems
  chatStore.streamItems = [makeAssistantItem({ messageId: "msg-1", persisted: true })]
  return { providerStore, chatStore }
}

function mountBubble(item: any) {
  return mount(MessageBubble, {
    props: {
      item,
      sessionId: "sess-1",
    },
  })
}

// ============================================================================
// Provider gating
// ============================================================================

describe("canSendPrompt gating", () => {
  it("canSendPrompt=true keeps Regenerate enabled", async () => {
    setupProviderState({ binding: makeBinding() })
    const { providerStore } = useProviderStore() // state setup
    void providerStore
    // profiles 中加 ready Profile——selectedProfile 找得到
    const store = useProviderStore()
    store.profiles = [
      {
        id: "prof-1",
        name: "P",
        provider_id: "glm",
        provider_display_name: "GLM",
        credential_id: "c",
        credential_masked_value: null,
        default_model: "m",
        enabled: true,
        is_default: false,
        status: "ready",
        created_at: 1,
        updated_at: 1,
      } as any,
    ]

    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    const btn = wrapper.find('[data-testid="message-regenerate-btn"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes("disabled")).toBeUndefined()
  })

  it("canSendPrompt=false disables button", async () => {
    const { providerStore } = setupProviderState({
      state: "error",
      binding: null,
    })
    void providerStore
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    const btn = wrapper.find('[data-testid="message-regenerate-btn"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes("disabled")).toBeDefined()
  })

  it("canSendPrompt=false click does not emit regenerate", async () => {
    setupProviderState({ state: "error", binding: null })
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    await wrapper.get('[data-testid="message-regenerate-btn"]').trigger("click")
    await flushAll()
    // 没 emit regenerate（chatStore.regenerateAssistantMessage 未被调用）
    // 通过 emit 不存在来断言——MessageBubble 不 emit regenerate，而是调 chatStore action。
    // 这里间接断言：chatStore.regeneration.status 仍是 idle。
    const chatStore = useChatStore()
    expect(chatStore.regeneration?.status ?? "idle").toBe("idle")
  })

  it("bindingLoadState=loading prevents regenerate", async () => {
    setupProviderState({ state: "loading", binding: null })
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    const btn = wrapper.find('[data-testid="message-regenerate-btn"]')
    expect(btn.attributes("disabled")).toBeDefined()
  })

  it("bindingLoadState=error prevents regenerate", async () => {
    setupProviderState({ state: "error", binding: null })
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    expect(
      wrapper.get('[data-testid="message-regenerate-btn"]').attributes("disabled"),
    ).toBeDefined()
  })

  it("loaded + null Binding allows regenerate", async () => {
    setupProviderState({ state: "loaded", binding: null })
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    expect(
      wrapper.get('[data-testid="message-regenerate-btn"]').attributes("disabled"),
    ).toBeUndefined()
  })

  it("loaded + ready Profile allows regenerate", async () => {
    setupProviderState({ state: "loaded", binding: makeBinding({ profile_id: "prof-1" }) })
    const store = useProviderStore()
    store.profiles = [
      {
        id: "prof-1",
        name: "P",
        provider_id: "glm",
        provider_display_name: "GLM",
        credential_id: "c",
        credential_masked_value: null,
        default_model: "m",
        enabled: true,
        is_default: false,
        status: "ready",
        created_at: 1,
        updated_at: 1,
      } as any,
    ]
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    expect(
      wrapper.get('[data-testid="message-regenerate-btn"]').attributes("disabled"),
    ).toBeUndefined()
  })

  it("loaded + invalid Profile prevents regenerate", async () => {
    setupProviderState({
      state: "loaded",
      binding: makeBinding({ profile_id: "prof-bad" }),
    })
    const store = useProviderStore()
    store.profiles = [
      {
        id: "prof-bad",
        name: "Bad",
        provider_id: "glm",
        provider_display_name: "GLM",
        credential_id: "c",
        credential_masked_value: null,
        default_model: "m",
        enabled: true,
        is_default: false,
        status: "needs_key",
        created_at: 1,
        updated_at: 1,
      } as any,
    ]
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    expect(
      wrapper.get('[data-testid="message-regenerate-btn"]').attributes("disabled"),
    ).toBeDefined()
  })

  it("existing regeneration in-flight guard remains", async () => {
    const { chatStore } = setupProviderState({ binding: makeBinding() })
    // 模拟 in-flight
    ;(chatStore as any).regeneration = { status: "running" }
    const wrapper = mountBubble(makeAssistantItem())
    await flushAll()
    // canRegenerate=false——button 不渲染（保持原有隐藏逻辑）
    expect(wrapper.find('[data-testid="message-regenerate-btn"]').exists()).toBe(false)
  })

  it("non-latest assistant does not render Regenerate button at all", async () => {
    const { chatStore } = setupProviderState({ binding: makeBinding() })
    chatStore.streamItems = [
      makeAssistantItem({ messageId: "msg-1", persisted: true }),
      makeAssistantItem({ messageId: "msg-2", persisted: true }),
    ]
    const wrapper = mountBubble(makeAssistantItem({ messageId: "msg-1" }))
    await flushAll()
    // 不是 latestPersistedAssistant——button 不渲染
    expect(wrapper.find('[data-testid="message-regenerate-btn"]').exists()).toBe(false)
  })
})

// ============================================================================
// Assistant Markdown
// ============================================================================

describe("assistant Markdown rendering", () => {
  it("renders common Markdown structures", () => {
    const wrapper = mountBubble(
      makeAssistantItem({
        persisted: false,
        content: [
          "# Result",
          "",
          "**bold** and `inline`",
          "",
          "- first",
          "- second",
          "",
          "```ts",
          "const answer = 42",
          "```",
          "",
          "| name | value |",
          "| --- | --- |",
          "| answer | 42 |",
        ].join("\n"),
      }),
    )

    const markdown = wrapper.get('[data-testid="assistant-markdown"]')
    expect(markdown.get("h1").text()).toBe("Result")
    expect(markdown.get("strong").text()).toBe("bold")
    expect(markdown.get("p code").text()).toBe("inline")
    expect(markdown.findAll("li").map((item) => item.text())).toEqual(["first", "second"])
    expect(markdown.get("pre code").text()).toContain("const answer = 42")
    expect(markdown.get("table").text()).toContain("answer")
  })

  it("does not execute raw HTML or unsafe links", () => {
    const wrapper = mountBubble(
      makeAssistantItem({
        persisted: false,
        content: '<script>alert("xss")</script>\n\n[unsafe](javascript:alert(1))',
      }),
    )

    const markdown = wrapper.get('[data-testid="assistant-markdown"]')
    expect(markdown.find("script").exists()).toBe(false)
    expect(markdown.text()).toContain('<script>alert("xss")</script>')
    expect(markdown.find('a[href^="javascript:"]').exists()).toBe(false)
  })

  it("opens generated links safely in a new tab", () => {
    const wrapper = mountBubble(
      makeAssistantItem({
        persisted: false,
        content: "[OpenAI](https://openai.com)",
      }),
    )

    const link = wrapper.get('[data-testid="assistant-markdown"] a')
    expect(link.attributes("href")).toBe("https://openai.com")
    expect(link.attributes("target")).toBe("_blank")
    expect(link.attributes("rel")).toBe("noopener noreferrer")
  })

  it("keeps user messages as literal text", () => {
    const wrapper = mountBubble({
      kind: "user_message",
      content: "**not bold**",
      files: [],
    })

    expect(wrapper.get('[data-testid="user-message"]').text()).toContain("**not bold**")
    expect(wrapper.find('[data-testid="user-message"] strong').exists()).toBe(false)
  })

  it("keeps the streaming cursor while rendering partial Markdown", () => {
    const wrapper = mountBubble(
      makeAssistantItem({
        persisted: false,
        streaming: true,
        content: "**partial**",
      }),
    )

    expect(wrapper.get('[data-testid="assistant-markdown"] strong').text()).toBe("partial")
    expect(wrapper.find(".stream-cursor").exists()).toBe(true)
  })
})
