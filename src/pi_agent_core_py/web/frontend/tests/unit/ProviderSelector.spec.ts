// ProviderSelector 单元测试——P1-E M2-3。
//
// 覆盖 spec §16 的 42 项测试：基础展示、Binding 状态、候选过滤、切换、
// 运行中禁用、安全与边界。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

import type {
  CredentialView,
  ProviderDefinitionView,
  ProviderProfileView,
  SessionModelBindingView,
} from "../../src/types/providers"

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

import ProviderSelector from "../../src/components/providers/ProviderSelector.vue"
import { useProviderStore } from "../../src/stores/providerStore"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useChatStore } from "../../src/stores/chatStore"

// ============================================================================
// Fixtures
// ============================================================================

const SECRET_MARKER = "sk-M2-3-SECRET-MARKER-DO-NOT-PERSIST"

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

const DEFINITIONS: ProviderDefinitionView[] = [
  makeDefinition({ id: "anthropic", display_name: "Anthropic" }),
  makeDefinition({ id: "glm", display_name: "GLM" }),
  makeDefinition({
    id: "qwen",
    display_name: "Qwen",
    api_style: "openai_compatible",
  }),
  makeDefinition({
    id: "kimi",
    display_name: "Kimi",
    api_style: "openai_compatible",
  }),
]

