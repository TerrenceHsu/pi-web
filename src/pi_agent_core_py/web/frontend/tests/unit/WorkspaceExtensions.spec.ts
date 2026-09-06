import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

const api = vi.hoisted(() => ({
  getWorkspaceExtensions: vi.fn(),
  updateWorkspaceExtensions: vi.fn(),
}))
vi.mock("../../src/api/workspaceExtensions", () => api)

import WorkspaceExtensions from "../../src/components/workspace/WorkspaceExtensions.vue"
import { useSessionStore } from "../../src/stores/sessionStore"
import type { WorkspaceExtensionsResponse } from "../../src/types"

function snapshot(selected = false): WorkspaceExtensionsResponse {
  return {
    session_id: "sess-1",
    configured: selected,
    mcp_servers: [
      {
        name: "ddgs",
        transport: "stdio",
        command: "python",
        args: [],
        enabled: true,
        attached: true,
        last_error: null,
        tool_count: 2,
        env_keys: [],
        builtin: true,
        deletable: false,
        available: true,
        selected,
      },
    ],
    skills: [
      {
        name: "review",
        description: "Review code",
        status: "enabled",
        priority: 100,
        tags: [],
        tool_names: [],
        metadata: {},
        available: true,
        selected,
      },
    ],
    selected_mcp_server_names: selected ? ["ddgs"] : [],
    selected_skill_names: selected ? ["review"] : [],
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  api.getWorkspaceExtensions.mockReset()
  api.updateWorkspaceExtensions.mockReset()
  api.getWorkspaceExtensions.mockResolvedValue(snapshot(false))
  api.updateWorkspaceExtensions.mockResolvedValue(snapshot(true))
  const sessions = useSessionStore()
  sessions.activeSessionId = "sess-1"
})

describe("Workspace extensions", () => {
  it("enables the optional tool without changing MCP or Skill selections", async () => {
    api.getWorkspaceExtensions.mockResolvedValue({ ...snapshot(true),
      tools: [{ name: "analyze_data", label: "Data Analysis", available: true, selected: false }],
      selected_tool_names: [],
    })
    const wrapper = mount(WorkspaceExtensions)
    await flushPromises()
    await wrapper.get('[data-testid="workspace-tool-analyze_data"]').setValue(true)
    await flushPromises()
    expect(api.updateWorkspaceExtensions).toHaveBeenCalledWith("sess-1", {
      mcp_server_names: ["ddgs"], skill_names: ["review"], tool_names: ["analyze_data"],
    })
    wrapper.unmount()
  })

  it("loads the global catalog and persists a Workspace MCP choice", async () => {
    const wrapper = mount(WorkspaceExtensions)
    await flushPromises()

    expect(api.getWorkspaceExtensions).toHaveBeenCalledWith("sess-1")
    expect(wrapper.text()).toContain("ddgs")
    const checkboxes = wrapper.findAll('input[type="checkbox"]')
    await checkboxes[0].setValue(true)
    await flushPromises()

    expect(api.updateWorkspaceExtensions).toHaveBeenCalledWith("sess-1", {
      mcp_server_names: ["ddgs"],
      skill_names: [],
      tool_names: [],
    })
  })
})
