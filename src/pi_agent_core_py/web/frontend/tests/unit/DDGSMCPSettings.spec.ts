import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"

import MCPServerList from "../../src/components/mcp/MCPServerList.vue"
import { useMcpStore } from "../../src/stores/mcpStore"
import type { MCPServerSummary } from "../../src/types"

function ddgsServer(): MCPServerSummary {
  return {
    name: "ddgs",
    command: "python",
    args: [],
    enabled: false,
    last_error: null,
    tool_count: 0,
    env_keys: [],
    builtin: true,
    deletable: false,
    settings: {
      max_results: 5,
      region: "wt-wt",
      safesearch: "moderate",
      timelimit: null,
      timeout_seconds: 10,
      backend: "auto",
    },
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe("built-in DDGS MCP settings", () => {
  it("pins DDGS in the server list and does not render a delete action", () => {
    const store = useMcpStore()
    store.servers = [ddgsServer()]

    const wrapper = mount(MCPServerList)

    expect(wrapper.text()).toContain("built-in · fixed")
    expect(wrapper.find('[data-testid="ddgs-settings-form"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="mcp-server-delete-btn"]').exists()).toBe(false)
  })

  it("submits editable result count, region, safety, time and backend parameters", async () => {
    const store = useMcpStore()
    const server = ddgsServer()
    store.servers = [server]
    const update = vi.spyOn(store, "updateDDGSSettings").mockResolvedValue(server)
    const wrapper = mount(MCPServerList)

    await wrapper.get('[data-testid="ddgs-max-results"]').setValue("11")
    await wrapper.get('[data-testid="ddgs-region"]').setValue("cn-zh")
    await wrapper.get('[data-testid="ddgs-safesearch"]').setValue("off")
    await wrapper.get('[data-testid="ddgs-timelimit"]').setValue("w")
    await wrapper.get('[data-testid="ddgs-timeout"]').setValue("18")
    await wrapper.get('[data-testid="ddgs-backend"]').setValue("duckduckgo")
    await wrapper.get('[data-testid="ddgs-settings-form"]').trigger("submit")
    await flushPromises()

    expect(update).toHaveBeenCalledWith({
      max_results: 11,
      region: "cn-zh",
      safesearch: "off",
      timelimit: "w",
      timeout_seconds: 18,
      backend: "duckduckgo",
    })
  })
})