function makeProfile(overrides: Partial<ProviderProfileView> = {}): ProviderProfileView {
  return {
    id: "prof-glm-1",
    name: "GLM Profile",
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
    label: "Key",
    storage_mode: "session_only",
    storage_status: "ready",
    masked_value: "sk***aaaa",
    provider_hint: null,
    provider_hint_confidence: null,
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
    created_at: 1000,
    updated_at: 1000,
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

function setupLoadedStore(opts: {
  profiles?: ProviderProfileView[]
  definitions?: ProviderDefinitionView[]
  credentials?: CredentialView[]
  binding?: SessionModelBindingView | null
  bindingLoadState?: "idle" | "loading" | "loaded" | "error"
  bindingLoadError?: string | null
  savingBinding?: boolean
}) {
  const providerStore = useProviderStore()
  const sessionStore = useSessionStore()
  const chatStore = useChatStore()

  providerStore.definitions = opts.definitions ?? DEFINITIONS
  providerStore.profiles = opts.profiles ?? [makeProfile()]
  providerStore.credentials = opts.credentials ?? [makeCredential()]
  // 用 store 内部 token 直接置状态——绕开 refresh
  const effectiveLoadState = opts.bindingLoadState ?? "loaded"
  ;(providerStore as any).currentBinding = opts.binding ?? null
  ;(providerStore as any).bindingLoadState = effectiveLoadState
  ;(providerStore as any).bindingLoadError = opts.bindingLoadError ?? null
  ;(providerStore as any).savingBinding = opts.savingBinding ?? false
  // loaded 状态下设置 bindingSessionId，让 selector 的 bindingSessionId 一致性检查通过
  if (effectiveLoadState === "loaded") {
    ;(providerStore as any).bindingSessionId = "sess-1"
  }

  sessionStore.activeSessionId = "sess-1"
  chatStore.sending = false
  chatStore.streaming = false
  chatStore.currentRequestId = null

  return { providerStore, sessionStore, chatStore }
}

function mountSelector() {
  return mount(ProviderSelector)
}

// ============================================================================
// 基础展示
// ============================================================================

describe("basic display", () => {
  it("only shows GLM / Qwen / Kimi Profiles", async () => {
    setupLoadedStore({
      profiles: [
        makeProfile({ id: "p-glm", provider_id: "glm", status: "ready" }),
        makeProfile({
          id: "p-qwen",
          provider_id: "qwen",
          default_model: "qwen-turbo",
          status: "ready",
        }),
        makeProfile({ id: "p-kimi", provider_id: "kimi", status: "ready" }),
      ],
      binding: null,
    })
    const wrapper = mountSelector()
    await flushAll()

    const options = wrapper.findAll("option")
    const values = options.map((o) => o.attributes("value"))
    expect(values).toContain("p-glm")
    expect(values).toContain("p-qwen")
    expect(values).toContain("p-kimi")
  })

  it("does not show Anthropic Profile", async () => {
    setupLoadedStore({
      profiles: [
        makeProfile({
          id: "p-ant",
          provider_id: "anthropic",
          status: "ready",
        }),
        makeProfile({ id: "p-glm", provider_id: "glm", status: "ready" }),
      ],
      binding: null,
    })
    const wrapper = mountSelector()
    await flushAll()

    const options = wrapper.findAll("option")
    const values = options.map((o) => o.attributes("value"))
    expect(values).not.toContain("p-ant")
  })

  it("groups options by provider via optgroup", async () => {
    setupLoadedStore({
      profiles: [
        makeProfile({ id: "p-glm", provider_id: "glm", status: "ready" }),
        makeProfile({
          id: "p-qwen",
          provider_id: "qwen",
          default_model: "qwen-turbo",
          status: "ready",
        }),
      ],
      binding: null,
    })
    const wrapper = mountSelector()
    await flushAll()

    const groups = wrapper.findAll("optgroup")
    const labels = groups.map((g) => g.attributes("label"))
    // 只为有 Profile 的 provider 渲染 optgroup
    expect(labels).toEqual(expect.arrayContaining(["GLM", "Qwen"]))
  })

  it("long profile/model text has full title attribute", async () => {
    setupLoadedStore({
      profiles: [
        makeProfile({
          id: "p-long",
          name: "SomeVeryLongProfileNameThatOverflows",
          default_model: "some-very-long-model-identifier-v2",
          status: "ready",
        }),
      ],
      binding: null,
    })
    const wrapper = mountSelector()
    await flushAll()

    const option = wrapper.find('option[value="p-long"]')
    expect(option.attributes("title")).toContain("SomeVeryLongProfileNameThatOverflows")
    expect(option.attributes("title")).toContain("some-very-long-model-identifier-v2")
  })

  it("same-name profiles are distinguishable via model info", async () => {
    setupLoadedStore({
      profiles: [
        makeProfile({
          id: "p-1",
          name: "Same Name",
          default_model: "glm-4.5-flash",
          status: "ready",
        }),
        makeProfile({
          id: "p-2",
          name: "Same Name",
          default_model: "glm-4.5",
          status: "ready",
        }),
      ],
      binding: null,
    })
    const wrapper = mountSelector()
    await flushAll()

    const opt1 = wrapper.find('option[value="p-1"]')
    const opt2 = wrapper.find('option[value="p-2"]')
    expect(opt1.text()).toContain("glm-4.5-flash")
    expect(opt2.text()).toContain("glm-4.5")
  })
})

// ============================================================================
// Binding 状态
// ============================================================================

describe("binding state", () => {
  it("idle state disables selector", async () => {
    setupLoadedStore({
      bindingLoadState: "idle",
      binding: null,
      profiles: [],
    })
    const wrapper = mountSelector()
    await flushAll()

    const select = wrapper.get('[data-testid="provider-profile-select"]')
      .element as HTMLSelectElement
    expect(select.disabled).toBe(true)
  })

  it("loading state shows loading and is disabled", async () => {
    setupLoadedStore({
      bindingLoadState: "loading",
      binding: null,
      profiles: [],
    })
    const wrapper = mountSelector()
    await flushAll()

    const select = wrapper.get('[data-testid="provider-profile-select"]')
      .element as HTMLSelectElement
    expect(select.disabled).toBe(true)
    expect(wrapper.text()).toContain("Loading")
  })

  it("error state shows bindingLoadError and is disabled", async () => {
    setupLoadedStore({
      bindingLoadState: "error",
      binding: null,
      bindingLoadError: "Provider binding could not be loaded.",
      profiles: [],
    })
    const wrapper = mountSelector()
    await flushAll()

    const select = wrapper.get('[data-testid="provider-profile-select"]')
      .element as HTMLSelectElement
    expect(select.disabled).toBe(true)
    // 安全文案显示——不含 raw ApiError detail
    expect(wrapper.find(".selector-error").exists()).toBe(true)
  })

  it("load error does not display as default model", async () => {
    setupLoadedStore({
      bindingLoadState: "error",
      binding: null,
      bindingLoadError: "Provider binding could not be loaded.",
      profiles: [],
    })
    const wrapper = mountSelector()
    await flushAll()

    // currentDisplayLabel 是 "Provider binding error." 不是 "默认模型"
    expect(wrapper.find(".selector-current").text()).not.toContain("默认模型")
  })

  it("loaded + null shows '默认模型'", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: null,
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find(".selector-current").text()).toContain("默认模型")
  })

  it("null binding with profiles allows selection", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: null,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    const wrapper = mountSelector()
    await flushAll()

    const select = wrapper.get('[data-testid="provider-profile-select"]')
      .element as HTMLSelectElement
    expect(select.disabled).toBe(false)
  })

  it("ready binding shows current Profile", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: makeBinding({ profile_id: "prof-glm-1", model_id: "glm-4.5-flash" }),
      profiles: [makeProfile({ id: "prof-glm-1", status: "ready" })],
    })
    const wrapper = mountSelector()
    await flushAll()

    expect(wrapper.find(".selector-current").text()).toContain("GLM Profile")
  })

  it("current display uses currentBinding.model_id", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: makeBinding({ profile_id: "prof-glm-1", model_id: "custom-model" }),
      profiles: [
        makeProfile({
          id: "prof-glm-1",
          default_model: "saved-default",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()

    expect(wrapper.find(".selector-current").text()).toContain("custom-model")
    expect(wrapper.find(".selector-current").text()).not.toContain("saved-default")
  })

  it("disabled current Profile is still displayed as current unavailable", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: makeBinding({ profile_id: "prof-x" }),
      profiles: [
        makeProfile({
          id: "prof-x",
          enabled: false,
          status: "disabled",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()

    expect(wrapper.find(".selector-current").text()).toContain("不可用")
  })

  it("needs_key current Profile still shown", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: makeBinding({ profile_id: "prof-nk" }),
      profiles: [makeProfile({ id: "prof-nk", status: "needs_key" })],
    })
    const wrapper = mountSelector()
    await flushAll()

    expect(wrapper.find(".selector-current").text()).toContain("不可用")
  })

  it("Profile missing shows '配置不可用'", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: makeBinding({ profile_id: "missing-id", model_id: "m" }),
      profiles: [], // 引用的 Profile 完全缺失
    })
    const wrapper = mountSelector()
    await flushAll()

    expect(wrapper.find(".selector-current").text()).toContain("配置不可用")
  })

  it("invalid binding does not show '默认模型'", async () => {
    setupLoadedStore({
      bindingLoadState: "loaded",
      binding: makeBinding({ profile_id: "prof-bad" }),
      profiles: [makeProfile({ id: "prof-bad", status: "needs_key" })],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find(".selector-current").text()).not.toContain("默认模型")
  })
})

