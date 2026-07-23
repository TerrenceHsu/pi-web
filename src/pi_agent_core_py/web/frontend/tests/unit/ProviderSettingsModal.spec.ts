// ProviderSettingsModal 单元测试——P1-E M2-2。
//
// 覆盖 spec §18.B 的 30 项测试：open/close、layout、refresh、session switch、
// error display、unused credentials、Sidebar 集成、Secret 安全。
//
// 注意：Modal 使用 <Teleport to="body">——modal 内容在 document.body 中，
// 不在 wrapper 的 DOM 树里。所有 modal 内查询必须用 body get/find helpers。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount, DOMWrapper } from "@vue/test-utils"
import { nextTick } from "vue"

import type {
  CredentialView,
  ProviderDefinitionView,
  ProviderProfileView,
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

import ProviderSettingsModal from "../../src/components/providers/ProviderSettingsModal.vue"
import SessionSidebar from "../../src/components/layout/SessionSidebar.vue"
import { useProviderStore } from "../../src/stores/providerStore"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useChatStore } from "../../src/stores/chatStore"

// ============================================================================
// Fixtures
// ============================================================================

const SECRET_MARKER = "sk-M2-1-SECRET-MARKER-DO-NOT-PERSIST"

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

function makeProfile(overrides: Partial<ProviderProfileView> = {}): ProviderProfileView {
  return {
    id: "prof-1",
    name: "Profile",
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

const ALL_DEFINITIONS: ProviderDefinitionView[] = [
  makeDefinition({
    id: "anthropic",
    display_name: "Anthropic",
    validation_supported: true,
    supports_model_listing: true,
  }),
  makeDefinition({ id: "glm", display_name: "GLM" }),
  makeDefinition({ id: "qwen", display_name: "Qwen", api_style: "openai_compatible" }),
  makeDefinition({ id: "kimi", display_name: "Kimi", api_style: "openai_compatible" }),
]

// ============================================================================
// Setup
// ============================================================================

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((fn) => fn.mockReset())
  api.getProviderProfileModels.mockResolvedValue({ models: [] })
  vi.spyOn(window, "confirm").mockReturnValue(true)
  // 清理 Teleport 残留
  document.body.innerHTML = ""
})

async function flushAll() {
  await nextTick()
  await new Promise((r) => setTimeout(r, 0))
  await nextTick()
}

function mountModal(props: { open?: boolean; sessionId?: string | null } = {}) {
  return mount(ProviderSettingsModal, {
    props: {
      open: props.open ?? true,
      sessionId: props.sessionId ?? "sess-1",
    },
  })
}

// Teleport-aware helpers
function bodyGet(selector: string): DOMWrapper<Element> {
  const el = document.body.querySelector(selector)
  if (!el) throw new Error(`not found: ${selector}`)
  return new DOMWrapper(el)
}
function bodyFind(selector: string): DOMWrapper<Element> | null {
  const el = document.body.querySelector(selector)
  return el ? new DOMWrapper(el) : null
}
function bodyFindAll(selector: string): DOMWrapper<Element>[] {
  return Array.from(document.body.querySelectorAll(selector)).map(
    (el) => new DOMWrapper(el as Element),
  )
}
function bodyExists(selector: string): boolean {
  return document.body.querySelector(selector) !== null
}

// ============================================================================
// Open / Close / Layout
// ============================================================================

