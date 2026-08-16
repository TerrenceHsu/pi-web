import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

import ContextBudgetBadge from "../../src/components/chat/ContextBudgetBadge.vue"
import type { ContextBudgetResponse, ContextBudgetLevel } from "../../src/types"

const api = vi.hoisted(() => ({
  getContextBudget: vi.fn(),
  estimateContextBudget: vi.fn(),
  compactContext: vi.fn(),
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

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((mock) => mock.mockReset())
})

describe("ContextBudgetBadge", () => {
  it("renders approximate percent and threshold styles", async () => {
    const wrapper = mount(ContextBudgetBadge, { props: { budget: budget("warning", 0.70) } })
    expect(wrapper.get('[data-testid="context-budget-label"]').text()).toBe("Context ~70%")
    expect(wrapper.get(".context-budget").classes()).toContain("level-warning")
    expect(wrapper.find('[data-testid="context-compact-button"]').exists()).toBe(false)

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
  })
})