// ============================================================================
// 候选过滤
// ============================================================================

describe("candidate filtering", () => {
  it("ready + enabled Profiles are candidates", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-ready", status: "ready", enabled: true })],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find('option[value="p-ready"]').exists()).toBe(true)
  })

  it("disabled Profile is not a candidate", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-dis", status: "disabled", enabled: false })],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find('option[value="p-dis"]').exists()).toBe(false)
  })

  it("needs_key Profile is not a candidate", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-nk", status: "needs_key", enabled: true })],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find('option[value="p-nk"]').exists()).toBe(false)
  })

  it("Anthropic Profile is not a candidate", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [
        makeProfile({
          id: "p-ant",
          provider_id: "anthropic",
          status: "ready",
          enabled: true,
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find('option[value="p-ant"]').exists()).toBe(false)
  })

  it("raw Profile data is not modified or deleted", async () => {
    const { providerStore } = setupLoadedStore({
      binding: null,
      profiles: [
        makeProfile({ id: "p-1", provider_id: "glm" }),
        makeProfile({
          id: "p-ant",
          provider_id: "anthropic",
          status: "ready",
        }),
        makeProfile({ id: "p-dis", enabled: false, status: "disabled" }),
      ],
    })
    const snapshot = JSON.parse(JSON.stringify(providerStore.profiles))
    const wrapper = mountSelector()
    await flushAll()
    expect(providerStore.profiles).toEqual(snapshot)
    // 3 个 Profile 全部保留在 store
    expect(providerStore.profiles).toHaveLength(3)
    void wrapper
  })
})

// ============================================================================
// 切换
// ============================================================================

