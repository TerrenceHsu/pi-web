import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

import ContextBudgetBadge from "../../src/components/chat/ContextBudgetBadge.vue"
import type { ContextBudgetResponse, ContextBudgetLevel, ContextCompactionStatus } from "../../src/types"

const api = vi.hoisted(() => ({
  getContextBudget: vi.fn(),
  estimateContextBudget: vi.fn(),
  compactContext: vi.fn(),
  getContextCompaction: vi.fn(),
  setContextAutoCompaction: vi.fn(),
}))
vi.mock("../../src/api/contextBudget", () => api)
vi.mock("../../src/api/client", () => ({
  ApiError: class ApiError extends Error { status = 500; detail = "error" },
}))

import { useContextBudgetStore } from "../../src/stores/contextBudgetStore"

function budget(level: ContextBudgetLevel, ratio: number | null = 0.42): ContextBudgetResponse {
  return {
    session_id: "sess-1",
    provider_id: "glm",
    model_id: "glm-test",
    capability_source: ratio === null ? "unknown" : "user",
    workspace_context: null,
    intent: null,
    estimate: {
      system_prompt_tokens: 100,
      message_tokens: 200,
      tool_definition_tokens: 50,
      estimated_input_tokens: 350,
      reserved_output_tokens: 100,
      projected_tokens: 450,
      context_window: ratio === null ? null : 1000,
      input_ratio: ratio,
      projected_ratio: ratio === null ? null : ratio + 0.1,
      level,
      can_send: level !== "blocked",
      approximate: true,
      estimator_version: "mixed-char-v1",
    },
  }
}

function status(overrides: Partial<ContextCompactionStatus> = {}): ContextCompactionStatus {
  return {
    auto_compact: true,
    status: "committed",
    active_projection_id: "ctx-1",
    covered_message_count: 8,
    token_stats: { estimated_input_tokens_before: 12000, estimated_input_tokens_after: 6000 },
    error_code: null,
    can_auto_compact: true,
    circuit_open: false,
    ...overrides,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((mock) => mock.mockReset())
  api.getContextCompaction.mockResolvedValue(status())
})

describe("ContextBudgetBadge", () => {
  it("renders approximate percent and threshold styles", async () => {
    const wrapper = mount(ContextBudgetBadge, { props: { budget: budget("warning", 0.70) } })
    expect(wrapper.get('[data-testid="context-budget-label"]').text()).toBe("Context ~70%")
    expect(wrapper.get(".context-budget").classes()).toContain("level-warning")
    expect(wrapper.find('[data-testid="context-compact-button"]').exists()).toBe(true)

    await wrapper.setProps({ budget: budget("compact", 0.85) })
    expect(wrapper.get('[data-testid="context-compact-button"]').exists()).toBe(true)
  })

  it("does not invent a percentage when the model window is unknown", () => {
    const wrapper = mount(ContextBudgetBadge, { props: { budget: budget("unknown", null) } })
    expect(wrapper.get('[data-testid="context-budget-label"]').text()).toBe("Context unknown")
    expect(wrapper.attributes("title")).toContain("Configure this model's context window")
  })

  it("emits compact from blocked state", async () => {
    const wrapper = mount(ContextBudgetBadge, { props: { budget: budget("blocked", 0.95) } })
    await wrapper.get('[data-testid="context-compact-button"]').trigger("click")
    expect(wrapper.emitted("compact")).toHaveLength(1)
  })

  it("uses the effective budget ratio and labels reserves as estimates", () => {
    const value = budget("warning", 0.5)
    value.estimate.effective_ratio = 0.8
    value.estimate.effective_input_budget = 700
    const wrapper = mount(ContextBudgetBadge, { props: { budget: value } })
    expect(wrapper.get('[data-testid="context-budget-label"]').text()).toBe("Context ~80%")
    expect(wrapper.attributes("title")).toContain("Effective input budget 700")
    expect(wrapper.attributes("title")).toContain("Approximate token estimate")
  })

  it("provides details but hides context management in Knowledge mode", async () => {
    const wrapper = mount(ContextBudgetBadge, { props: { budget: budget("normal") } })
    await wrapper.get('[data-testid="context-details-button"]').trigger("click")
    expect(wrapper.emitted("show-details")).toHaveLength(1)
    await wrapper.setProps({ managementEnabled: false })
    expect(wrapper.find("button").exists()).toBe(false)
  })
})

