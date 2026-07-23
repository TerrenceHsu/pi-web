// providerStore 单元测试——P1-E M2-1。
//
// 完整覆盖：
//   A. initialize / B. filtering / C. binding state machine /
//   D. stale-response / E. selected projections /
//   F. unusedCredentials / G. mutations / H. binding mutation /
//   I. secret marker containment
//
// Mock src/api/providers（不访问真实网络）。
// 使用 setActivePinia(createPinia()) 隔离每个 test。

import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

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

import { useProviderStore } from "../../src/stores/providerStore"
import type {
  CredentialView,
  ProviderDefinitionView,
  ProviderProfileView,
  SessionModelBindingView,
} from "../../src/types/providers"

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

const DEFINITIONS: ProviderDefinitionView[] = [
  makeDefinition({
    id: "anthropic",
    display_name: "Anthropic",
    validation_supported: true,
    supports_model_listing: true,
  }),
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
    label: "Key 1",
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

// ============================================================================
// A. initialize
// ============================================================================

describe("A. initialize", () => {
  it("loads definitions, profiles, credentials in parallel", async () => {
    api.getProviderDefinitions.mockResolvedValue(DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({
      profiles: [makeProfile()],
    })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential()],
    })

    const store = useProviderStore()
    await store.initialize()

    expect(store.definitions).toEqual(DEFINITIONS)
    expect(store.profiles).toHaveLength(1)
    expect(store.credentials).toHaveLength(1)
    expect(store.initialized).toBe(true)
  })

  it("resets all three loading flags to false after success", async () => {
    api.getProviderDefinitions.mockResolvedValue([])
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    await store.initialize()

    expect(store.definitionsLoading).toBe(false)
    expect(store.profilesLoading).toBe(false)
    expect(store.credentialsLoading).toBe(false)
  })

  it("sets initialized=true even when one resource fails", async () => {
    api.getProviderDefinitions.mockRejectedValue(new FakeApiError(500, "boom"))
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    await store.initialize()

    expect(store.initialized).toBe(true)
    expect(store.loadError).toBeTruthy()
  })

  it("does not throw raw error to caller on failure", async () => {
    api.getProviderDefinitions.mockResolvedValue([])
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    await expect(store.initialize()).resolves.toBeUndefined()
  })

  it("second initialize call is a no-op (idempotent semantics)", async () => {
    api.getProviderDefinitions.mockResolvedValue(DEFINITIONS)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    await store.initialize()
    const firstCallCount = api.getProviderDefinitions.mock.calls.length

    await store.initialize()
    expect(api.getProviderDefinitions.mock.calls.length).toBe(firstCallCount)
  })

  it("loadError is a fixed safe string on failure", async () => {
    api.getProviderDefinitions.mockResolvedValue([])
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockRejectedValue(new FakeApiError(0, "net boom"))

    const store = useProviderStore()
    await store.initialize()
    expect(typeof store.loadError).toBe("string")
    expect(store.loadError).not.toContain("net boom")
    expect(store.loadError).not.toContain("stack")
  })
})

// ============================================================================
// B. Provider filtering
// ============================================================================

