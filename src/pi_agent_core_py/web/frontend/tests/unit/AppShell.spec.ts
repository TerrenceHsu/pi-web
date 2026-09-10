import { mount } from "@vue/test-utils"
import { nextTick } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import AppShell from "../../src/components/layout/AppShell.vue"

const SIDEBAR_KEY = "pi-agent-layout-sidebar-width"
const WORKSPACE_KEY = "pi-agent-layout-workspace-width"
const originalInnerWidth = Object.getOwnPropertyDescriptor(window, "innerWidth")!
const wrappers: ReturnType<typeof mount<typeof AppShell>>[] = []

function viewport(width: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width })
  window.dispatchEvent(new Event("resize"))
}

function createShell() {
  const wrapper = mount(AppShell, {
    props: { workspaceAttention: true },
    slots: { sidebar: "Sessions", main: "Chat", workspace: "Latest result" },
  })
  wrappers.push(wrapper)
  return wrapper
}

function columnWidth(wrapper: ReturnType<typeof createShell>, column: "sidebar" | "workspace") {
  return Number.parseFloat(
    (wrapper.get(".app-shell").element as HTMLElement).style.getPropertyValue(`--${column}-width`),
  )
}

function pointer(target: EventTarget, type: string, props: Partial<PointerEvent> = {}): void {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.assign(
    event,
    { pointerId: 7, button: 0, buttons: 1, isPrimary: true, clientX: 500 },
    props,
  )
  target.dispatchEvent(event)
}

function pointerCapture(element: Element) {
  const captured = new Set<number>()
  const set = vi.fn((id: number) => captured.add(id))
  const release = vi.fn((id: number) => captured.delete(id))
  Object.assign(element, {
    setPointerCapture: set,
    releasePointerCapture: release,
    hasPointerCapture: (id: number) => captured.has(id),
  })
  return { set, release }
}

beforeEach(() => {
  localStorage.clear()
  viewport(1600)
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => window.innerWidth)
})

afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  vi.restoreAllMocks()
  Object.defineProperty(window, "innerWidth", originalInnerWidth)
  localStorage.clear()
})

