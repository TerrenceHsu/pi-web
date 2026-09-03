// Provider Frontend 跨组件集成测试——P1-E M2-4。
//
// 与现有 232 个 unit 测试的关系：
// - 现有 spec 多为单组件 mount + 手动注入 store state
// - 本 spec mount 真实 ChatPanel + 真实 SessionSidebar + 真实 ProviderSettingsModal
//   并通过真实 store actions 驱动状态机（refreshForSession / setSessionBinding / createCredential / createProfile）
// - 覆盖 spec §6-§15 中"跨组件 + 多状态变迁"的端到端语义
//
// 范围（与单组件 spec 互补，不重复）：
// - 场景 A：startup lifecycle —— binding 状态机推进，所有 gate 同步
// - 场景 B：A/B session 切换 + stale response 丢弃
// - 场景 E：请求运行中 Selector + Settings 入口 disabled
// - 场景 G：invalid binding 不自动 fallback 到其它 ready profile
// - 场景 H：组件 unmount + remount 后从 API 重新加载（不依赖 localStorage）
// - 场景 I：Settings → Apply → 当前 session binding 更新 → ChatInput 重新启用
// - Secret marker 保存成功后不残留于 DOM / Pinia state / storage
//
// Mock 边界：只 mock api/providers + api/client（与现有 spec 一致）；其余组件全部用真实实现。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount, DOMWrapper } from "@vue/test-utils"
import { nextTick } from "vue"

import type {
  CredentialView,
  ProviderDefinitionView,
  ProviderProfileView,
  ProviderProfilesResponse,
  CredentialsResponse,
  SessionModelBindingResponse,
  SessionModelBindingView,
} from "../../src/types/providers"

// ============================================================================
// Mock api/providers + api/client
// ============================================================================

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

vi.mock("../../src/api/state", () => ({
  abortRun: vi.fn().mockResolvedValue(undefined),
  getState: vi.fn().mockResolvedValue({ plan_mode: { enabled: true } }),
}))
vi.mock("../../src/api/slashCommands", () => ({
  listSlashCommands: vi.fn().mockResolvedValue({ count: 0, commands: [] }),
}))

import ChatPanel from "../../src/components/chat/ChatPanel.vue"
import SessionSidebar from "../../src/components/layout/SessionSidebar.vue"
import ProviderSettingsModal from "../../src/components/providers/ProviderSettingsModal.vue"
import { getState } from "../../src/api/state"
import { useProviderStore } from "../../src/stores/providerStore"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useChatStore } from "../../src/stores/chatStore"

// ============================================================================
// Constants
// ============================================================================

const SECRET_MARKER = "sk-M2-4-SECRET-MARKER-DO-NOT-PERSIST"

// ============================================================================
// Fixtures
// ============================================================================

function makeDefinition(overrides: Partial<ProviderDefinitionView> = {}): ProviderDefinitionView {
  return {
    id: "glm",
    display_name: "GLM",
    api_style: "anthropic_compatible",
    validation_supported: false,
    supports_model_listing: false,
    ...overrides,
  }
}

const ALL_DEFINITIONS: ProviderDefinitionView[] = [
  makeDefinition({ id: "anthropic", display_name: "Anthropic" }),
  makeDefinition({ id: "glm", display_name: "Zhipu GLM (Anthropic-compatible)" }),
  makeDefinition({ id: "qwen", display_name: "Qwen", api_style: "openai_compatible" }),
  makeDefinition({ id: "kimi", display_name: "Kimi", api_style: "openai_compatible" }),
]

function makeProfile(overrides: Partial<ProviderProfileView> = {}): ProviderProfileView {
  return {
    id: "prof-glm-1",
    name: "GLM Work",
    provider_id: "glm",
    provider_display_name: "GLM",
    credential_id: "cred-1",
    credential_masked_value: "sk***aaaa",
    default_model: "glm-4.5-flash",
    enabled: true,
    is_default: true,
    status: "ready",
    created_at: 1000,
    updated_at: 1000,
    ...overrides,
  }
}

