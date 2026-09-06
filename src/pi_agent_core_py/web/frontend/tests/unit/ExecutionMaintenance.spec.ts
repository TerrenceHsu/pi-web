import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

const { requestJson } = vi.hoisted(() => ({ requestJson: vi.fn() }))
vi.mock("../../src/api/client", () => ({ requestJson }))
import ExecutionAdmin from "../../src/components/telemetry/ExecutionAdmin.vue"
import ExecutionHistory from "../../src/components/workspace/ExecutionHistory.vue"

const task = {
  task_id: "t1", account_id: "a1", session_id: "s1", kind: "coding", backend: "local_docker",
  runtime_id: "sha256:pinned", expires_ms: 123, stop_requested: false, released: false,
  error_code: null, phase: "active", commands_used: 3, max_commands: 50,
  charged_ms: 1500, max_execution_ms: 600000, state: "active", cleanup_pending: false,
}
const status = {
  quota: { account_tasks: 2, global_tasks: 4, cpu: 8, memory_mb: 8192 },
  active_tasks: 1, cpu: 1, memory_mb: 1024, cleanup_pending: 1, admission_paused: true,
  last_cleanup_ms: 1, error_code: null, cache: { bytes: 128, removed_files: 2 }, tasks: [task],
}
beforeEach(() => {
  requestJson.mockReset()
  requestJson.mockImplementation(async (path: string) => {
    if (path.endsWith("/status")) return status
    if (path.includes("/telemetry/spans")) return { spans: [] }
    if (path.endsWith("/probe")) return { environment_ready: true }
    if (path.endsWith("/execution-tasks")) return { tasks: [task] }
    if (path.endsWith("/t1")) return { ...task, logs: [{ command_id: "cmd", input: "<script>private</script>", result: null }] }
    return { stop_requested: true }
  })
})

describe("execution administration", () => {
  it("shows cleanup debt and revokes without issuing an execute or publish request", async () => {
    const wrapper = mount(ExecutionAdmin)
    await flushPromises()
    expect(wrapper.text()).toContain("New execution paused")
    expect(wrapper.text()).toContain("Per account 2")
    expect(wrapper.text()).toContain("3/50 commands")
    const revoke = wrapper.findAll("button").find(b => b.text() === "Revoke execution")!
    await revoke.trigger("click")
    await flushPromises()
    expect(requestJson).toHaveBeenCalledWith("/api/admin/local-execution/tasks/t1/revoke", { method: "POST" })
    expect(requestJson.mock.calls.some(([path]) => /publish|execute$/.test(path))).toBe(false)
    wrapper.unmount()
  })
  it("does not present a daemon probe as execution authorization", async () => {
    const wrapper = mount(ExecutionAdmin)
    await flushPromises()
    await wrapper.findAll("button").find(b => b.text() === "Probe daemon / image")!.trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("does not authorize execution or certify isolation")
    wrapper.unmount()
  })
  it("renders private input as text and clears it when the Workspace changes", async () => {
    const wrapper = mount(ExecutionHistory, { props: { sessionId: "s1" } })
    await flushPromises()
    await wrapper.findAll("button").find(b => b.text() === "coding · active")!.trigger("click")
    await flushPromises()
    expect(wrapper.find("pre").text()).toContain("<script>private</script>")
    expect(wrapper.find("script").exists()).toBe(false)
    await wrapper.setProps({ sessionId: "s2" })
    await flushPromises()
    expect(wrapper.find("pre").exists()).toBe(false)
    expect(requestJson).toHaveBeenCalledWith("/api/sessions/s2/execution-tasks")
    wrapper.unmount()
  })
  it("does not retain private data from a late old-Workspace response", async () => {
    let resolve: (value: unknown) => void = () => undefined
    requestJson.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const wrapper = mount(ExecutionHistory, { props: { sessionId: "s1" } })
    await wrapper.setProps({ sessionId: "s2" })
    await flushPromises()
    resolve({ tasks: [{ ...task, kind: "OLD PRIVATE TASK" }] })
    await flushPromises()
    expect(wrapper.text()).not.toContain("OLD PRIVATE TASK")
    wrapper.unmount()
  })
})