describe("AppShell Workspace layout", () => {
  it("renders Workspace between sessions and chat with adjustable columns", async () => {
    const wrapper = createShell()

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

  it("supports accessible keyboard resizing, full available width, limits and per-column reset", async () => {
    const wrapper = createShell()
    const sidebar = wrapper.get("[data-testid='sidebar-resizer']")
    const workspace = wrapper.get("[data-testid='workspace-resizer']")
    await nextTick()
    expect(workspace.attributes()).toMatchObject({
      role: "separator",
      tabindex: "0",
      "aria-orientation": "vertical",
      "aria-valuemin": "280",
      "aria-valuemax": "1004",
      "aria-valuenow": "380",
    })
    await sidebar.trigger("keydown", { key: "ArrowRight" })
    await workspace.trigger("keydown", { key: "ArrowLeft" })
    expect(columnWidth(wrapper, "sidebar")).toBe(276)
    expect(columnWidth(wrapper, "workspace")).toBe(364)
    await workspace.trigger("keydown", { key: "ArrowRight", shiftKey: true })
    expect(columnWidth(wrapper, "workspace")).toBe(428)
    await workspace.trigger("keydown", { key: "End" })
    expect(columnWidth(wrapper, "workspace")).toBe(988)
    expect(workspace.attributes("aria-valuenow")).toBe("988")
    await workspace.trigger("keydown", { key: "Home" })
    await workspace.trigger("keydown", { key: "ArrowLeft" })
    expect(columnWidth(wrapper, "workspace")).toBe(280)
    await sidebar.trigger("keydown", { key: "End" })
    expect(columnWidth(wrapper, "sidebar")).toBe(440)
    await sidebar.trigger("keydown", { key: "Home" })
    expect(columnWidth(wrapper, "sidebar")).toBe(180)
    await workspace.trigger("dblclick")
    expect(columnWidth(wrapper, "workspace")).toBe(380)
    expect(columnWidth(wrapper, "sidebar")).toBe(180)
    await sidebar.trigger("dblclick")
    expect(columnWidth(wrapper, "sidebar")).toBe(260)
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe("380")
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe("260")
  })

  it("retains user width preferences through temporary narrowing and remount", async () => {
    const wrapper = createShell()
    await wrapper.get("[data-testid='workspace-resizer']").trigger("keydown", { key: "End" })
    expect(columnWidth(wrapper, "workspace")).toBe(1004)
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe("1004")
    viewport(1200)
    await nextTick()
    expect(columnWidth(wrapper, "workspace")).toBeLessThanOrEqual(604)
    expect(columnWidth(wrapper, "workspace")).toBeGreaterThanOrEqual(280)
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe("1004")
    viewport(900)
    await nextTick()
    const stored = localStorage.getItem(WORKSPACE_KEY)
    await wrapper.get("[data-testid='workspace-resizer']").trigger("keydown", { key: "Home" })
    pointer(wrapper.get("[data-testid='workspace-resizer']").element, "pointerdown")
    pointer(window, "pointermove", { clientX: 700 })
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe(stored)
    viewport(1600)
    await nextTick()
    expect(columnWidth(wrapper, "workspace")).toBe(1004)
    wrapper.unmount()
    const reloaded = createShell()
    await nextTick()
    expect(columnWidth(reloaded, "workspace")).toBe(1004)
  })

  it("ignores invalid stored values and works when storage access is denied", async () => {
    localStorage.setItem(SIDEBAR_KEY, "not-a-number")
    localStorage.setItem(WORKSPACE_KEY, "Infinity")
    const wrapper = createShell()
    expect(columnWidth(wrapper, "sidebar")).toBe(260)
    expect(columnWidth(wrapper, "workspace")).toBe(380)
    wrapper.unmount()
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage disabled", "SecurityError")
    })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Storage disabled", "SecurityError")
    })
    const denied = createShell()
    await denied.get("[data-testid='workspace-resizer']").trigger("keydown", { key: "ArrowRight" })
    expect(columnWidth(denied, "workspace")).toBe(396)
  })

  it("captures only the primary pointer and ignores unrelated movement and release", async () => {
    const wrapper = createShell()
    const handle = wrapper.get("[data-testid='workspace-resizer']").element
    const capture = pointerCapture(handle)
    pointer(handle, "pointerdown", { button: 2 })
    pointer(window, "pointermove", { clientX: 700 })
    expect(capture.set).not.toHaveBeenCalled()
    expect(columnWidth(wrapper, "workspace")).toBe(380)
    pointer(handle, "pointerdown")
    expect(capture.set).toHaveBeenCalledWith(7)
    pointer(window, "pointermove", { pointerId: 8, clientX: 800 })
    pointer(window, "pointerup", { pointerId: 8 })
    pointer(window, "pointermove", { clientX: 700 })
    await nextTick()
    expect(columnWidth(wrapper, "workspace")).toBe(580)
    pointer(window, "pointerup")
    expect(capture.release).toHaveBeenCalledWith(7)
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe("580")
    pointer(window, "pointermove", { clientX: 1000 })
    await nextTick()
    expect(columnWidth(wrapper, "workspace")).toBe(580)
  })

  it.each(["pointercancel", "blur"])("stops dragging and releases capture on %s", async (event) => {
    const wrapper = createShell()
    const handle = wrapper.get("[data-testid='workspace-resizer']").element
    const capture = pointerCapture(handle)
    pointer(handle, "pointerdown")
    pointer(window, "pointermove", { clientX: 700 })
    await nextTick()
    expect(columnWidth(wrapper, "workspace")).toBe(580)
    if (event === "pointercancel") pointer(window, event)
    else window.dispatchEvent(new Event(event))
    pointer(window, "pointermove", { clientX: 1000 })
    await nextTick()
    expect(capture.release).toHaveBeenCalledWith(7)
    expect(wrapper.get(".app-shell").classes()).not.toContain("resizing")
    expect(columnWidth(wrapper, "workspace")).toBe(580)
  })

  it("releases active capture and all global listeners when unmounted", () => {
    const added = vi.spyOn(window, "addEventListener")
    const removed = vi.spyOn(window, "removeEventListener")
    const wrapper = createShell()
    const handle = wrapper.get("[data-testid='workspace-resizer']").element
    const capture = pointerCapture(handle)
    pointer(handle, "pointerdown")
    const callbacks = added.mock.calls.filter(([name]) =>
      ["keydown", "pointermove", "pointerup", "pointercancel", "resize", "blur"].includes(name),
    )
    expect(callbacks.length).toBeGreaterThanOrEqual(5)
    wrapper.unmount()
    expect(capture.release).toHaveBeenCalledWith(7)
    for (const [name, callback] of callbacks) {
      expect(
        removed.mock.calls.some(([type, listener]) => type === name && listener === callback),
      ).toBe(true)
    }
  })
})