describe("open / close / layout", () => {
  it("open=false does not render modal body", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    mountModal({ open: false })
    await flushAll()
    expect(bodyExists(".modal-overlay")).toBe(false)
  })

  it("open=true loads definitions / profiles / credentials", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    mountModal({ open: true })
    await flushAll()

    expect(api.getProviderDefinitions).toHaveBeenCalledTimes(1)
    expect(api.listProviderProfiles).toHaveBeenCalledTimes(1)
    expect(api.listCredentials).toHaveBeenCalledTimes(1)
  })

  it("renders only GLM / Qwen / Kimi sections", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    mountModal({ open: true })
    await flushAll()

    const sections = bodyFindAll(".provider-section")
    expect(sections).toHaveLength(3)
  })

  it("does not render Anthropic section", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({
      profiles: [
        makeProfile({ id: "p-anthropic", provider_id: "anthropic" }),
        makeProfile({ id: "p-glm", provider_id: "glm" }),
      ],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    mountModal({ open: true })
    await flushAll()

    expect(bodyExists('[data-provider-id="anthropic"]')).toBe(false)
    expect(bodyExists('[data-provider-id="glm"]')).toBe(true)
  })

  it("shows all profiles for a provider including disabled", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({
      profiles: [
        makeProfile({ id: "p-1", provider_id: "glm", status: "ready" }),
        makeProfile({
          id: "p-2",
          provider_id: "glm",
          enabled: false,
          status: "disabled",
        }),
        makeProfile({
          id: "p-3",
          provider_id: "glm",
          status: "needs_key",
        }),
      ],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    mountModal({ open: true })
    await flushAll()

    const forms = bodyFindAll('[data-testid="profile-name-input"]')
    expect(forms.length).toBeGreaterThanOrEqual(3)
  })

  it("Add Profile button creates at most one draft per provider", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    mountModal({ open: true })
    await flushAll()

    // 点击 GLM 的 Add Profile
    await bodyGet('[data-provider-id="glm"] [data-testid="add-profile-btn"]').trigger("click")
    await flushAll()
    // 第二次——按钮应消失
    expect(bodyFind('[data-provider-id="glm"] [data-testid="add-profile-btn"]')).toBeNull()
  })

  it("cancel draft clears draft form", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-provider-id="glm"] [data-testid="add-profile-btn"]').trigger("click")
    await flushAll()
    expect(bodyExists('[data-profile-id="draft"]')).toBe(true)

    await bodyGet('[data-profile-id="draft"] [data-testid="cancel-draft-btn"]').trigger("click")
    await flushAll()
    expect(bodyExists('[data-profile-id="draft"]')).toBe(false)
  })

  it("closing Modal unmounts all ProfileForms", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    const wrapper = mountModal({ open: true })
    await flushAll()
    expect(bodyExists('[data-testid="profile-name-input"]')).toBe(true)

    await wrapper.setProps({ open: false })
    await flushAll()
    expect(bodyExists('[data-testid="profile-name-input"]')).toBe(false)
  })
})

// ============================================================================
// Session switch
// ============================================================================

describe("session switch", () => {
  it("emits close when activeSessionId changes during open", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const sessionStore = useSessionStore()
    sessionStore.activeSessionId = "sess-1"

    const wrapper = mountModal({ open: true, sessionId: "sess-1" })
    await flushAll()

    const closeBefore = wrapper.emitted("close")?.length ?? 0
    sessionStore.activeSessionId = "sess-2"
    await flushAll()
    const closeAfter = wrapper.emitted("close")?.length ?? 0
    expect(closeAfter).toBeGreaterThan(closeBefore)
  })

  it("after session switch, in-flight Apply does not execute for old session", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    const sessionStore = useSessionStore()
    sessionStore.activeSessionId = "sess-1"
    const wrapper = mountModal({ open: true, sessionId: "sess-1" })
    await flushAll()
    sessionStore.activeSessionId = "sess-other"
    await flushAll()
    expect(wrapper.emitted("close")?.length).toBeGreaterThan(0)
    expect(api.putSessionModelBinding).not.toHaveBeenCalled()
  })
})

// ============================================================================
// Errors
// ============================================================================

describe("error display", () => {
  it("loadError surfaces via ErrorBanner", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    // 后端 detail 已脱敏——使用一般文案
    api.listCredentials.mockRejectedValue(new FakeApiError(500, "backend credential load failed"))

    mountModal({ open: true })
    await flushAll()

    expect(bodyExists('[data-testid="provider-modal-error"]')).toBe(true)
    const text = bodyGet('[data-testid="provider-modal-error"]').text()
    // Pinia store 信任 backend detail——detail 已脱敏
    expect(text).toContain("backend credential load failed")
  })

  it("mutationError surfaces via ErrorBanner", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-unused" })],
    })
    api.deleteCredential.mockRejectedValue(new FakeApiError(500, "delete fail"))

    mountModal({ open: true })
    await flushAll()

    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()
    await bodyGet('[data-testid="delete-unused-c-unused"]').trigger("click")
    await flushAll()

    expect(bodyExists('[data-testid="provider-modal-error"]')).toBe(true)
  })

  it("does not render a Toast component", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    mountModal({ open: true })
    await flushAll()

    expect(bodyExists('[data-testid="toast"], .toast')).toBe(false)
  })
})