describe("context budget store", () => {
  it("keeps estimates isolated by session and applies live events", () => {
    const store = useContextBudgetStore()
    store.applyEvent("sess-1", budget("warning", 0.7))
    store.applyEvent("sess-2", {
      ...budget("normal", 0.2),
      provider_id: "qwen",
    })
    expect(store.getBudget("sess-1")?.estimate.level).toBe("warning")
    expect(store.getBudget("sess-2")?.provider_id).toBe("qwen")
  })

  it("uses the post-compaction budget returned by the server", async () => {
    const store = useContextBudgetStore()
    const after = budget("normal", 0.3)
    api.compactContext.mockResolvedValue({
      ok: true,
      session_id: "sess-1",
      summary_message: {},
      source_message_count: 10,
      compacted_message_count: 8,
      retained_message_count: 2,
      snapshots_retained: 3,
      budget: after,
    })
    expect(await store.compact("sess-1")).toBe(true)
    expect(store.getBudget("sess-1")?.estimate.input_ratio).toBe(0.3)
  })

  it("clears all user workspace state on logout", () => {
    const store = useContextBudgetStore()
    store.applyEvent("sess-1", budget("normal"))
    store.resetWorkspace()
    expect(store.getBudget("sess-1")).toBeNull()
    expect(store.getCompaction("sess-1")).toBeNull()
  })

  it("keeps summary details for the same projection but never reuses them for another branch", async () => {
    const store = useContextBudgetStore()
    api.getContextCompaction.mockResolvedValue(status({ summary_text: "Known facts", source_entry_ids: ["entry-1"] }))
    await store.loadCompaction("sess-1")
    store.applyEvent("sess-1", { ...budget("normal"), compaction: status({ status: "failed", error_code: "summary_invalid" }) })
    expect(store.getCompaction("sess-1")?.summary_text).toBe("Known facts")
    expect(store.getCompaction("sess-1")?.error_code).toBe("summary_invalid")
    store.applyEvent("sess-1", { ...budget("normal"), compaction: status({ active_projection_id: "ctx-2" }) })
    expect(store.getCompaction("sess-1")?.summary_text).toBeUndefined()
    expect(store.getCompaction("sess-1")?.source_entry_ids).toBeUndefined()
  })

  it("persists the toggle only after the server accepts it and preserves it on failure", async () => {
    const store = useContextBudgetStore()
    store.applyEvent("sess-1", { ...budget("blocked"), compaction: status() })
    api.setContextAutoCompaction.mockResolvedValue(status({ auto_compact: false, can_auto_compact: false }))
    expect(await store.setAutoCompaction("sess-1", false)).toBe(true)
    expect(api.setContextAutoCompaction).toHaveBeenCalledWith("sess-1", false)
    expect(store.getBudget("sess-1")?.compaction?.can_auto_compact).toBe(false)
    api.setContextAutoCompaction.mockRejectedValue(new Error("offline"))
    expect(await store.setAutoCompaction("sess-1", true)).toBe(false)
    expect(store.getCompaction("sess-1")?.auto_compact).toBe(false)
    expect(store.error).toContain("could not be saved")
  })

  it("retains the old summary when manual compaction fails and allows retry", async () => {
    const store = useContextBudgetStore()
    api.getContextCompaction.mockResolvedValue(status({ summary_text: "Existing summary" }))
    await store.loadCompaction("sess-1")
    api.getContextCompaction.mockResolvedValue(status({ status: "failed", error_code: "no_benefit" }))
    api.compactContext.mockRejectedValue(new Error("failed"))
    expect(await store.compact("sess-1")).toBe(false)
    expect(store.compactingSessionId).toBeNull()
    expect(store.getCompaction("sess-1")?.summary_text).toBe("Existing summary")
    expect(store.getCompaction("sess-1")?.error_code).toBe("no_benefit")
  })

  it("does not restore private summary or estimates from requests that finish after logout", async () => {
    const store = useContextBudgetStore()
    let resolveStatus!: (value: ContextCompactionStatus) => void
    let resolvePreview!: (value: ContextBudgetResponse) => void
    api.getContextCompaction.mockReturnValue(new Promise((resolve) => { resolveStatus = resolve }))
    api.estimateContextBudget.mockReturnValue(new Promise((resolve) => { resolvePreview = resolve }))
    const pendingStatus = store.loadCompaction("sess-1")
    const pendingPreview = store.preview("sess-1", { text: "private" })
    store.resetWorkspace()
    resolveStatus(status({ summary_text: "private" }))
    resolvePreview(budget("normal"))
    expect(await pendingStatus).toBeNull()
    expect(await pendingPreview).toBeNull()
    expect(store.getCompaction("sess-1")).toBeNull()
    expect(store.getBudget("sess-1")).toBeNull()
  })

  it("does not replace a new projection with a stale summary response", async () => {
    const store = useContextBudgetStore()
    store.applyEvent("sess-1", { ...budget("normal"), compaction: status() })
    let resolve!: (value: ContextCompactionStatus) => void
    api.getContextCompaction.mockReturnValue(new Promise((done) => { resolve = done }))
    const pending = store.loadCompaction("sess-1")
    store.applyEvent("sess-1", { ...budget("normal"), compaction: status({ active_projection_id: "ctx-2" }) })
    resolve(status({ summary_text: "stale branch" }))
    expect(await pending).toBeNull()
    expect(store.getCompaction("sess-1")?.active_projection_id).toBe("ctx-2")
  })
})