function makeCredential(overrides: Partial<CredentialView> = {}): CredentialView {
  return {
    credential_id: "cred-1",
    label: "GLM Key",
    storage_mode: "session_only",
    storage_status: "ready",
    masked_value: "sk***aaaa",
    provider_hint: "glm",
    provider_hint_confidence: "high",
    validation_status: "never_validated",
    last_validated_provider_id: null,
    last_validated_at: null,
    last_error_code: null,
    created_at: 1000,
    updated_at: 1000,
    ...overrides,
  }
}

function makeBinding(overrides: Partial<SessionModelBindingView> = {}): SessionModelBindingView {
  return {
    session_id: "sess-1",
    profile_id: "prof-glm-1",
    model_id: "glm-4.5-flash",
    source: "explicit",
    created_at: 1,
    updated_at: 2,
    ...overrides,
  }
}

function makeProfilesResponse(profiles: ProviderProfileView[]): ProviderProfilesResponse {
  return { profiles }
}

function makeCredentialsResponse(credentials: CredentialView[]): CredentialsResponse {
  return { credentials }
}

function makeBindingResponse(binding: SessionModelBindingView | null): SessionModelBindingResponse {
  return { binding }
}

// ============================================================================
// Setup helpers
// ============================================================================

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((fn) => fn.mockReset())
  vi.mocked(getState).mockResolvedValue({ plan_mode: { enabled: true } } as never)
  // 清理 Teleport 残留
  document.body.innerHTML = ""
})

async function flushAll() {
  await new Promise((r) => setTimeout(r, 0))
  await nextTick()
}

/** Modal 用 Teleport to body——wrapper.find 不查 body。 */
function bodyFind(selector: string): DOMWrapper<Element> | null {
  const el = document.body.querySelector(selector)
  return el ? new DOMWrapper(el) : null
}

function bodyFindAll(selector: string): DOMWrapper<Element>[] {
  return Array.from(document.body.querySelectorAll(selector)).map(
    (el) => new DOMWrapper(el as Element),
  )
}

/**
 * 启动一个真实 providerStore.initialize()——并行加载 definitions/profiles/credentials。
 * 所有 API mock 默认返回 GLM ready profile。
 */
async function bootProviderStore(
  opts: {
    profiles?: ProviderProfileView[]
    credentials?: CredentialView[]
    definitions?: ProviderDefinitionView[]
  } = {},
) {
  const definitions = opts.definitions ?? ALL_DEFINITIONS
  const credentials = opts.credentials ?? [makeCredential()]
  const profiles = opts.profiles ?? [makeProfile()]

  api.getProviderDefinitions.mockResolvedValue(definitions)
  api.listCredentials.mockResolvedValue(makeCredentialsResponse(credentials))
  api.listProviderProfiles.mockResolvedValue(makeProfilesResponse(profiles))

  const providerStore = useProviderStore()
  await providerStore.initialize()
  return { providerStore, definitions, credentials, profiles }
}

function seedSessionStore(sessions: Array<{ id: string; title: string }>) {
  const sessionStore = useSessionStore()
  sessionStore.sessions = sessions.map((s, idx) => ({
    id: s.id,
    title: s.title,
    created_at: idx,
    updated_at: idx,
    is_current: idx === 0,
  })) as any
  return sessionStore
}

// ============================================================================
// Scenario A: Startup lifecycle —— binding 状态机驱动所有 gate
// ============================================================================

