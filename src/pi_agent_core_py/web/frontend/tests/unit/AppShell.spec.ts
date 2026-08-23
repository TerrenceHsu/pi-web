import { mount } from "@vue/test-utils"
import { describe, expect, it } from "vitest"

import AppShell from "../../src/components/layout/AppShell.vue"

describe("AppShell Workspace layout", () => {
  it("renders the third column and controls its narrow-screen drawer state", async () => {
    const wrapper = mount(AppShell, {
      props: { workspaceAttention: true },
      slots: {
        sidebar: "Sessions",
        main: "Chat",
        workspace: "Latest result",
      },
    })

    expect(wrapper.get("[data-testid='workspace-sidebar']").text()).toContain("Latest result")
    const trigger = wrapper.get("[data-testid='workspace-drawer-trigger']")
    expect(trigger.attributes("aria-expanded")).toBe("false")
    expect(trigger.find("[aria-label='New Agent result']").exists()).toBe(true)

    await trigger.trigger("click")
    expect(trigger.attributes("aria-expanded")).toBe("true")
    expect(wrapper.get("[data-testid='workspace-sidebar']").classes()).toContain("open")
    expect(wrapper.emitted("workspace-opened")).toHaveLength(1)

    await wrapper.get("button[aria-label='Close Workspace results']").trigger("click")
    expect(trigger.attributes("aria-expanded")).toBe("false")
  })
})