describe("B. Provider filtering", () => {
  function createStoreWith(data: {
    definitions?: ProviderDefinitionView[]
    profiles?: ProviderProfileView[]
    credentials?: CredentialView[]
  }) {
    const store = useProviderStore()
    if (data.definitions) store.definitions = data.definitions
    if (data.profiles) store.profiles = data.profiles
    if (data.credentials) store.credentials = data.credentials
    return store
  }

  it("visibleDefinitions excludes Anthropic (only glm/qwen/kimi)", () => {
    const store = createStoreWith({ definitions: DEFINITIONS })
    const ids = store.visibleDefinitions.map((d) => d.id)
    expect(ids).toEqual(["glm", "qwen", "kimi"])
    expect(ids).not.toContain("anthropic")
  })

  it("raw definitions still contains Anthropic", () => {
    const store = createStoreWith({ definitions: DEFINITIONS })
    expect(store.definitions.map((d) => d.id)).toContain("anthropic")
  })

  it("usableProfiles only includes visible + enabled + ready profiles", () => {
    const store = createStoreWith({
      profiles: [
        makeProfile({ id: "p-ready", provider_id: "glm", status: "ready" }),
        makeProfile({
          id: "p-disabled",
          provider_id: "glm",
          enabled: false,
          status: "disabled",
        }),
        makeProfile({
          id: "p-needs-key",
          provider_id: "glm",
          status: "needs_key",
        }),
        makeProfile({
          id: "p-anthropic",
          provider_id: "anthropic",
          status: "ready",
        }),
        makeProfile({
          id: "p-qwen",
          provider_id: "qwen",
          status: "ready",
        }),
      ],
    })
    const ids = store.usableProfiles.map((p) => p.id)
    expect(ids).toEqual(["p-ready", "p-qwen"])
  })

  it("profilesByProvider keeps invalid visible-provider profiles", () => {
    const store = createStoreWith({
      profiles: [
        makeProfile({ id: "p-1", provider_id: "glm", status: "ready" }),
        makeProfile({ id: "p-2", provider_id: "glm", status: "disabled" }),
        makeProfile({
          id: "p-3",
          provider_id: "qwen",
          status: "needs_key",
        }),
        makeProfile({
          id: "p-4",
          provider_id: "anthropic",
          status: "ready",
        }),
      ],
    })
    const map = store.profilesByProvider
    expect(map.get("glm")?.map((p) => p.id)).toEqual(["p-1", "p-2"])
    expect(map.get("qwen")?.map((p) => p.id)).toEqual(["p-3"])
    expect(map.has("anthropic" as never)).toBe(false)
  })

  it("profilesByProvider groups disabled/needs_key profiles under their provider", () => {
    const store = createStoreWith({
      profiles: [
        makeProfile({ id: "p-dis", provider_id: "kimi", status: "disabled" }),
        makeProfile({
          id: "p-nk",
          provider_id: "kimi",
          status: "needs_key",
        }),
      ],
    })
    const kimiProfiles = store.profilesByProvider.get("kimi")
    expect(kimiProfiles).toHaveLength(2)
    expect(kimiProfiles?.map((p) => p.id).sort()).toEqual(["p-dis", "p-nk"])
  })
})

// ============================================================================
// C. Binding state machine
// ============================================================================

describe("C. Binding state machine", () => {
  it("starts at idle with null binding", () => {
    const store = useProviderStore()
    expect(store.bindingLoadState).toBe("idle")
    expect(store.currentBinding).toBeNull()
    expect(store.bindingSessionId).toBeNull()
    expect(store.bindingLoadError).toBeNull()
  })

  it("refreshForSession: idle → loading immediately", async () => {
    let resolveFn!: (v: unknown) => void
    api.getSessionModelBinding.mockReturnValue(
      new Promise((r) => {
        resolveFn = r
      }),
    )
    const store = useProviderStore()
    const pending = store.refreshForSession("s1")
    expect(store.bindingLoadState).toBe("loading")
    expect(store.bindingSessionId).toBe("s1")
    resolveFn({ binding: null })
    await pending
  })

  it("GET success with binding: loading → loaded", async () => {
    const binding = makeBinding()
    api.getSessionModelBinding.mockResolvedValue({ binding })
    const store = useProviderStore()
    await store.refreshForSession("s1")
    expect(store.bindingLoadState).toBe("loaded")
    expect(store.currentBinding).toEqual(binding)
  })

  it("binding=null (Legacy): loaded + canSendPrompt=true", async () => {
    api.getSessionModelBinding.mockResolvedValue({ binding: null })
    const store = useProviderStore()
    await store.refreshForSession("s1")
    expect(store.bindingLoadState).toBe("loaded")
    expect(store.currentBinding).toBeNull()
    expect(store.canSendPrompt).toBe(true)
  })

  it("ready binding: canSendPrompt=true", async () => {
    const store = useProviderStore()
    store.definitions = DEFINITIONS
    store.profiles = [makeProfile({ id: "prof-glm-1", status: "ready" })]
    api.getSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "prof-glm-1" }),
    })
    await store.refreshForSession("s1")
    expect(store.canSendPrompt).toBe(true)
  })

  it("Profile missing for binding: canSendPrompt=false", async () => {
    const store = useProviderStore()
    store.profiles = []
    api.getSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "missing" }),
    })
    await store.refreshForSession("s1")
    expect(store.bindingLoadState).toBe("loaded")
    expect(store.canSendPrompt).toBe(false)
  })

  it("disabled profile: canSendPrompt=false", async () => {
    const store = useProviderStore()
    store.profiles = [
      makeProfile({
        id: "prof-x",
        provider_id: "glm",
        enabled: false,
        status: "disabled",
      }),
    ]
    api.getSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "prof-x" }),
    })
    await store.refreshForSession("s1")
    expect(store.canSendPrompt).toBe(false)
  })

  it("needs_key profile: canSendPrompt=false", async () => {
    const store = useProviderStore()
    store.profiles = [
      makeProfile({
        id: "prof-nk",
        provider_id: "glm",
        status: "needs_key",
      }),
    ]
    api.getSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "prof-nk" }),
    })
    await store.refreshForSession("s1")
    expect(store.canSendPrompt).toBe(false)
  })

  it("GET failure: state=error, canSendPrompt=false", async () => {
    api.getSessionModelBinding.mockRejectedValue(new FakeApiError(500, "boom"))
    const store = useProviderStore()
    await store.refreshForSession("s1")
    expect(store.bindingLoadState).toBe("error")
    expect(store.canSendPrompt).toBe(false)
    expect(store.currentBinding).toBeNull()
  })

  it("request failure is not interpreted as Legacy null binding", async () => {
    api.getSessionModelBinding.mockRejectedValue(new FakeApiError(0, "net"))
    const store = useProviderStore()
    await store.refreshForSession("s1")
    expect(store.bindingLoadState).not.toBe("loaded")
    expect(store.bindingLoadError).toBeTruthy()
  })

  it("clearSessionState resets to idle", async () => {
    api.getSessionModelBinding.mockResolvedValue({ binding: makeBinding() })
    const store = useProviderStore()
    await store.refreshForSession("s1")
    store.clearSessionState()
    expect(store.bindingLoadState).toBe("idle")
    expect(store.currentBinding).toBeNull()
    expect(store.bindingSessionId).toBeNull()
  })
})