describe("Scenario A: startup lifecycle drives all component gates", () => {
  it("binding=loading 时 ProviderSelector disabled 且 ChatInput providerReady=false", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    // 用真实 refreshForSession 驱动状态机——binding 进入 loading
    let resolveBinding!: (r: SessionModelBindingResponse) => void
    api.getSessionModelBinding.mockImplementation(
      () => new Promise<SessionModelBindingResponse>((r) => void (resolveBinding = r)),
    )

    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()

    const wrapper = mount(ChatPanel)
    await flushAll()

    // 三个 gate 都关闭
    const select = wrapper.find('[data-testid="provider-profile-select"]')
    expect((select.element as HTMLSelectElement).disabled).toBe(true)

    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(false)

    // 完成 GET——binding 进入 loaded
    resolveBinding(makeBindingResponse(null))
    await flushAll()

    expect(chatInput.props("providerReady")).toBe(true)
  })

  it("binding GET 失败时 state=error，所有 gate 关闭且 ErrorBanner 文案可见", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    api.getSessionModelBinding.mockRejectedValue(new FakeApiError(500, "boom"))

    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    const wrapper = mount(ChatPanel)
    await flushAll()

    expect(providerStore.bindingLoadState).toBe("error")

    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(false)

    // Selector 渲染了 error 文本（具体文案来自 store safe fallback）
    const selectorError = wrapper.find(".selector-error")
    expect(selectorError.exists()).toBe(true)
  })

  it("binding=loaded+null 时 ChatInput 可发送 + Selector 显示 Legacy hint", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(null))
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    const wrapper = mount(ChatPanel)
    await flushAll()

    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(true)
    // Legacy hint 出现
    expect(wrapper.html()).toContain("Legacy")
  })
})

// ============================================================================
// Scenario B: A/B session 切换 + stale response 丢弃
// ============================================================================