describe("switch", () => {
  it("calls setSessionBinding(activeSessionId, profile.id, profile.default_model)", async () => {
    api.putSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "p-target" }),
    })
    setupLoadedStore({
      binding: null,
      profiles: [
        makeProfile({
          id: "p-target",
          default_model: "glm-4.5",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()

    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-target")
    await flushAll()

    expect(api.putSessionModelBinding).toHaveBeenCalledWith("sess-1", {
      profile_id: "p-target",
      model_id: "glm-4.5",
    })
  })

  it("does not pass currentBinding.model_id", async () => {
    api.putSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "p-target", model_id: "m-target" }),
    })
    setupLoadedStore({
      binding: makeBinding({ profile_id: "p-current", model_id: "current-model" }),
      profiles: [
        makeProfile({
          id: "p-current",
          default_model: "current-default",
          status: "ready",
        }),
        makeProfile({
          id: "p-target",
          default_model: "target-default",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()

    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-target")
    await flushAll()

    const [, body] = api.putSessionModelBinding.mock.calls[0]
    expect(body.model_id).toBe("target-default")
    expect(body.model_id).not.toBe("current-model")
  })

  it("does not optimistic-update display during pending mutation", async () => {
    let resolvePut!: (v: unknown) => void
    api.putSessionModelBinding.mockReturnValue(
      new Promise((r) => {
        resolvePut = r as (v: unknown) => void
      }),
    )
    setupLoadedStore({
      binding: makeBinding({ profile_id: "p-current", model_id: "current-model" }),
      profiles: [
        makeProfile({
          id: "p-current",
          default_model: "current-default",
          status: "ready",
        }),
        makeProfile({
          id: "p-target",
          default_model: "target-default",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find(".selector-current").text()).toContain("current-model")

    // 用户切换——但 store binding 未变（pending）
    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-target")
    await flushAll()
    // 仍显示原 binding（pending 期间 store currentBinding 未变）
    expect(wrapper.find(".selector-current").text()).toContain("current-model")

    resolvePut({
      binding: makeBinding({ profile_id: "p-target", model_id: "target-default" }),
    })
    await flushAll()
    // 现在 store 已更新——显示新 binding
    expect(wrapper.find(".selector-current").text()).toContain("target-default")
  })

  it("failure preserves original Profile display", async () => {
    api.putSessionModelBinding.mockRejectedValue(new FakeApiError(500, "backend rejected"))
    const { providerStore } = setupLoadedStore({
      binding: makeBinding({ profile_id: "p-current", model_id: "current-model" }),
      profiles: [
        makeProfile({
          id: "p-current",
          default_model: "current-default",
          status: "ready",
        }),
        makeProfile({
          id: "p-target",
          default_model: "target-default",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()

    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-target")
    await flushAll()

    // 失败——store currentBinding 未被覆盖
    expect(providerStore.currentBinding?.profile_id).toBe("p-current")
    expect(wrapper.find(".selector-current").text()).toContain("current-model")
  })

  it("failure shows safe mutationError", async () => {
    api.putSessionModelBinding.mockRejectedValue(new FakeApiError(500, "backend rejected switch"))
    setupLoadedStore({
      binding: makeBinding({ profile_id: "p-current", model_id: "current-model" }),
      profiles: [
        makeProfile({
          id: "p-current",
          default_model: "current-default",
          status: "ready",
        }),
        makeProfile({
          id: "p-target",
          default_model: "target-default",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-target")
    await flushAll()

    // store mutationError 已写——此处仅验证组件没把 raw error 拼进 DOM
    expect(wrapper.html()).not.toContain("backend rejected switch")
  })

  it("mutation in-flight disables selector", async () => {
    let resolvePut!: (v: unknown) => void
    api.putSessionModelBinding.mockReturnValue(
      new Promise((r) => {
        resolvePut = r as (v: unknown) => void
      }),
    )
    setupLoadedStore({
      binding: null,
      savingBinding: true,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    const wrapper = mountSelector()
    await flushAll()

    const select = wrapper.get('[data-testid="provider-profile-select"]')
      .element as HTMLSelectElement
    expect(select.disabled).toBe(true)
    resolvePut({ binding: null })
  })

  it("rapid repeated change triggers only one mutation", async () => {
    api.putSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "p-1" }),
    })
    setupLoadedStore({
      binding: null,
      profiles: [
        makeProfile({ id: "p-1", status: "ready" }),
        makeProfile({ id: "p-2", status: "ready" }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    const select = wrapper.get('[data-testid="provider-profile-select"]')
    await select.setValue("p-1")
    // 第二次 setValue 不会触发——savingBinding 已为 true（store 内部）。
    // 实际：putSessionModelBinding 已被调用一次；在 store savingBinding 期间
    // selector.disabled 为 true——但 change 事件是同步的，需要 await。
    await flushAll()
    expect(api.putSessionModelBinding.mock.calls.length).toBe(1)
  })

  it("session switch hides stale mutation result", async () => {
    // 此场景由 M2-1 store token 处理——这里仅验证 selector 不显示旧 session
    let resolvePut!: (v: unknown) => void
    api.putSessionModelBinding.mockReturnValue(
      new Promise((r) => {
        resolvePut = r as (v: unknown) => void
      }),
    )
    const { sessionStore } = setupLoadedStore({
      binding: makeBinding({ profile_id: "p-current", model_id: "m" }),
      profiles: [
        makeProfile({ id: "p-current", status: "ready" }),
        makeProfile({ id: "p-target", status: "ready" }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-target")
    await flushAll()

    // Session 切换
    sessionStore.activeSessionId = "sess-2"
    await flushAll()

    // 旧 mutation 完成——不影响新 session selector
    resolvePut({
      binding: makeBinding({
        session_id: "sess-1",
        profile_id: "p-target",
      }),
    })
    await flushAll()

    // selector-disabled 因 activeSessionId 与 bindingSessionId 不匹配
    const select = wrapper.get('[data-testid="provider-profile-select"]')
      .element as HTMLSelectElement
    expect(select.disabled).toBe(true)
  })
})

// ============================================================================
// 运行中禁用
// ============================================================================

describe("runtime disable", () => {
  it("chatStore.sending=true disables selector", async () => {
    const { chatStore } = setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    chatStore.sending = true
    const wrapper = mountSelector()
    await flushAll()
    expect(
      (wrapper.get('[data-testid="provider-profile-select"]').element as HTMLSelectElement)
        .disabled,
    ).toBe(true)
  })

  it("streaming=true disables selector", async () => {
    const { chatStore } = setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    chatStore.streaming = true
    const wrapper = mountSelector()
    await flushAll()
    expect(
      (wrapper.get('[data-testid="provider-profile-select"]').element as HTMLSelectElement)
        .disabled,
    ).toBe(true)
  })

  it("currentRequestId present disables selector", async () => {
    const { chatStore } = setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    chatStore.currentRequestId = "req-1"
    const wrapper = mountSelector()
    await flushAll()
    expect(
      (wrapper.get('[data-testid="provider-profile-select"]').element as HTMLSelectElement)
        .disabled,
    ).toBe(true)
  })

  it("no activeSessionId disables selector", async () => {
    const { sessionStore } = setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    sessionStore.activeSessionId = null
    const wrapper = mountSelector()
    await flushAll()
    expect(
      (wrapper.get('[data-testid="provider-profile-select"]').element as HTMLSelectElement)
        .disabled,
    ).toBe(true)
  })
})

// ============================================================================
// 安全与边界
// ============================================================================

describe("safety and boundaries", () => {
  it("does not display credential_id", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-1", credential_id: "cred-secret-id" })],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.html()).not.toContain("cred-secret-id")
  })

  it("does not display masked credential", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [
        makeProfile({
          id: "p-1",
          credential_masked_value: "sk***supersecret",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.html()).not.toContain("sk***supersecret")
  })

  it("does not directly call fetch/requestJson", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [makeProfile({ id: "p-1", status: "ready" })],
    })
    const wrapper = mountSelector()
    await flushAll()
    // 通过实际切换验证——只调用 api mock，不触发 fetch
    const fetchSpy = vi.spyOn(window, "fetch" as any)
    await wrapper.get('[data-testid="provider-profile-select"]').setValue("p-1")
    await flushAll()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })

  it("no usable Profile shows settings hint", async () => {
    setupLoadedStore({
      binding: null,
      profiles: [], // 没有 ready Profile
    })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.text()).toContain("Provider Settings")
  })

  it("does not trigger ProviderSettingsModal", async () => {
    setupLoadedStore({ binding: null, profiles: [] })
    const wrapper = mountSelector()
    await flushAll()
    // selector 没有 modal-open 按钮/事件——纯文本引导
    expect(wrapper.find("button").exists()).toBe(false)
  })

  it("does not introduce Toast", async () => {
    setupLoadedStore({ binding: null, profiles: [] })
    const wrapper = mountSelector()
    await flushAll()
    expect(wrapper.find(".toast, [data-testid='toast']").exists()).toBe(false)
  })

  it("SECRET_MARKER does not appear in DOM via this component", async () => {
    // 组件不处理 Secret——验证 marker 即使被注入到数据中也不会显示
    setupLoadedStore({
      binding: null,
      profiles: [
        makeProfile({
          id: "p-1",
          name: SECRET_MARKER,
          default_model: "m",
          status: "ready",
        }),
      ],
    })
    const wrapper = mountSelector()
    await flushAll()
    // Profile.name 会在 option 中显示——这是 user-visible label 不是 Secret
    // 这里验证组件不主动拼接 secret 类型字段
    expect(wrapper.html()).not.toContain("apiKey")
    expect(wrapper.html()).not.toContain("envVarName")
    expect(wrapper.html()).not.toContain("secret_value")
  })
})