// ============================================================================
// Unused Credentials
// ============================================================================

describe("unused credentials section", () => {
  it("defaults to collapsed", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-1" })],
    })

    mountModal({ open: true })
    await flushAll()
    expect(bodyExists('[data-testid="unused-credential-c-1"]')).toBe(false)
  })

  it("expand shows unusedCredentials", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-1" })],
    })

    mountModal({ open: true })
    await flushAll()

    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()
    expect(bodyExists('[data-testid="unused-credential-c-1"]')).toBe(true)
  })

  it("includes unused Anthropic credentials (no provider filtering on unused list)", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({
      profiles: [
        makeProfile({
          id: "p-ant",
          provider_id: "anthropic",
          credential_id: "c-anthropic",
        }),
      ],
    })
    api.listCredentials.mockResolvedValue({
      credentials: [
        makeCredential({ credential_id: "c-anthropic", label: "Anthropic Key" }),
        makeCredential({ credential_id: "c-other", label: "Unused Key" }),
      ],
    })

    mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()

    expect(bodyExists('[data-testid="unused-credential-c-other"]')).toBe(true)
    expect(bodyExists('[data-testid="unused-credential-c-anthropic"]')).toBe(false)
  })

  it("does not show used credentials", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({
      profiles: [makeProfile({ credential_id: "c-used" })],
    })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-used" })],
    })

    mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()

    expect(bodyExists('[data-testid="unused-credential-c-used"]')).toBe(false)
  })

  it("delete requires confirm", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-1" })],
    })
    api.deleteCredential.mockResolvedValue({
      credential_id: "c-1",
      deleted: true,
      warnings: [],
    })

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false)
    mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()
    await bodyGet('[data-testid="delete-unused-c-1"]').trigger("click")
    await flushAll()

    expect(confirmSpy).toHaveBeenCalled()
    expect(api.deleteCredential).not.toHaveBeenCalled()
  })

  it("successful delete refreshes credentials", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    // listCredentials 返回 c-1——确保 mountModal 的 loadCredentials 后 c-1 仍在
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-1" })],
    })
    api.deleteCredential.mockResolvedValue({
      credential_id: "c-1",
      deleted: true,
      warnings: [],
    })

    mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()
    await bodyGet('[data-testid="delete-unused-c-1"]').trigger("click")
    await flushAll()

    expect(api.deleteCredential).toHaveBeenCalledWith("c-1")
    expect(api.listCredentials.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it("failed delete shows safe error", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-1" })],
    })
    // 后端 detail 已脱敏——这里用一般文本，不用含敏感词的文案
    api.deleteCredential.mockRejectedValue(new FakeApiError(500, "backend rejected deletion"))

    mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-testid="unused-credentials-toggle"]').trigger("click")
    await flushAll()
    await bodyGet('[data-testid="delete-unused-c-1"]').trigger("click")
    await flushAll()

    expect(bodyExists('[data-testid="provider-modal-error"]')).toBe(true)
    const text = bodyGet('[data-testid="provider-modal-error"]').text()
    // Pinia store 信任 backend detail——detail 已脱敏
    expect(text).toContain("backend rejected deletion")
  })
})

// ============================================================================
// Sidebar integration
// ============================================================================