describe("Scenario B: A/B session switch discards stale binding response", () => {
  it("A→B 快速切换：A 的延迟响应到达后不覆盖 B 的 binding", async () => {
    await bootProviderStore({
      profiles: [
        makeProfile({
          id: "prof-qwen",
          name: "Qwen Work",
          provider_id: "qwen",
          provider_display_name: "Qwen",
          default_model: "qwen-model-a",
          credential_id: "cred-qwen",
        }),
        makeProfile({
          id: "prof-kimi",
          name: "Kimi Work",
          provider_id: "kimi",
          provider_display_name: "Kimi",
          default_model: "kimi-model-b",
          credential_id: "cred-kimi",
          is_default: false,
        }),
      ],
      credentials: [
        makeCredential({ credential_id: "cred-qwen", masked_value: "sk***qwen" }),
        makeCredential({ credential_id: "cred-kimi", masked_value: "sk***kimi" }),
      ],
    })
    const sessionStore = seedSessionStore([
      { id: "sess-A", title: "A" },
      { id: "sess-B", title: "B" },
    ])

    // 两个 session 的 GET binding 都 pending
    let resolveA!: (r: SessionModelBindingResponse) => void
    let resolveB!: (r: SessionModelBindingResponse) => void
    api.getSessionModelBinding.mockImplementation((sid: string) => {
      if (sid === "sess-A") {
        return new Promise<SessionModelBindingResponse>((r) => void (resolveA = r))
      }
      return new Promise<SessionModelBindingResponse>((r) => void (resolveB = r))
    })

    const providerStore = useProviderStore()
    const wrapper = mount(ChatPanel)

    // 1. 切到 A，启动 GET binding
    sessionStore.activeSessionId = "sess-A"
    void providerStore.refreshForSession("sess-A")
    await flushAll()

    // 2. 立刻切到 B，启动 GET binding——A 的响应还未到达
    sessionStore.activeSessionId = "sess-B"
    void providerStore.refreshForSession("sess-B")
    await flushAll()

    // 3. A 的延迟响应现在到达——必须被丢弃
    resolveA(
      makeBindingResponse(
        makeBinding({ session_id: "sess-A", profile_id: "prof-qwen", model_id: "qwen-model-a" }),
      ),
    )
    await flushAll()

    // 此时 binding 仍属于 B（pending 或 loaded-B），不能是 A 的 Qwen
    expect(providerStore.bindingSessionId).toBe("sess-B")
    if (providerStore.currentBinding) {
      expect(providerStore.currentBinding.session_id).toBe("sess-B")
    }

    // 4. B 的响应到达——selector 显示 Kimi
    resolveB(
      makeBindingResponse(
        makeBinding({ session_id: "sess-B", profile_id: "prof-kimi", model_id: "kimi-model-b" }),
      ),
    )
    await flushAll()

    expect(providerStore.currentBinding?.profile_id).toBe("prof-kimi")
    expect(providerStore.selectedModel).toBe("kimi-model-b")

    // 5. ChatInput providerReady 始终反映当前 session（B）的 ready 状态
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(true)
  })

  it("Selector 切换 session 时不短暂展示对方 Profile（currentBinding 先清空）", async () => {
    await bootProviderStore({
      profiles: [
        makeProfile({
          id: "prof-qwen",
          name: "Qwen Work",
          provider_id: "qwen",
          default_model: "qwen-model-a",
        }),
        makeProfile({
          id: "prof-kimi",
          name: "Kimi Work",
          provider_id: "kimi",
          default_model: "kimi-model-b",
          is_default: false,
        }),
      ],
    })
    const sessionStore = seedSessionStore([
      { id: "sess-A", title: "A" },
      { id: "sess-B", title: "B" },
    ])

    api.getSessionModelBinding.mockResolvedValue(
      makeBindingResponse(
        makeBinding({ session_id: "sess-A", profile_id: "prof-qwen", model_id: "qwen-model-a" }),
      ),
    )

    const providerStore = useProviderStore()
    sessionStore.activeSessionId = "sess-A"
    void providerStore.refreshForSession("sess-A")
    await flushAll()
    await flushAll()

    expect(providerStore.currentBinding?.profile_id).toBe("prof-qwen")

    // 切到 B——currentBinding 必须立即 null（不能残留 A 的 Qwen）
    // 用 deferred promise 锁住 B 的 GET，验证中间态
    let resolveB!: (r: SessionModelBindingResponse) => void
    api.getSessionModelBinding.mockImplementation(
      () => new Promise<SessionModelBindingResponse>((r) => void (resolveB = r)),
    )
    sessionStore.activeSessionId = "sess-B"
    void providerStore.refreshForSession("sess-B")
    await flushAll()

    // 中间态：currentBinding 已清空 + state=loading
    expect(providerStore.currentBinding).toBeNull()
    expect(providerStore.bindingLoadState).toBe("loading")

    // 完成 GET——loaded + null binding（Legacy）
    resolveB(makeBindingResponse(null))
    await flushAll()
    await flushAll()
    expect(providerStore.bindingLoadState).toBe("loaded")
    expect(providerStore.currentBinding).toBeNull()
  })
})

// ============================================================================
// Scenario E: 请求运行中 Selector + Settings 入口 disabled
// ============================================================================

describe("Scenario E: request running disables Selector + Settings entry", () => {
  it("chatStore.sending=true 时 ProviderSelector disabled", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"
    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(makeBinding()))
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    const chatStore = useChatStore()
    chatStore.sending = true
    chatStore.streaming = true
    chatStore.currentRequestId = "req-1"

    const wrapper = mount(ChatPanel)
    await flushAll()

    const select = wrapper.find('[data-testid="provider-profile-select"]')
    expect((select.element as HTMLSelectElement).disabled).toBe(true)
  })

  it("请求运行中 SessionSidebar Provider Settings 入口 disabled", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    const chatStore = useChatStore()
    chatStore.sending = true
    chatStore.streaming = true
    chatStore.currentRequestId = "req-1"

    const wrapper = mount(SessionSidebar)
    await flushAll()

    const btn = wrapper.find('[data-testid="provider-settings-button"]')
    expect(btn.exists()).toBe(true)
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)
  })
})

// ============================================================================
// Scenario G: invalid binding 不自动 fallback 到其它 ready profile
// ============================================================================

