import { flushPromises, mount } from "@vue/test-utils"
import { describe, expect, it, vi } from "vitest"

import SessionFolderPanel from "../../src/components/chat/SessionFolderPanel.vue"
import type { FileRef } from "../../src/types"

const file: FileRef = {
  id: "file-1",
  name: "agent-report.md",
  logical_path: "outputs/agent-report.md",
  origin: "agent",
  purpose: "file",
  mime: "text/markdown",
  size: 42,
  sha256: "abc",
  format: "markdown",
}

const agentMd: FileRef = {
  id: "file-agent",
  name: "AGENT.md",
  logical_path: "AGENT.md",
  origin: "system",
  purpose: "agent_instructions",
  mime: "text/markdown",
  size: 20,
  sha256: "agent-sha",
  format: "markdown",
}

const memoryMd: FileRef = {
  id: "file-memory",
  name: "Memory.md",
  logical_path: "Memory.md",
  origin: "agent",
  purpose: "memory",
  mime: "text/markdown",
  size: 64,
  sha256: "memory-sha",
  format: "markdown",
}

function mountPanel(
  files: FileRef[],
  overrides: {
    loadContent?: (fileId: string) => Promise<string>
    saveContent?: (
      fileId: string,
      content: string,
      expectedSha256: string,
    ) => Promise<FileRef>
  } = {},
) {
  return mount(SessionFolderPanel, {
    props: {
      files,
      sessionId: "sess-1",
      loadContent: overrides.loadContent ?? vi.fn().mockResolvedValue("# AGENT.md"),
      saveContent: overrides.saveContent ?? vi.fn().mockResolvedValue(agentMd),
    },
  })
}

describe("SessionFolderPanel", () => {
  it("shows the initializing tree state", () => {
    const wrapper = mountPanel([])
    expect(wrapper.text()).toContain("Session files")
    expect(wrapper.text()).toContain("Initializing Session files")
  })

  it("renders nested logical folders and emits delete", async () => {
    const wrapper = mountPanel([file])
    expect(wrapper.get("[role='tree']").text()).toContain("outputs")
    expect(wrapper.text()).toContain("agent-report.md")
    expect(wrapper.get("a[download='agent-report.md']").attributes("href")).toBe(
      "/api/sessions/sess-1/files/file-1",
    )
    await wrapper.get("button[aria-label='Remove agent-report.md']").trigger("click")
    expect(wrapper.emitted("delete")).toEqual([["file-1"]])
  })

  it("opens and saves the protected AGENT.md editor", async () => {
    const loadContent = vi.fn().mockResolvedValue("# AGENT.md\n\nOriginal")
    const updated = { ...agentMd, sha256: "new-sha", origin: "user" as const }
    const saveContent = vi.fn().mockResolvedValue(updated)
    const wrapper = mountPanel([agentMd], { loadContent, saveContent })

    expect(wrapper.find("button[aria-label='Remove AGENT.md']").exists()).toBe(false)
    await wrapper.get("button[aria-label='Edit AGENT.md']").trigger("click")
    await flushPromises()
    expect(loadContent).toHaveBeenCalledWith("file-agent")
    const textarea = wrapper.get("textarea[aria-label='AGENT.md content']")
    await textarea.setValue("# AGENT.md\n\nUpdated")
    await wrapper.get("button[aria-label='Save AGENT.md']").trigger("click")
    await flushPromises()
    expect(saveContent).toHaveBeenCalledWith(
      "file-agent",
      "# AGENT.md\n\nUpdated",
      "agent-sha",
    )
  })

  it("opens, views, and saves Memory.md while keeping its delete action", async () => {
    const loadContent = vi.fn().mockResolvedValue("# Memory\n\nOriginal")
    const updated = { ...memoryMd, sha256: "memory-new-sha", origin: "user" as const }
    const saveContent = vi.fn().mockResolvedValue(updated)
    const wrapper = mountPanel([memoryMd], { loadContent, saveContent })

    expect(wrapper.find("button[aria-label='Remove Memory.md']").exists()).toBe(true)
    await wrapper.get("button[aria-label='Edit Memory.md']").trigger("click")
    await flushPromises()
    expect(loadContent).toHaveBeenCalledWith("file-memory")
    expect(wrapper.text()).toContain("Loaded as durable context in future turns")

    const textarea = wrapper.get("textarea[aria-label='Memory.md content']")
    expect((textarea.element as HTMLTextAreaElement).value).toContain("Original")
    await textarea.setValue("# Memory\n\nUser-maintained fact")
    await wrapper.get("button[aria-label='Save Memory.md']").trigger("click")
    await flushPromises()
    expect(saveContent).toHaveBeenCalledWith(
      "file-memory",
      "# Memory\n\nUser-maintained fact",
      "memory-sha",
    )
  })

  it("emits refresh and exposes loading state", async () => {
    const wrapper = mountPanel([agentMd])
    await wrapper.get("button[aria-label='Refresh session folder']").trigger("click")
    expect(wrapper.emitted("refresh")).toHaveLength(1)

    await wrapper.setProps({ loading: true })
    expect(wrapper.get("button[aria-label='Refresh session folder']").attributes()).toHaveProperty(
      "disabled",
    )
    expect(wrapper.text()).toContain("Loading…")
  })
})