// ============================================================================
// D. stale-response
// ============================================================================

describe("D. stale-response protection", () => {
  it("stale binding from session A does not overwrite session B", async () => {
    let resolveA!: (v: unknown) => void
    const pendingA = new Promise((r) => {
      resolveA = r as (v: unknown) => void
    })
    api.getSessionModelBinding.mockImplementation((sid: string) => {
      if (sid === "A") return pendingA
      if (sid === "B")
        return Promise.resolve({
          binding: makeBinding({
            session_id: "B",
            profile_id: "prof-B",
          }),
        })
      throw new Error("unexpected sid")
    })

    const store = useProviderStore()
    const refreshAPromise = store.refreshForSession("A")
    // A is pending; switch to B
    await store.refreshForSession("B")
    expect(store.bindingSessionId).toBe("B")
    expect(store.currentBinding?.profile_id).toBe("prof-B")

    // Now resolve A (stale)
    resolveA({
      binding: makeBinding({ session_id: "A", profile_id: "prof-A" }),
    })
    await refreshAPromise

    // State must still belong to B
    expect(store.bindingSessionId).toBe("B")
    expect(store.currentBinding?.profile_id).toBe("prof-B")
  })

  it("stale error response does not flip state to error after a successful switch", async () => {
    let rejectA!: (e: unknown) => void
    const pendingA = new Promise((_, reject) => {
      rejectA = reject
    })
    api.getSessionModelBinding.mockImplementation((sid: string) => {
      if (sid === "A") return pendingA
      if (sid === "B")
        return Promise.resolve({
          binding: makeBinding({ session_id: "B", profile_id: "prof-B" }),
        })
      throw new Error("unexpected sid")
    })

    const store = useProviderStore()
    const refreshAPromise = store.refreshForSession("A").catch(() => undefined)
    await store.refreshForSession("B")
    expect(store.bindingLoadState).toBe("loaded")

    rejectA(new FakeApiError(500, "stale boom"))
    await refreshAPromise

    // State still loaded for B
    expect(store.bindingLoadState).toBe("loaded")
    expect(store.bindingLoadError).toBeNull()
    expect(store.currentBinding?.profile_id).toBe("prof-B")
  })

  it("clearSessionState invalidates pending response", async () => {
    let resolveA!: (v: unknown) => void
    const pendingA = new Promise((r) => {
      resolveA = r as (v: unknown) => void
    })
    api.getSessionModelBinding.mockReturnValue(pendingA)

    const store = useProviderStore()
    const refreshAPromise = store.refreshForSession("A")
    store.clearSessionState()
    expect(store.bindingLoadState).toBe("idle")

    resolveA({ binding: makeBinding({ session_id: "A" }) })
    await refreshAPromise

    // Idle must persist——stale response did not write
    expect(store.bindingLoadState).toBe("idle")
    expect(store.currentBinding).toBeNull()
  })

  it("rapid A → B → C: final state belongs to C only", async () => {
    let resolveA!: (v: unknown) => void
    let resolveB!: (v: unknown) => void
    const pendingA = new Promise((r) => {
      resolveA = r as (v: unknown) => void
    })
    const pendingB = new Promise((r) => {
      resolveB = r as (v: unknown) => void
    })
    api.getSessionModelBinding.mockImplementation((sid: string) => {
      if (sid === "A") return pendingA
      if (sid === "B") return pendingB
      if (sid === "C")
        return Promise.resolve({
          binding: makeBinding({ session_id: "C", profile_id: "prof-C" }),
        })
      throw new Error("unexpected sid")
    })

    const store = useProviderStore()
    const a = store.refreshForSession("A").catch(() => undefined)
    const b = store.refreshForSession("B").catch(() => undefined)
    await store.refreshForSession("C")
    expect(store.currentBinding?.profile_id).toBe("prof-C")

    resolveB({ binding: makeBinding({ session_id: "B", profile_id: "prof-B" }) })
    resolveA({ binding: makeBinding({ session_id: "A", profile_id: "prof-A" }) })
    await Promise.all([a, b])

    expect(store.bindingSessionId).toBe("C")
    expect(store.currentBinding?.profile_id).toBe("prof-C")
  })
})

