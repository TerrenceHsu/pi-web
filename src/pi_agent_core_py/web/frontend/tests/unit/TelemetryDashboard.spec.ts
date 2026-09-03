import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

const api = vi.hoisted(() => ({
  getTelemetrySummary: vi.fn(),
  listTelemetrySpans: vi.fn(),
  getTelemetrySpan: vi.fn(),
}))

vi.mock("../../src/api/telemetry", () => api)

import TelemetryDashboard from "../../src/components/telemetry/TelemetryDashboard.vue"

const span = {
  id: "span-1",
  trace_id: "trace-1",
  parent_id: null,
  name: "web.request",
  started_at_ms: 1_700_000_000_000,
  ended_at_ms: 1_700_000_001_250,
  duration_ms: 1250,
  status: "ok" as const,
  error: null,
  attributes: {
    account_name: "alice",
    provider: "openai",
    model: "gpt-test",
    execution_mode: "direct",
    outcome: "completed",
    input_tokens: 120,
    output_tokens: 30,
    tool_calls: 1,
  },
}

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((mock) => mock.mockReset())
  api.getTelemetrySummary.mockResolvedValue({
    since_ms: 1,
    generated_at_ms: 1_700_000_002_000,
    requests: {
      total: 1,
      completed: 1,
      error: 0,
      aborted: 0,
      running: 0,
      error_rate: 0,
      average_duration_ms: 1250,
      p95_duration_ms: 1250,
    },
    usage: { input_tokens: 120, output_tokens: 30, total_tokens: 150, cost: 0.01 },
    tools: { calls: 1, errors: 0, top: [{ name: "read_file", calls: 1 }] },
    providers: [{ name: "openai", requests: 1 }],
    accounts: [{ id: "user-1", name: "alice" }],
    timeline: [
      {
        timestamp_ms: 1_700_000_000_000,
        total: 1,
        completed: 1,
        error: 0,
        aborted: 0,
        running: 0,
      },
    ],
  })
  api.listTelemetrySpans.mockResolvedValue({ spans: [span], count: 1 })
  api.getTelemetrySpan.mockResolvedValue({
    ...span,
    events: [
      {
        name: "model.end",
        timestamp_ms: 1_700_000_000_900,
        attributes: { input_tokens: 120, output_tokens: 30 },
      },
    ],
  })
})

describe("TelemetryDashboard", () => {
  it("renders aggregate health and opens a content-free trace timeline", async () => {
    const wrapper = mount(TelemetryDashboard)
    await flushPromises()

    expect(wrapper.text()).toContain("Agent Telemetry")
    expect(wrapper.text()).toContain("150")
    expect(wrapper.text()).toContain("read_file")
    expect(wrapper.text()).toContain("alice")
    expect(api.getTelemetrySummary).toHaveBeenCalledWith(24)

    await wrapper.get('[data-testid="telemetry-span-row"]').trigger("click")
    await flushPromises()

    expect(api.getTelemetrySpan).toHaveBeenCalledWith("span-1")
    expect(wrapper.get('[data-testid="telemetry-span-detail"]').text()).toContain("model.end")
    expect(wrapper.text()).toContain("Content and credentials are not collected")
    wrapper.unmount()
  })

  it("emits close from the Back to chat action", async () => {
    const wrapper = mount(TelemetryDashboard)
    await wrapper.get('[data-testid="telemetry-back-to-chat"]').trigger("click")
    expect(wrapper.emitted("close")).toEqual([[]])
    wrapper.unmount()
  })
})