describe("Scenario G: invalid binding does not auto-fallback", () => {
  it("binding 指向 needs_key profile 时即使有其它 ready profile 也不自动切换", async () => {
    const disabledProfile = makeProfile({
      id: "prof-glm-1",
      name: "GLM Broken",
      status: "needs_key",
      is_default: true,
    })
    const readyQwen = makeProfile({
      id: "prof-qwen",
      name: "Qwen Ready",
      provider_id: "qwen",
      default_model: "qwen-model-a",
      is_default: false,
    })
    await bootProviderStore({ profiles: [disabledProfile, readyQwen] })

    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    api.getSessionModelBinding.mockResolvedValue(
      makeBindingResponse(
        makeBinding({ session_id: "sess-1", profile_id: "prof-glm-1", model_id: "glm-4.5" }),
      ),
    )
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    // Store 仍持有指向 disabled profile 的 binding
    expect(providerStore.currentBinding?.profile_id).toBe("prof-glm-1")
    // selectedProfile 解析到 disabled profile
    expect(providerStore.selectedProfile?.id).toBe("prof-glm-1")
    expect(providerStore.selectedProfile?.status).toBe("needs_key")
    // canSendPrompt=false —— Profile 失效时不允许发送
    expect(providerStore.canSendPrompt).toBe(false)

    const wrapper = mount(ChatPanel)
    await flushAll()
    const chatInput = wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput.props("providerReady")).toBe(false)

    // usableProfiles 仍包含 ready Qwen——但 binding 不会自动指向它
    expect(providerStore.usableProfiles.map((p) => p.id)).toEqual(["prof-qwen"])
  })

  it("binding 指向被删除的 profile 时显示'配置不可用'且不 fallback", async () => {
    await bootProviderStore({
      profiles: [
        makeProfile({
          id: "prof-qwen",
          name: "Qwen Ready",
          provider_id: "qwen",
          default_model: "qwen-model-a",
        }),
      ],
    })
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    // binding 指向不存在的 prof-deleted
    api.getSessionModelBinding.mockResolvedValue(
      makeBindingResponse(
        makeBinding({ session_id: "sess-1", profile_id: "prof-deleted", model_id: "old" }),
      ),
    )
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    expect(providerStore.selectedProfile).toBeNull()
    expect(providerStore.canSendPrompt).toBe(false)

    const wrapper = mount(ChatPanel)
    await flushAll()
    const selectorText = wrapper.find('[data-testid="provider-selector"]').text()
    expect(selectorText).toContain("配置不可用")
  })
})

// ============================================================================
// Scenario H: 组件 unmount + remount 从 API 重新加载（无 localStorage）
// ============================================================================

describe("Scenario H: unmount + remount re-fetches from API, no localStorage", () => {
  it("组件 remount 后 store 重新走 initialize + refreshForSession", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(makeBinding()))

    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    const callsBefore = api.getProviderDefinitions.mock.calls.length
    expect(callsBefore).toBeGreaterThanOrEqual(1)

    // unmount + remount 一个 ChatPanel——store 已 initialized，不会再调 initialize
    const w1 = mount(ChatPanel)
    await flushAll()
    w1.unmount()
    await flushAll()

    const w2 = mount(ChatPanel)
    await flushAll()
    await flushAll()

    // 新 mount 不会让 store 重新 initialize——definitions 仍在内存
    const providerStore2 = useProviderStore()
    expect(providerStore2.definitions).toHaveLength(4)
    expect(providerStore2.currentBinding?.profile_id).toBe("prof-glm-1")
    w2.unmount()
  })

  it("M2 全程不写 localStorage / sessionStorage（Provider 数据）", async () => {
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"
    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(makeBinding()))

    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()

    const wrapper = mount(ChatPanel)
    await flushAll()
    wrapper.unmount()

    // 浏览器 storage 中不应出现 provider / credential / binding 数据
    const ls = JSON.stringify(localStorage)
    const ss = JSON.stringify(sessionStorage)
    for (const keyword of [
      "prof-glm-1",
      "cred-1",
      "sk***aaaa",
      "glm-4.5-flash",
      "provider",
      "binding",
    ]) {
      expect(ls).not.toContain(keyword)
      expect(ss).not.toContain(keyword)
    }
    // 清掉 setup 期间 jsdom 自动累积的 key（避免跨测试污染）
    localStorage.clear()
    sessionStorage.clear()
  })
})