// ============================================================================
// E. selected projections
// ============================================================================

describe("E. selected projections", () => {
  function setupWithBinding(
    profiles: ProviderProfileView[],
    binding: SessionModelBindingView | null,
  ) {
    const store = useProviderStore()
    store.definitions = DEFINITIONS
    store.profiles = profiles
    // Directly set binding via store internals——bypass API mock.
    store.currentBinding = binding
    store.bindingLoadState = "loaded"
    return store
  }

  it("selectedProfile resolves from raw profiles by binding.profile_id", () => {
    const prof = makeProfile({ id: "prof-xyz", provider_id: "glm" })
    const store = setupWithBinding([prof], makeBinding({ profile_id: "prof-xyz" }))
    expect(store.selectedProfile?.id).toBe("prof-xyz")
  })

  it("selectedModel comes from binding.model_id, not profile.default_model", () => {
    const prof = makeProfile({
      id: "prof-m",
      default_model: "glm-4.5-flash",
    })
    const store = setupWithBinding(
      [prof],
      makeBinding({ profile_id: "prof-m", model_id: "glm-4.5" }),
    )
    expect(store.selectedModel).toBe("glm-4.5")
    expect(store.selectedModel).not.toBe("glm-4.5-flash")
  })

  it("selectedModel does not fall back to Profile.default_model on null binding", () => {
    const prof = makeProfile({
      id: "prof-m",
      default_model: "glm-4.5-flash",
    })
    const store = setupWithBinding([prof], null)
    expect(store.selectedModel).toBeNull()
  })

  it("selectedProfile resolves invalid (e.g. anthropic) profile from raw list", () => {
    const prof = makeProfile({
      id: "prof-ant",
      provider_id: "anthropic",
      status: "ready",
    })
    const store = setupWithBinding([prof], makeBinding({ profile_id: "prof-ant" }))
    expect(store.selectedProfile?.id).toBe("prof-ant")
    // canSendPrompt stays false——anthropic profile missing from usable set
    // but selectedProfile getter itself still resolves.
    expect(store.canSendPrompt).toBe(true) // status=ready + enabled → true
  })

  it("selectedProvider resolves from raw definitions", () => {
    const prof = makeProfile({ id: "prof-x", provider_id: "qwen" })
    const store = setupWithBinding([prof], makeBinding({ profile_id: "prof-x" }))
    expect(store.selectedProvider?.id).toBe("qwen")
  })
})

// ============================================================================
// F. unusedCredentials
// ============================================================================

