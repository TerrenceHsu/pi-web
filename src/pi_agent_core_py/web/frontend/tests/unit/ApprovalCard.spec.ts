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
  it.each(["coding", "plan"])("shows shared Bash scope for %s without a second script approval", async (kind) => {
    const resolve = vi.spyOn(useChatStore(), "resolveToolApproval").mockResolvedValue()
    const wrapper = mount(ApprovalCard, { props: { item: {
      ...pendingItem(), policyName: "execution_task", toolName: "execution_task", arguments: {
        kind, task_bash_enabled: true, backend: "local_docker", data_location: "local Docker",
      },
    } } })
    expect(wrapper.get('[data-testid="execution-scope-warning"]').text()).toContain("share this task's copy and budget")
    expect(wrapper.text()).toContain("Fixed validation and freeze still apply")
    expect(wrapper.find('[data-testid="approval-bash-script"]').exists()).toBe(false)
    expect(resolve).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="approval-approve"]').trigger("click")
    await flushPromises()
    expect(resolve).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
  it("shows the complete Bash script and exact scope without automatic approval", async () => {
    const resolve = vi.spyOn(useChatStore(), "resolveToolApproval").mockResolvedValue()
    const script = "# <script>unsafe()</script>\n" + "# 审阅\n".repeat(1500) + "echo end"
    const wrapper = mount(ApprovalCard, { props: { item: {
      ...pendingItem(), policyName: "execution_task", arguments: {
        kind: "bash", script, script_sha256: "f".repeat(64), cwd: "upload", timeout_seconds: 60,
        backend: "local_docker", data_location: "local Docker", workspace_revision: 3,
      },
    } } })
    expect(wrapper.get('[data-testid="approval-bash-script"]').text()).toBe(script)
    expect(wrapper.find("script").exists()).toBe(false)
    expect(wrapper.text()).toContain("Copy file changes are discarded")
    expect(resolve).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="approval-approve"]').text()).toContain("exact Bash script once")
    await wrapper.get('[data-testid="approval-approve"]').trigger("click")
    await flushPromises()
    expect(resolve).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
  it("displays a full execution scope and requires one explicit combined Plan approval", async () => {
    const resolve = vi.spyOn(useChatStore(), "resolveToolApproval").mockResolvedValue()
    const goal = "review ".repeat(100) + "<script>unsafe()</script>"
    const wrapper = mount(ApprovalCard, { props: { item: {
      ...pendingItem(), policyName: "execution_task", toolName: "execution_task",
      arguments: { kind: "plan", goal, plan_version: 3, backend: "e2b",
        data_location: "remote E2B", workspace_revision: 7,
        input_paths: ["upload/private.csv"], scope_sha256: "a".repeat(64) },
    } } })
    expect(wrapper.get('[data-testid="approval-arguments"]').text()).toContain(goal)
    expect(wrapper.get('[data-testid="execution-scope-warning"]').text()).toContain("remote E2B")
    expect(wrapper.find("script").exists()).toBe(false)
    expect(resolve).not.toHaveBeenCalled()
    const button = wrapper.get('[data-testid="approval-approve"]')
    expect(button.text()).toContain("Approve this plan and isolated execution")
    await button.trigger("click")
    await flushPromises()
    expect(resolve).toHaveBeenCalledOnce()
    expect(resolve).toHaveBeenCalledWith("approval-1", "approve")
    wrapper.unmount()
  })

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