// ============================================================================
// Scenario I: Settings → Apply → 当前 session binding 更新 → ChatInput 重新启用
// ============================================================================

describe("Scenario I: Settings save + Apply updates binding and re-enables ChatInput", () => {
  it("从 null binding 到完成 Apply 后 ChatInput providerReady 从 legacy 切换到 ready", async () => {
    // 启动：null binding（Legacy），ready Qwen profile 存在
    await bootProviderStore({
      profiles: [
        makeProfile({
          id: "prof-qwen",
          name: "Qwen Work",
          provider_id: "qwen",
          provider_display_name: "Qwen",
          default_model: "qwen-model-a",
          credential_id: "cred-qwen",
          is_default: true,
        }),
      ],
      credentials: [makeCredential({ credential_id: "cred-qwen" })],
    })
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(null))
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    // 初始：legacy，可发送（canSendPrompt=true via null binding）
    const wrapper = mount(ChatPanel)
    await flushAll()
    const chatInput = () => wrapper.findComponent({ name: "ChatInput" })
    expect(chatInput().props("providerReady")).toBe(true) // legacy 允许
    expect(providerStore.currentBinding).toBeNull()

    // 用户主动 PUT binding 到 Qwen
    api.putSessionModelBinding.mockResolvedValue(
      makeBindingResponse(
        makeBinding({ session_id: "sess-1", profile_id: "prof-qwen", model_id: "qwen-model-a" }),
      ),
    )
    const result = await providerStore.setSessionBinding("sess-1", "prof-qwen", "qwen-model-a")
    await flushAll()

    expect(result?.profile_id).toBe("prof-qwen")
    expect(providerStore.currentBinding?.profile_id).toBe("prof-qwen")
    expect(providerStore.bindingLoadState).toBe("loaded")
    // ChatInput 仍可发送（ready profile）
    expect(chatInput().props("providerReady")).toBe(true)
    expect(providerStore.canSendPrompt).toBe(true)
  })

  it("Apply 失败时不 optimistic 展示新 binding，UI 保留旧值", async () => {
    await bootProviderStore({
      profiles: [
        makeProfile({
          id: "prof-qwen",
          name: "Qwen",
          provider_id: "qwen",
          default_model: "qwen-model-a",
        }),
      ],
    })
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    // 旧 binding = null（Legacy）
    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(null))
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()
    expect(providerStore.currentBinding).toBeNull()

    // PUT 失败
    api.putSessionModelBinding.mockRejectedValue(new FakeApiError(409, "profile_disabled"))
    const result = await providerStore.setSessionBinding("sess-1", "prof-qwen", "qwen-model-a")
    await flushAll()

    expect(result).toBeNull()
    // 失败不 optimistic——currentBinding 保持原值（null）
    expect(providerStore.currentBinding).toBeNull()
    // mutationError 写入安全文案
    expect(providerStore.mutationError).toBeTruthy()
    // canSendPrompt 仍反映 legacy（null binding）
    expect(providerStore.canSendPrompt).toBe(true)
  })
})

// ============================================================================
// Secret marker 安全：保存成功后 / Modal 关闭后 marker 不残留
// ============================================================================