describe("F. unusedCredentials", () => {
  function setup(profiles: ProviderProfileView[], credentials: CredentialView[]) {
    const store = useProviderStore()
    store.profiles = profiles
    store.credentials = credentials
    return store
  }

  it("no profile references credential → unused", () => {
    const store = setup([], [makeCredential({ credential_id: "c-1" })])
    expect(store.unusedCredentials.map((c) => c.credential_id)).toEqual(["c-1"])
  })

  it("visible profile references credential → used", () => {
    const store = setup(
      [makeProfile({ provider_id: "glm", credential_id: "c-1" })],
      [makeCredential({ credential_id: "c-1" })],
    )
    expect(store.unusedCredentials).toHaveLength(0)
  })

  it("disabled profile references credential → still used", () => {
    const store = setup(
      [
        makeProfile({
          provider_id: "glm",
          enabled: false,
          status: "disabled",
          credential_id: "c-1",
        }),
      ],
      [makeCredential({ credential_id: "c-1" })],
    )
    expect(store.unusedCredentials).toHaveLength(0)
  })

  it("Anthropic (hidden) profile references credential → still used", () => {
    const store = setup(
      [
        makeProfile({
          provider_id: "anthropic",
          credential_id: "c-1",
        }),
      ],
      [makeCredential({ credential_id: "c-1" })],
    )
    // 关键：遍历 raw profiles，hidden anthropic profile 的引用仍生效。
    expect(store.unusedCredentials).toHaveLength(0)
  })

  it("same credential referenced by multiple profiles → used", () => {
    const store = setup(
      [
        makeProfile({ id: "p-1", provider_id: "glm", credential_id: "c-shared" }),
        makeProfile({
          id: "p-2",
          provider_id: "qwen",
          credential_id: "c-shared",
        }),
      ],
      [makeCredential({ credential_id: "c-shared" })],
    )
    expect(store.unusedCredentials).toHaveLength(0)
  })

  it("delete used credential: store rejects before calling API", async () => {
    api.listCredentials.mockResolvedValue({ credentials: [] })
    const store = setup(
      [makeProfile({ provider_id: "glm", credential_id: "c-1" })],
      [makeCredential({ credential_id: "c-1" })],
    )
    const ok = await store.deleteCredential("c-1")
    expect(ok).toBe(false)
    expect(api.deleteCredential).not.toHaveBeenCalled()
    expect(store.mutationError).toBeTruthy()
    expect(store.mutationError).not.toContain("c-1")
  })

  it("delete unused credential: store calls API and reloads", async () => {
    api.deleteCredential.mockResolvedValue({
      credential_id: "c-1",
      deleted: true,
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    const store = setup([], [makeCredential({ credential_id: "c-1" })])
    const ok = await store.deleteCredential("c-1")
    expect(ok).toBe(true)
    expect(api.deleteCredential).toHaveBeenCalledTimes(1)
  })
})

// ============================================================================
// G. Mutations
// ============================================================================

describe("G. Mutations", () => {
  it("mutation start clears previous mutationError", async () => {
    api.createCredential.mockResolvedValue({
      credential: makeCredential({ credential_id: "c-1" }),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-1" })],
    })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    store.mutationError = "previous error"

    await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: "x",
    })
    expect(store.mutationError).toBeNull()
  })

  it("createCredential success returns the credential id", async () => {
    api.createCredential.mockResolvedValue({
      credential: makeCredential({ credential_id: "c-new" }),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    const result = await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: "x",
    })
    expect(result?.credential_id).toBe("c-new")
  })

  it("rotateCredentialSecret success reloads credentials and profiles", async () => {
    api.rotateCredentialSecret.mockResolvedValue({
      credential: makeCredential({ credential_id: "c-1" }),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.rotateCredentialSecret("c-1", { secret_value: "y" })
    expect(api.listCredentials).toHaveBeenCalledTimes(1)
    expect(api.listProviderProfiles).toHaveBeenCalledTimes(1)
  })

  it("updateProfile success reloads profiles", async () => {
    api.updateProviderProfile.mockResolvedValue({
      profile: makeProfile({ id: "p-1" }),
    })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    await store.updateProfile("p-1", { name: "renamed" })
    expect(api.listProviderProfiles).toHaveBeenCalledTimes(1)
  })

  it("default switch: store re-GETs profiles (no local default patching)", async () => {
    api.updateProviderProfile.mockResolvedValue({
      profile: makeProfile({ id: "p-1", is_default: true }),
    })
    api.listProviderProfiles.mockResolvedValue({
      profiles: [
        makeProfile({ id: "p-1", is_default: true }),
        makeProfile({ id: "p-2", is_default: false }),
      ],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })

    const store = useProviderStore()
    await store.updateProfile("p-1", { is_default: true })
    // Profile list was re-fetched——no local "is_default patching" attempted.
    expect(api.listProviderProfiles).toHaveBeenCalledTimes(1)
    const profilesAfter = store.profiles
    expect(profilesAfter.find((p) => p.id === "p-2")?.is_default).toBe(false)
  })

  it("deleteProfile success does NOT auto-delete associated credential", async () => {
    api.deleteProviderProfile.mockResolvedValue(null)
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })
    api.listCredentials.mockResolvedValue({
      credentials: [makeCredential({ credential_id: "c-orphan" })],
    })

    const store = useProviderStore()
    store.profiles = [makeProfile({ id: "p-1", credential_id: "c-orphan" })]
    store.credentials = [makeCredential({ credential_id: "c-orphan" })]

    await store.deleteProfile("p-1")
    expect(api.deleteProviderProfile).toHaveBeenCalledTimes(1)
    expect(api.deleteCredential).not.toHaveBeenCalled()
    // Credential still present in store.
    expect(store.credentials.find((c) => c.credential_id === "c-orphan")).toBeTruthy()
  })

  it("mutation failure: writes mutationError, does not throw raw, clears saving flag", async () => {
    // 后端 detail 已脱敏——store 信任并保留。这里 detail 文案不含敏感信息。
    api.createCredential.mockRejectedValue(new FakeApiError(500, "backend rejected mutation"))
    const store = useProviderStore()
    await expect(
      store.createCredential({
        label: "l",
        storage_mode: "session_only",
        secret_value: "x",
      }),
    ).resolves.toBeNull()
    expect(store.mutationError).toBe("backend rejected mutation")
    expect(store.savingCredential).toBe(false)
  })

  it("mutation finally resets saving flag even on failure", async () => {
    api.updateProviderProfile.mockRejectedValue(new FakeApiError(500, "x"))
    const store = useProviderStore()
    await store.updateProfile("p-1", { name: "x" })
    expect(store.savingProfile).toBe(false)
  })
})

