import { flushPromises, mount } from "@vue/test-utils"
import { describe, expect, it, vi } from "vitest"

import WorkspaceFilePreview from "../../src/components/workspace/WorkspaceFilePreview.vue"
import type { FileRef } from "../../src/types"

const original: FileRef = {
  id: "old-file",
  session_id: "one",
  name: "notes.md",
  logical_path: "notes.md",
  format: "markdown",
  mime: "text/markdown",
  size: 20,
  sha256: "old-sha",
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe("Workspace preview request ownership", () => {
  it.each(["file", "sha", "session"])("ignores a late read after changing %s", async (change) => {
    const oldRead = deferred<string>()
    const newRead = deferred<string>()
    const loadContent = vi
      .fn()
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(newRead.promise)
    const wrapper = mount(WorkspaceFilePreview, {
      props: { file: original, sessionId: "one", loadContent, saveContent: vi.fn() },
    })
    const file = {
      ...original,
      ...(change === "file"
        ? {
            id: "new-file",
            name: "main.py",
            format: "text" as const,
            logical_path: "upload/main.py",
          }
        : {}),
      ...(change === "sha" ? { sha256: "new-sha" } : {}),
    }
    await wrapper.setProps({ file, sessionId: change === "session" ? "two" : "one" })
    newRead.resolve("Current content")
    await flushPromises()
    oldRead.resolve("Stale Markdown from a previous request")
    await flushPromises()

    expect(loadContent).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain("Current content")
    expect(wrapper.text()).not.toContain("Stale Markdown")
    wrapper.unmount()
  })

  it.each(["success", "failure"])(
    "ignores a stale save %s while the new file loads",
    async (outcome) => {
      const saved = deferred<FileRef>()
      const currentRead = deferred<string>()
      const loadContent = vi
        .fn()
        .mockResolvedValueOnce("Original")
        .mockReturnValueOnce(currentRead.promise)
      const saveContent = vi.fn().mockReturnValue(saved.promise)
      const wrapper = mount(WorkspaceFilePreview, {
        props: { file: original, sessionId: "one", loadContent, saveContent },
      })
      await flushPromises()
      await wrapper.findAll("[role='tab']")[1].trigger("click")
      await wrapper.get("textarea").setValue("Edited")
      await wrapper.get(".editor-actions button").trigger("click")
      await wrapper.setProps({ file: { ...original, id: "new-file" } })

      if (outcome === "success") saved.resolve({ ...original, sha256: "saved-sha" })
      else saved.reject(new Error("Stale save error"))
      await flushPromises()
      expect(wrapper.text()).toContain("Loading result")
      expect(wrapper.find("[role='alert']").exists()).toBe(false)
      expect(wrapper.emitted("saved")).toBeUndefined()
      currentRead.resolve("New file")
      await flushPromises()
      expect(wrapper.text()).toContain("New file")
      expect(saveContent).toHaveBeenCalledWith("old-file", "Edited", "old-sha")
      wrapper.unmount()
    },
  )
})
