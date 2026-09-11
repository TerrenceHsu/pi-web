import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { useWorkspaceDeskStore } from "../../src/stores/workspaceDeskStore"
import WorkspaceDesk from "../../src/components/workspace/WorkspaceDesk.vue"
import WorkspaceBrowser from "../../src/components/workspace/WorkspaceBrowser.vue"
import WorkspaceFilePreview from "../../src/components/workspace/WorkspaceFilePreview.vue"
import type { FileRef } from "../../src/types"

vi.mock("../../src/api/browser", () => ({
  list: vi.fn().mockResolvedValue({ pages: [] }),
  create: vi.fn(),
  close: vi.fn(),
}))
const file: FileRef = {
  id: "one",
  session_id: "s",
  name: "one.md",
  format: "markdown",
  mime: "text/markdown",
  size: 5,
  sha256: "sha1",
}
const tab = (id: string) => ({ id, kind: "file" as const, resourceId: id, title: id })
beforeEach(() => {
  setActivePinia(createPinia())
})

describe("Workspace tab ownership", () => {
  it("deduplicates tabs, isolates Sessions and clears deleted Session drafts", () => {
    const store = useWorkspaceDeskStore()
    store.open("a", tab("one"))
    store.open("a", tab("one"))
    store.open("b", tab("two"))
    store.setDraft("a", "one", { content: "draft", baseline: "base", sha: "s", editing: true })
    expect(store.desk("a").tabs).toHaveLength(1)
    expect(store.hasDrafts()).toBe(true)
    store.forgetSession("a")
    store.setDraft("a", "one", { content: "late", baseline: "", sha: "", editing: true })
    expect(store.sessions.a).toBeUndefined()
    expect(store.desk("b").tabs).toHaveLength(1)
    expect(store.hasDrafts()).toBe(false)
  })
  it("splits two distinct tabs, and closes a tab without deleting a file", () => {
    const store = useWorkspaceDeskStore()
    store.open("s", tab("one"))
    store.open("s", tab("two"))
    store.setSplit("s", "columns")
    expect(store.desk("s").primary).toBe("two")
    expect(store.desk("s").secondary).toBe("one")
    store.activate("s", "one")
    expect(store.desk("s").focused).toBe("secondary")
    store.close("s", "two")
    expect(store.desk("s").primary).toBe("one")
    expect(store.desk("s").split).toBe("none")
    store.resetWorkspace()
    expect(store.sessions).toEqual({})
  })
  it("bounds open tabs without duplicating views", () => {
    const store = useWorkspaceDeskStore()
    for (let i = 0; i < 16; i++) store.open("s", tab(String(i)))
    expect(() => store.open("s", tab("extra"))).toThrow("limit 16")
    store.setSplit("s", "rows")
    store.activate("s", "15")
    expect(store.desk("s").primary).not.toBe(store.desk("s").secondary)
  })
  it("activates the nearest neighboring tab when the current tab closes", () => {
    const store = useWorkspaceDeskStore()
    for (const id of ["one", "two", "three", "four"]) store.open("s", tab(id))
    store.activate("s", "three")
    store.close("s", "three")
    expect(store.desk("s").primary).toBe("four")
    store.close("s", "four")
    expect(store.desk("s").primary).toBe("two")
    store.close("s", "one")
    expect(store.desk("s").primary).toBe("two")
  })
  it("offers one Markdown action, an end-of-strip plus and keyboard tab navigation", async () => {
    const wrapper = mount(WorkspaceDesk, {
      props: {
        sessionId: "s",
        files: [file, { ...file, id: "two", name: "two.md" }],
        filesReady: true,
        selectedFileId: "one",
        loadContent: vi.fn().mockResolvedValue("Original"),
        saveContent: vi.fn(),
      },
      global: { stubs: { WorkspaceBrowser: true } },
    })
    await wrapper.setProps({ selectedFileId: "two" })
    await flushPromises()
    expect(
      wrapper.findAll("button").filter((button) => button.text() === "New Markdown"),
    ).toHaveLength(1)
    expect(wrapper.get(".desk-tab-bar > button").attributes("aria-label")).toBe("New browser tab")
    const tabs = wrapper.findAll(".desk-tabs [role='tab']")
    expect(tabs[1].attributes("tabindex")).toBe("0")
    await tabs[1].trigger("keydown", { key: "ArrowRight" })
    expect(tabs[0].attributes("aria-selected")).toBe("true")
    await tabs[0].trigger("keydown", { key: "End" })
    expect(tabs[1].attributes("aria-selected")).toBe("true")
    await wrapper.get("select").setValue("columns")
    expect(wrapper.findAll(".desk-tabs [aria-selected='true']")).toHaveLength(1)
    await tabs[0].trigger("click")
    expect(useWorkspaceDeskStore().desk("s").focused).toBe("secondary")
    expect(wrapper.findAll(".desk-tabs [aria-selected='true']")).toHaveLength(1)
    wrapper.unmount()
  })
  describe("preferred browser media ownership", () => {
    const browserTab = (id: string) => ({
      id: `browser:${id}`,
      kind: "browser" as const,
      resourceId: id,
      title: id,
    })
    async function mountDesk() {
      const wrapper = mount(WorkspaceDesk, {
        props: {
          sessionId: "s",
          files: [file],
          filesReady: true,
          selectedFileId: null,
          active: true,
          loadContent: vi.fn(),
          saveContent: vi.fn(),
        },
        global: { stubs: { WorkspaceBrowser: true, WorkspaceFilePreview: true } },
      })
      // Finish the initial empty server poll before opening the fixture tabs;
      // otherwise that response correctly removes the unlisted local pages.
      await flushPromises()
      return wrapper
    }
    function preferredPages(wrapper: Awaited<ReturnType<typeof mountDesk>>) {
      return wrapper
        .findAllComponents(WorkspaceBrowser)
        .filter((browser) => browser.props("mediaPreferred"))
        .map((browser) => browser.props("pageId"))
    }
    it("prefers exactly the visible browser in a single pane", async () => {
      const wrapper = await mountDesk()
      try {
        const store = useWorkspaceDeskStore()
        store.open("s", browserTab("one"))
        store.open("s", browserTab("two"))
        await flushPromises()
        expect(preferredPages(wrapper)).toEqual(["two"])
        const browsers = wrapper.findAllComponents(WorkspaceBrowser)
        expect(browsers.map((browser) => browser.props("active"))).toEqual([false, true])
        await wrapper.get('[id="desk-tab-s-browser:one"]').trigger("click")
        expect(preferredPages(wrapper)).toEqual(["one"])
        expect(browsers.map((browser) => browser.props("active"))).toEqual([true, false])
      } finally {
        wrapper.unmount()
      }
    })
    it.each(["columns", "rows"] as const)(
      "transfers one media preference when focus moves between two %s browser panes",
      async (split) => {
        const wrapper = await mountDesk()
        try {
          const store = useWorkspaceDeskStore()
          store.open("s", browserTab("one"))
          store.open("s", browserTab("two"))
          store.setSplit("s", split)
          await flushPromises()
          expect(preferredPages(wrapper)).toEqual(["two"])
          expect(
            wrapper.findAllComponents(WorkspaceBrowser).map((browser) => browser.props("active")),
          ).toEqual([true, true])
          await wrapper.get(".desk-view.secondary").trigger("pointerdown")
          expect(store.desk("s").focused).toBe("secondary")
          expect(preferredPages(wrapper)).toEqual(["one"])
          await wrapper.get(".desk-view:not(.secondary)").trigger("pointerdown")
          expect(store.desk("s").focused).toBe("primary")
          expect(preferredPages(wrapper)).toEqual(["two"])
        } finally {
          wrapper.unmount()
        }
      },
    )
    it("keeps the sole browser preferred when its neighboring Markdown pane is focused", async () => {
      const wrapper = await mountDesk()
      try {
        const store = useWorkspaceDeskStore()
        store.open("s", browserTab("one"))
        store.open("s", { id: "file:one", kind: "file", resourceId: "one", title: "one.md" })
        store.setSplit("s", "columns")
        await flushPromises()
        expect(store.desk("s").primary).toBe("file:one")
        expect(preferredPages(wrapper)).toEqual(["one"])
        await wrapper.get(".desk-view.secondary").trigger("pointerdown")
        expect(store.desk("s").focused).toBe("secondary")
        expect(preferredPages(wrapper)).toEqual(["one"])
        await wrapper.get(".desk-view:not(.secondary)").trigger("pointerdown")
        expect(store.desk("s").focused).toBe("primary")
        expect(preferredPages(wrapper)).toEqual(["one"])
        expect(wrapper.findComponent(WorkspaceBrowser).props("active")).toBe(true)
      } finally {
        wrapper.unmount()
      }
    })
    it("does not prefer a browser hidden behind a Markdown tab", async () => {
      const wrapper = await mountDesk()
      try {
        const store = useWorkspaceDeskStore()
        store.open("s", browserTab("one"))
        store.open("s", { id: "file:one", kind: "file", resourceId: "one", title: "one.md" })
        await flushPromises()
        expect(preferredPages(wrapper)).toEqual([])
        expect(wrapper.findComponent(WorkspaceBrowser).props("active")).toBe(false)
        store.setSplit("s", "columns")
        await flushPromises()
        expect(preferredPages(wrapper)).toEqual(["one"])
        store.setSplit("s", "none")
        await flushPromises()
        expect(preferredPages(wrapper)).toEqual([])
        expect(wrapper.findComponent(WorkspaceBrowser).props("active")).toBe(false)
      } finally {
        wrapper.unmount()
      }
    })
  })
  it("renders multiple documents and keeps edited Markdown while switching", async () => {
    const files = [file, { ...file, id: "two", name: "two.md" }]
    const wrapper = mount(WorkspaceDesk, {
      props: {
        sessionId: "s",
        files,
        filesReady: true,
        selectedFileId: "one",
        loadContent: vi.fn().mockResolvedValue("Original"),
        saveContent: vi.fn(),
      },
      global: { stubs: { WorkspaceBrowser: true } },
    })
    await flushPromises()
    const preview = wrapper.findComponent(WorkspaceFilePreview)
    await preview.findAll("[role='tab']")[1].trigger("click")
    await preview.get("textarea").setValue("Unsaved draft")
    await wrapper.setProps({ selectedFileId: "two" })
    await flushPromises()
    expect(wrapper.findAll("[data-testid='desk-tab-file']")).toHaveLength(2)
    expect(useWorkspaceDeskStore().desk("s").drafts.one.content).toBe("Unsaved draft")
    await wrapper.get("select").setValue("columns")
    expect(wrapper.get("[data-testid='desk-panes']").classes()).toContain("columns")
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    await wrapper.get("[aria-label='Close one.md']").trigger("click")
    expect(confirm).toHaveBeenCalled()
    expect(useWorkspaceDeskStore().desk("s").tabs).toHaveLength(2)
    confirm.mockReturnValue(true)
    await wrapper.get("[aria-label='Close one.md']").trigger("click")
    await flushPromises()
    expect(useWorkspaceDeskStore().desk("s").tabs).toHaveLength(1)
    expect(files).toHaveLength(2)
    wrapper.unmount()
    confirm.mockRestore()
  })
  it("restores a draft after remount and does not overwrite it after external file changes", async () => {
    const load = vi.fn().mockResolvedValue("On disk")
    const save = vi.fn().mockRejectedValue(new Error("Conflict"))
    const wrapper = mount(WorkspaceFilePreview, {
      props: {
        sessionId: "s",
        file,
        loadContent: load,
        saveContent: save,
        draft: { content: "My edit", baseline: "Old", sha: "original-sha", editing: true },
      },
    })
    await flushPromises()
    expect(load).not.toHaveBeenCalled()
    expect(wrapper.get("textarea").element.value).toBe("My edit")
    await wrapper.setProps({ file: { ...file, sha256: "external-sha" } })
    expect(wrapper.get("textarea").element.value).toBe("My edit")
    await wrapper.get(".editor-actions button").trigger("click")
    await flushPromises()
    expect(save).toHaveBeenCalledWith("one", "My edit", "original-sha")
    expect(wrapper.text()).toContain("Conflict")
    wrapper.unmount()
  })
})
