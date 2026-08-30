import { mount } from "@vue/test-utils"
import { describe, expect, it } from "vitest"

import AppShell from "../../src/components/layout/AppShell.vue"

describe("AppShell Workspace layout", () => {
  it("renders Workspace between sessions and chat with adjustable columns", async () => {
    localStorage.clear()
    const wrapper = mount(AppShell, {
      props: { workspaceAttention: true },
      slots: {
        sidebar: "Sessions",
        main: "Chat",
        workspace: "Latest result",
      },
    })

    const shellChildren = Array.from(wrapper.get(".app-shell").element.children)
    expect(
      shellChildren.indexOf(wrapper.get("[data-testid='session-sidebar']").element),
    ).toBeLessThan(shellChildren.indexOf(wrapper.get("[data-testid='workspace-sidebar']").element))
    expect(
      shellChildren.indexOf(wrapper.get("[data-testid='workspace-sidebar']").element),
    ).toBeLessThan(shellChildren.indexOf(wrapper.get("[data-testid='chat-panel']").element))

    await wrapper.get("[data-testid='sidebar-resizer']").trigger("keydown", {
      key: "ArrowRight",
    })
    await wrapper.get("[data-testid='workspace-resizer']").trigger("keydown", {
      key: "ArrowLeft",
    })
    expect(wrapper.get(".app-shell").attributes("style")).toContain("--sidebar-width: 276px")
    expect(wrapper.get(".app-shell").attributes("style")).toContain("--workspace-width: 364px")

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