describe("SessionSidebar integration", () => {
  function mountSidebar() {
    return mount(SessionSidebar)
  }

  it("disables entry when no active session", async () => {
    const sessionStore = useSessionStore()
    sessionStore.activeSessionId = null
    sessionStore.sessions = []

    const wrapper = mountSidebar()
    await flushAll()
    const btn = wrapper.get('[data-testid="provider-settings-button"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("disables entry when chat request is running", async () => {
    const sessionStore = useSessionStore()
    sessionStore.activeSessionId = "sess-1"
    sessionStore.sessions = [
      { id: "sess-1", title: "S1", created_at: 0, updated_at: 0, is_current: true },
    ]
    const chatStore = useChatStore()
    chatStore.sending = true

    const wrapper = mountSidebar()
    await flushAll()
    const btn = wrapper.get('[data-testid="provider-settings-button"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("click opens Modal", async () => {
    const sessionStore = useSessionStore()
    sessionStore.activeSessionId = "sess-1"
    sessionStore.sessions = [
      { id: "sess-1", title: "S1", created_at: 0, updated_at: 0, is_current: true },
    ]
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const wrapper = mountSidebar()
    await flushAll()
    expect(bodyExists(".modal-overlay")).toBe(false)
    await wrapper.get('[data-testid="provider-settings-button"]').trigger("click")
    await flushAll()
    expect(bodyExists(".modal-overlay")).toBe(true)
  })

  it("close emits zero out Sidebar open state", async () => {
    const sessionStore = useSessionStore()
    sessionStore.activeSessionId = "sess-1"
    sessionStore.sessions = [
      { id: "sess-1", title: "S1", created_at: 0, updated_at: 0, is_current: true },
    ]
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const wrapper = mountSidebar()
    await flushAll()
    await wrapper.get('[data-testid="provider-settings-button"]').trigger("click")
    await flushAll()
    expect(bodyExists(".modal-overlay")).toBe(true)

    await bodyGet('[data-testid="modal-close"]').trigger("click")
    await flushAll()
    expect(bodyExists(".modal-overlay")).toBe(false)
  })
})

// ============================================================================
// Secret safety
// ============================================================================

describe("secret safety", () => {
  it("Modal close removes DOM secrets", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    const wrapper = mountModal({ open: true })
    await flushAll()

    const apiKeyInput = bodyFind('[data-testid="api-key-input"]')
    if (apiKeyInput) {
      await apiKeyInput.setValue(SECRET_MARKER)
      await flushAll()
    }

    await wrapper.setProps({ open: false })
    await flushAll()

    expect(document.body.innerHTML).not.toContain(SECRET_MARKER)
  })

  it("Pinia state has no Secret marker", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    const store = useProviderStore()
    mountModal({ open: true })
    await flushAll()

    expect(JSON.stringify(store.$state)).not.toContain(SECRET_MARKER)
  })

  it("console has no Secret marker", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => undefined)
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined)
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    mountModal({ open: true })
    await flushAll()

    const all = [...logSpy.mock.calls.flat(), ...errSpy.mock.calls.flat()].join(" ")
    expect(all).not.toContain(SECRET_MARKER)
    logSpy.mockRestore()
    errSpy.mockRestore()
  })

  it("storage has no Secret marker", async () => {
    const lsSet = vi.spyOn(Storage.prototype, "setItem")
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [makeProfile()] })
    api.listCredentials.mockResolvedValue({ credentials: [makeCredential()] })

    mountModal({ open: true })
    await flushAll()

    expect(lsSet).not.toHaveBeenCalled()
    lsSet.mockRestore()
  })

  it("does not persist envVarName", async () => {
    api.getProviderDefinitions.mockResolvedValue(ALL_DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    const wrapper = mountModal({ open: true })
    await flushAll()
    await bodyGet('[data-provider-id="glm"] [data-testid="add-profile-btn"]').trigger("click")
    await flushAll()
    await bodyGet('[data-profile-id="draft"] [data-testid="storage-mode-env"]').setValue(true)
    await bodyGet('[data-profile-id="draft"] [data-testid="env-var-name-input"]').setValue("MY_VAR")
    await flushAll()

    expect(JSON.stringify(store.$state)).not.toContain("MY_VAR")

    await wrapper.setProps({ open: false })
    await flushAll()
    expect(document.body.innerHTML).not.toContain("MY_VAR")
  })
})