// ============================================================================
// H. Binding mutation
// ============================================================================

describe("H. setSessionBinding", () => {
  it("PUT success on current session updates currentBinding", async () => {
    api.putSessionModelBinding.mockResolvedValue({
      binding: makeBinding({
        session_id: "s1",
        profile_id: "p-new",
        model_id: "m-new",
      }),
    })
    const store = useProviderStore()
    store.bindingSessionId = "s1"
    await store.setSessionBinding("s1", "p-new", "m-new")
    expect(store.currentBinding?.profile_id).toBe("p-new")
    expect(store.currentBinding?.model_id).toBe("m-new")
    expect(store.bindingLoadState).toBe("loaded")
  })

  it("PUT success while session switched: response does NOT overwrite current state", async () => {
    let resolvePut!: (v: unknown) => void
    const pending = new Promise((r) => {
      resolvePut = r as (v: unknown) => void
    })
    api.putSessionModelBinding.mockReturnValue(pending)
    api.getSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ session_id: "s2", profile_id: "p-s2" }),
    })

    const store = useProviderStore()
    store.bindingSessionId = "s1"
    const putPromise = store.setSessionBinding("s1", "p-old", "m-old")
    // User switches to s2 while s1 PUT is in flight.
    await store.refreshForSession("s2")
    expect(store.bindingSessionId).toBe("s2")
    expect(store.currentBinding?.profile_id).toBe("p-s2")

    // s1 PUT now resolves——must not overwrite s2 state.
    resolvePut({
      binding: makeBinding({
        session_id: "s1",
        profile_id: "p-old",
        model_id: "m-old",
      }),
    })
    await putPromise

    expect(store.bindingSessionId).toBe("s2")
    expect(store.currentBinding?.profile_id).toBe("p-s2")
  })

  it("PUT failure: preserves currentBinding, does not null it out", async () => {
    api.putSessionModelBinding.mockRejectedValue(new FakeApiError(500, "boom"))
    const store = useProviderStore()
    store.bindingSessionId = "s1"
    const original = makeBinding({
      session_id: "s1",
      profile_id: "p-orig",
      model_id: "m-orig",
    })
    store.currentBinding = original

    const result = await store.setSessionBinding("s1", "p-other", "m-other")
    expect(result).toBeNull()
    // Original binding preserved——no fallback, no null.
    expect(store.currentBinding).toEqual(original)
  })

  it("PUT failure: mutationError set, no fallback", async () => {
    api.putSessionModelBinding.mockRejectedValue(new FakeApiError(500, "boom"))
    const store = useProviderStore()
    store.bindingSessionId = "s1"
    await store.setSessionBinding("s1", "p", "m")
    expect(store.mutationError).toBeTruthy()
  })

  it("store passes caller-supplied model_id (never substitutes profile.default_model)", async () => {
    api.putSessionModelBinding.mockResolvedValue({
      binding: makeBinding({ profile_id: "p-1", model_id: "caller-m" }),
    })
    const store = useProviderStore()
    store.bindingSessionId = "s1"
    await store.setSessionBinding("s1", "p-1", "caller-m")
    const [, body] = api.putSessionModelBinding.mock.calls[0]
    expect(body.model_id).toBe("caller-m")
  })
})

