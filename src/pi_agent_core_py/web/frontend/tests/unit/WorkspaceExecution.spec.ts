import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

const request = vi.hoisted(() => vi.fn())
vi.mock("../../src/api/client", () => ({ requestJson: request }))
import WorkspaceExecution from "../../src/components/workspace/WorkspaceExecution.vue"

const snapshot = {
  backend: "e2b", revision: 2, backends: [
    { id: "disabled", available: true, label: "Disabled" },
    { id: "e2b", available: true, label: "Remote E2B" },
    { id: "local_docker", available: false, label: "Local Docker" },
  ],
}
beforeEach(() => request.mockReset())

describe("WorkspaceExecution", () => {
  it("only reads capability on mount; selecting uses a revision, never an execution approval", async () => {
    request.mockResolvedValueOnce(snapshot)
    const wrapper = mount(WorkspaceExecution, { props: { sessionId: "one" } })
    await flushPromises()
    expect(request).toHaveBeenCalledOnce()
    expect(request).toHaveBeenCalledWith("/api/workspaces/one/execution")
    expect(wrapper.get('option[value="local_docker"]').attributes("disabled")).toBeDefined()
    request.mockResolvedValueOnce({ ...snapshot, backend: "disabled", revision: 3 })
    await wrapper.get("select").setValue("disabled")
    await flushPromises()
    expect(request).toHaveBeenLastCalledWith("/api/workspaces/one/execution", {
      method: "PUT", body: { backend: "disabled", expected_revision: 2 },
    })
    expect(wrapper.text()).toContain("revokes current execution")
    wrapper.unmount()
  })

  it("ignores a previous Workspace's late capability response", async () => {
    let finish: (value: typeof snapshot) => void = () => {}
    request.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const wrapper = mount(WorkspaceExecution, { props: { sessionId: "one" } })
    request.mockResolvedValueOnce({ ...snapshot, backend: "disabled" })
    await wrapper.setProps({ sessionId: "two" })
    await flushPromises()
    finish(snapshot)
    await flushPromises()
    expect((wrapper.get("select").element as HTMLSelectElement).value).toBe("disabled")
    wrapper.unmount()
  })
})