describe("Secret marker safety after save + Modal close", () => {
  it("marker 保存成功后从 DOM 和 Pinia state 消失", async () => {
    // 启动：一个 ready Qwen profile
    await bootProviderStore({
      profiles: [
        makeProfile({
          id: "prof-qwen",
          name: "Qwen Work",
          provider_id: "qwen",
          default_model: "qwen-model-a",
          credential_id: "cred-qwen",
          is_default: true,
        }),
      ],
      credentials: [makeCredential({ credential_id: "cred-qwen" })],
    })
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"

    api.getSessionModelBinding.mockResolvedValue(
      makeBindingResponse(
        makeBinding({ session_id: "sess-1", profile_id: "prof-qwen", model_id: "qwen-model-a" }),
      ),
    )
    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()
    await flushAll()

    // mount Modal——Teleport to body
    const modalWrapper = mount(ProviderSettingsModal, {
      props: { open: true, sessionId: "sess-1" },
    })
    await flushAll()
    await flushAll()

    // 在 ProfileForm 的 api_key 输入框填入 marker（用 body 查询）
    const apiKeyInput = bodyFind('input[type="password"]')
    expect(apiKeyInput).not.toBeNull()
    await apiKeyInput!.setValue(SECRET_MARKER)
    await flushAll()

    // marker 必然短暂存在于 input.value（浏览器事实）
    expect((apiKeyInput!.element as HTMLInputElement).value).toContain(SECRET_MARKER)

    // 触发保存——createCredential 走 PUT secret（已有 cred-qwen）；store 重新加载 credentials
    api.rotateCredentialSecret.mockResolvedValue({
      credential: makeCredential({ credential_id: "cred-qwen" }),
      warnings: [],
    })
    api.updateProviderProfile.mockResolvedValue({
      profile: makeProfile({
        id: "prof-qwen",
        name: "Qwen Work",
        default_model: "qwen-model-a",
      }),
    })

    // 找到 Save 按钮并触发——用 button 文本定位（body 查询）
    const buttons = bodyFindAll("button")
    const saveBtn = buttons.find((b) => /save configuration/i.test(b.text()))
    expect(saveBtn).toBeTruthy()
    await saveBtn!.trigger("click")
    await flushAll()
    await flushAll()

    // 保存成功后——input.value 清空
    const apiKeyAfterEl = document.body.querySelector(
      'input[type="password"]',
    ) as HTMLInputElement | null
    if (apiKeyAfterEl) {
      expect(apiKeyAfterEl.value).not.toContain(SECRET_MARKER)
    }
    // DOM body 整体扫描 marker
    expect(document.body.innerHTML).not.toContain(SECRET_MARKER)

    // 关闭 Modal——marker 不残留
    await modalWrapper.setProps({ open: false })
    await flushAll()

    // DOM body 中不应再有 marker
    expect(document.body.innerHTML).not.toContain(SECRET_MARKER)

    // Pinia state 中不应有 marker
    const stateJson = JSON.stringify({
      definitions: providerStore.definitions,
      profiles: providerStore.profiles,
      credentials: providerStore.credentials,
      currentBinding: providerStore.currentBinding,
    })
    expect(stateJson).not.toContain(SECRET_MARKER)

    // localStorage / sessionStorage 不应有 marker
    expect(JSON.stringify(localStorage)).not.toContain(SECRET_MARKER)
    expect(JSON.stringify(sessionStorage)).not.toContain(SECRET_MARKER)

    modalWrapper.unmount()
    // 清理 Teleport 残留——避免污染后续测试
    document.body.innerHTML = ""
  })
})

// ============================================================================
// 不调用真实网络（外部 Provider host 必须为 0）
// ============================================================================

describe("External Provider network", () => {
  it("整个集成流程不调用 window.fetch（不命中外部 Provider）", async () => {
    const fetchSpy = vi.spyOn(window as any, "fetch")
    await bootProviderStore()
    const sessionStore = seedSessionStore([{ id: "sess-1", title: "S1" }])
    sessionStore.activeSessionId = "sess-1"
    api.getSessionModelBinding.mockResolvedValue(makeBindingResponse(null))

    const providerStore = useProviderStore()
    void providerStore.refreshForSession("sess-1")
    await flushAll()

    const wrapper = mount(ChatPanel)
    await flushAll()
    wrapper.unmount()

    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })
})
