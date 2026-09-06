import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { AnalysisResult as Result } from "../../src/api/dataAnalysis"

const api = vi.hoisted(() => ({ saveAnalysis: vi.fn(), loadFiles: vi.fn() }))
vi.mock("../../src/api/dataAnalysis", async importOriginal => ({
  ...await importOriginal<typeof import("../../src/api/dataAnalysis")>(),
  saveAnalysis: api.saveAnalysis,
}))
import AnalysisResult from "../../src/components/workspace/AnalysisResult.vue"
import DataAnalysisCard from "../../src/components/chat/DataAnalysisCard.vue"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useFileStore } from "../../src/stores/fileStore"

function result(): Result {
  return {
    schema_version: "pi-agent-data-analysis/v1", run_id: `analysis-${"a".repeat(32)}`,
    session_id: "one", action: "aggregate", source_name: "sales.csv",
    source_logical_path: "upload/sales.csv", source_sha256: "abc",
    source_rows: 100, analyzed_rows: 100, result_rows: 2, exported_rows: 2, preview_rows: 2,
    limited: false, preview_limited: false, columns: ["Region", "Total"],
    rows: [["East", 40], ["<script>alert(1)</script>", 20]], warnings: [],
    source: {}, has_chart: true, null_policy: "nulls excluded", numeric_policy: "not exact decimal",
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  useSessionStore().activeSessionId = "one"
  vi.spyOn(useFileStore(), "loadFiles").mockImplementation(api.loadFiles)
  api.saveAnalysis.mockReset().mockResolvedValue({ saved: true })
  api.loadFiles.mockReset().mockResolvedValue(undefined)
})

describe("Data analysis results", () => {
  it("shows approved Python code, stdout and errors without allowing failed-result saves", () => {
    const wrapper = mount(AnalysisResult, { props: { result: {
      ...result(), action: "python", code: "print('hello')", stdout: "hello",
      python_error: "KeyError: missing (analysis line 2)", has_chart: false,
    } } })
    expect(wrapper.text()).toContain("print('hello')")
    expect(wrapper.get('[data-testid="analysis-stdout"]').text()).toBe("hello")
    expect(wrapper.get('[role="alert"]').text()).toContain("KeyError")
    expect(wrapper.find('[data-testid="save-analysis"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it("renders computed values safely and saves only on an explicit click", async () => {
    const wrapper = mount(AnalysisResult, { props: { result: result() } })
    expect(wrapper.text()).toContain("East")
    expect(wrapper.find("script").exists()).toBe(false)
    expect(wrapper.find("img").attributes("src")).toMatch(/^\/api\/workspaces\/one\/analysis\//)
    expect(api.saveAnalysis).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="save-analysis"]').trigger("click")
    await flushPromises()
    expect(api.saveAnalysis).toHaveBeenCalledWith("one", result().run_id)
    expect(api.loadFiles).toHaveBeenCalledWith("one")
    expect(wrapper.text()).toContain("Saved to Workspace")
    wrapper.unmount()
  })

  it("does not save or load a chart from a different Workspace", async () => {
    useSessionStore().activeSessionId = "two"
    const wrapper = mount(AnalysisResult, { props: { result: result() } })
    expect(wrapper.find("img").exists()).toBe(false)
    expect(wrapper.get("button").attributes("disabled")).toBeDefined()
    await wrapper.get("button").trigger("click")
    expect(api.saveAnalysis).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it("supports live and persisted tool details and rejects malformed records", async () => {
    const wrapper = mount(DataAnalysisCard, { props: { details: { analysis: result() } } })
    expect(wrapper.find('[data-testid="analysis-result"]').exists()).toBe(true)
    await wrapper.setProps({ details: { details: { analysis: result() } } })
    expect(wrapper.text()).toContain("Source 100 rows")
    await wrapper.setProps({ details: { analysis: { ...result(), rows: ["malformed"] } } })
    expect(wrapper.find('[data-testid="analysis-result"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it("does not mark a new result saved when the previous save finishes late", async () => {
    let finish!: (value: { saved: boolean }) => void
    api.saveAnalysis.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(AnalysisResult, { props: { result: result() } })
    await wrapper.get("button").trigger("click")
    await wrapper.setProps({ result: { ...result(), run_id: `analysis-${"b".repeat(32)}` } })
    finish({ saved: true })
    await flushPromises()
    expect(wrapper.text()).not.toContain("Saved to Workspace")
    expect(wrapper.get("button").attributes("disabled")).toBeUndefined()
    wrapper.unmount()
  })
})
