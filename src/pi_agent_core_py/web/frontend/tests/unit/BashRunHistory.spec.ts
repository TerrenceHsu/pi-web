import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

const request = vi.hoisted(() => vi.fn())
vi.mock("../../src/api/client", () => ({ requestJson: request }))
import BashRunHistory from "../../src/components/workspace/BashRunHistory.vue"

beforeEach(() => request.mockReset())
describe("Bash history", () => {
  it("only reads persisted runs and shows untrusted script/output as text", async () => {
    request.mockResolvedValueOnce({ runs: [{ run_id: "run1", status: "failed", cwd: ".", created_at_ms: 1 }] })
    const wrapper = mount(BashRunHistory, { props: { sessionId: "one" } })
    await flushPromises()
    request.mockResolvedValueOnce({ script: "<script>unsafe()</script>", result: { stdout: "failed", exit_code: 7 } })
    await wrapper.get("li button").trigger("click")
    await flushPromises()
    expect(wrapper.get('[data-testid="bash-run-detail"]').text()).toContain("<script>unsafe()</script>")
    expect(wrapper.find("script").exists()).toBe(false)
    expect(request.mock.calls.every(call => call.length === 1)).toBe(true)
    request.mockResolvedValueOnce({ runs: [] })
    await wrapper.get("h3 button").trigger("click")
    await flushPromises()
    expect(wrapper.find('[data-testid="bash-run-detail"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it("ignores detail from a previous Workspace", async () => {
    request.mockResolvedValueOnce({ runs: [{ run_id: "run1", status: "succeeded", cwd: ".", created_at_ms: 1 }] })
    const wrapper = mount(BashRunHistory, { props: { sessionId: "one" } })
    await flushPromises()
    let finish: (value: unknown) => void = () => {}
    request.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    await wrapper.get("li button").trigger("click")
    request.mockResolvedValueOnce({ runs: [] })
    await wrapper.setProps({ sessionId: "two" })
    finish({ script: "private previous workspace" })
    await flushPromises()
    expect(wrapper.text()).not.toContain("private previous workspace")
    wrapper.unmount()
  })
})