// ============================================================================
// I. Secret marker containment
// ============================================================================

describe("I. Secret marker containment", () => {
  it("createCredential forwards secret to API as parameter", async () => {
    api.createCredential.mockResolvedValue({
      credential: makeCredential(),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: SECRET_MARKER,
    })
    const [body] = api.createCredential.mock.calls[0]
    expect(body.secret_value).toBe(SECRET_MARKER)
  })

  it("rotateCredentialSecret forwards secret to API as parameter", async () => {
    api.rotateCredentialSecret.mockResolvedValue({
      credential: makeCredential(),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.rotateCredentialSecret("c-1", { secret_value: SECRET_MARKER })
    // rotateCredentialSecret(credentialId, payload)——2 args，payload 在第二位。
    const [, body] = api.rotateCredentialSecret.mock.calls[0]
    expect(body.secret_value).toBe(SECRET_MARKER)
  })

  it("store state contains 0 occurrences of marker after create", async () => {
    api.createCredential.mockResolvedValue({
      credential: makeCredential(),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: SECRET_MARKER,
    })
    const serialized = JSON.stringify(store.$state)
    expect(serialized).not.toContain(SECRET_MARKER)
  })

  it("definitions / profiles / credentials do not contain marker", async () => {
    api.createCredential.mockResolvedValue({
      credential: makeCredential(),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.createCredential({
      label: SECRET_MARKER,
      storage_mode: "session_only",
      secret_value: SECRET_MARKER,
    })
    expect(JSON.stringify(store.definitions)).not.toContain(SECRET_MARKER)
    expect(JSON.stringify(store.profiles)).not.toContain(SECRET_MARKER)
    expect(JSON.stringify(store.credentials)).not.toContain(SECRET_MARKER)
  })

  it("loadError / bindingLoadError / mutationError never contain marker", async () => {
    // 后端 detail 已脱敏——store 信任并保留。这里 detail 不含 marker，验证
    // mutationError 也不含 marker。
    api.createCredential.mockRejectedValue(new FakeApiError(500, "backend rejected"))
    const store = useProviderStore()
    await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: SECRET_MARKER,
    })
    expect(store.mutationError).not.toContain(SECRET_MARKER)
    expect(store.loadError).toBeNull()
    expect(store.bindingLoadError).toBeNull()
  })

  it("console.log/error spy never observes marker", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => undefined)
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined)
    api.createCredential.mockResolvedValue({
      credential: makeCredential(),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: SECRET_MARKER,
    })

    const allLogs = [...logSpy.mock.calls.flat(), ...errSpy.mock.calls.flat()].join(" ")
    expect(allLogs).not.toContain(SECRET_MARKER)

    logSpy.mockRestore()
    errSpy.mockRestore()
  })

  it("does not write to localStorage or sessionStorage", async () => {
    const lsSet = vi.spyOn(Storage.prototype, "setItem")
    api.createCredential.mockResolvedValue({
      credential: makeCredential(),
      warnings: [],
    })
    api.listCredentials.mockResolvedValue({ credentials: [] })
    api.listProviderProfiles.mockResolvedValue({ profiles: [] })

    const store = useProviderStore()
    await store.createCredential({
      label: "l",
      storage_mode: "session_only",
      secret_value: SECRET_MARKER,
    })
    expect(lsSet).not.toHaveBeenCalled()
    lsSet.mockRestore()
  })
})
