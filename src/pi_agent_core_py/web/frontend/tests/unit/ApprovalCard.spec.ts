import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"

import ApprovalCard from "../../src/components/chat/ApprovalCard.vue"
import { useChatStore } from "../../src/stores/chatStore"
import type { ToolApprovalItem } from "../../src/types"

function pendingItem(): ToolApprovalItem {
  return {
    kind: "tool_approval",
    id: "approval-item-1",
    approvalId: "approval-1",
    requestId: "req-1",
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "write_file",
    toolLabel: "Write file",
    arguments: { filename: "report.md", content: "hello" },
    reason: "write operation requires confirmation",
    policyName: "default",
    status: "pending",
    createdAt: "2026-08-16T00:00:00Z",
    resolvedAt: null,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe("ApprovalCard", () => {
  it("shows complete Python code as text without executing or auto-approving", () => {
    const resolve = vi.spyOn(useChatStore(), "resolveToolApproval").mockResolvedValue()
    const code = "# <script>alert(1)</script>\n" + "# review this line\n".repeat(80) + "print('end')"
    const wrapper = mount(ApprovalCard, { props: { item: {
      ...pendingItem(), policyName: "python_execution", toolName: "run_python_analysis",
      arguments: { file_id: "file-a", code, source_name: "sales.csv" },
    } } })
    expect(wrapper.get('[data-testid="approval-python-code"]').text()).toBe(code)
    expect(wrapper.find("script").exists()).toBe(false)
    expect(resolve).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it("shows tool arguments and approves exactly once", async () => {
    const store = useChatStore()
    const resolve = vi.spyOn(store, "resolveToolApproval").mockResolvedValue()
    const wrapper = mount(ApprovalCard, { props: { item: pendingItem() } })

    expect(wrapper.get('[data-testid="approval-tool-name"]').text()).toBe("Write file")
    expect(wrapper.get('[data-testid="approval-arguments"]').text()).toContain(
      '"filename": "report.md"',
    )
    await wrapper.get('[data-testid="approval-approve"]').trigger("click")
    await flushPromises()

    expect(resolve).toHaveBeenCalledOnce()
    expect(resolve).toHaveBeenCalledWith("approval-1", "approve")
  })

  it("offers deny and hides actions after resolution", async () => {
    const store = useChatStore()
    const resolve = vi.spyOn(store, "resolveToolApproval").mockResolvedValue()
    const item = pendingItem()
    const wrapper = mount(ApprovalCard, { props: { item } })

    await wrapper.get('[data-testid="approval-deny"]').trigger("click")
    await flushPromises()
    expect(resolve).toHaveBeenCalledWith("approval-1", "deny")

    await wrapper.setProps({ item: { ...item, status: "denied" } })
    expect(wrapper.attributes("data-status")).toBe("denied")
    expect(wrapper.find('[data-testid="approval-approve"]').exists()).toBe(false)
    expect(wrapper.text()).toContain("Denied")
  })
})
