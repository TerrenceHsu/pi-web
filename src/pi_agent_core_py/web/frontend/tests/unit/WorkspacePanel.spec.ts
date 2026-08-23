import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { FileRef, WorkspaceState } from "../../src/types"

const filesApi = vi.hoisted(() => ({
  listFiles: vi.fn(),
  uploadFiles: vi.fn(),
  deleteFile: vi.fn(),
  readTextFile: vi.fn(),
  updateTextFile: vi.fn(),
  createMarkdownFile: vi.fn(),
  moveMarkdownFile: vi.fn(),
  downloadFileUrl: vi.fn(
    (sessionId: string, fileId: string) => `/api/sessions/${sessionId}/files/${fileId}`,
  ),
}))

vi.mock("../../src/api/files", () => filesApi)

import WorkspacePanel from "../../src/components/workspace/WorkspacePanel.vue"
import { useChatStore } from "../../src/stores/chatStore"
import { useFileStore } from "../../src/stores/fileStore"
import { useSessionStore } from "../../src/stores/sessionStore"

const workspace: WorkspaceState = {
  schema_version: 1,
  session_id: "sess-1",
  revision: 1,
  created_at: 1,
  updated_at: 2,
}

const pythonResult: FileRef = {
  id: "file-python",
  session_id: "sess-1",
  name: "main.py",
  logical_path: "scripts/main.py",
  origin: "agent",
  purpose: "file",
  mime: "text/x-python",
  format: "text",
  size: 18,
  sha256: "python-sha",
  created_at: 2,
  updated_at: 2,
}

const agentMd: FileRef = {
  id: "file-agent",
  session_id: "sess-1",
  name: "AGENT.md",
  logical_path: "AGENT.md",
  origin: "system",
  purpose: "agent_instructions",
  mime: "text/markdown",
  format: "markdown",
  size: 10,
  sha256: "agent-sha",
  created_at: 1,
  updated_at: 1,
}

function setup() {
  const sessionStore = useSessionStore()
  const chatStore = useChatStore()
  const fileStore = useFileStore()
  sessionStore.activeSessionId = "sess-1"
  sessionStore.sessions = [
    {
      id: "sess-1",
      title: "Workspace",
      created_at: 1,
      updated_at: 1,
      is_current: true,
    } as any,
  ]
  fileStore.filesBySession["sess-1"] = [agentMd, pythonResult]
  fileStore.workspaceBySession["sess-1"] = workspace
  return { chatStore, fileStore }
}

function mountPanel() {
  return mount(WorkspacePanel, {
    global: {
      stubs: {
        CodingSandboxModal: true,
      },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(filesApi).forEach((mock) => mock.mockReset())
  filesApi.downloadFileUrl.mockImplementation(
    (sessionId: string, fileId: string) => `/api/sessions/${sessionId}/files/${fileId}`,
  )
  filesApi.readTextFile.mockResolvedValue("print('delivered')\n")
})

describe("Workspace result panel", () => {
  it("focuses and previews a completed Agent write_file result", async () => {
    const { chatStore, fileStore } = setup()
    const wrapper = mountPanel()

    chatStore.streamItems.push({
      kind: "file_read",
      id: "tool-result-1",
      toolName: "write_file",
      toolCallId: "call-1",
      status: "done",
      details: {
        details: {
          file_id: "file-python",
          logical_path: "scripts/main.py",
          workspace_revision: 1,
        },
      },
    })
    await flushPromises()

    expect(fileStore.selectedFileIdBySession["sess-1"]).toBe("file-python")
    expect(fileStore.latestArtifactBySession["sess-1"]).toMatchObject({
      fileId: "file-python",
      logicalPath: "scripts/main.py",
      unseen: true,
    })
    expect(wrapper.get("[data-testid='workspace-latest-artifact']").text()).toContain(
      "scripts/main.py",
    )
    expect(wrapper.get("[aria-label='main.py code']").text()).toContain("print('delivered')")
  })

  it("creates Markdown through the Workspace revision API and selects it", async () => {
    const { fileStore } = setup()
    const created: FileRef = {
      ...agentMd,
      id: "file-plan",
      name: "plan.md",
      logical_path: "notes/plan.md",
      purpose: "file",
      origin: "user",
      sha256: "plan-sha",
    }
    filesApi.createMarkdownFile.mockResolvedValue({
      file: created,
      workspace: { ...workspace, revision: 2 },
    })
    filesApi.readTextFile.mockResolvedValue("# Plan\n")
    const wrapper = mountPanel()

    await wrapper.findAll(".workspace-toolbar button")[1].trigger("click")
    await wrapper.get("input[aria-label='Markdown logical path']").setValue("notes/plan.md")
    await wrapper.get("textarea[aria-label='Initial Markdown content']").setValue("# Plan\n")
    await wrapper.get("form.create-markdown").trigger("submit")
    await flushPromises()

    expect(filesApi.createMarkdownFile).toHaveBeenCalledWith(
      "sess-1",
      "notes/plan.md",
      "# Plan\n",
      1,
    )
    expect(fileStore.workspaceBySession["sess-1"].revision).toBe(2)
    expect(fileStore.selectedFileIdBySession["sess-1"]).toBe("file-plan")
  })

  it("previews and edits Markdown with sha and Workspace revision guards", async () => {
    const { fileStore } = setup()
    fileStore.selectedFileIdBySession["sess-1"] = "file-agent"
    filesApi.readTextFile.mockResolvedValue("# Original\n")
    filesApi.updateTextFile.mockResolvedValue({
      file: { ...agentMd, sha256: "agent-sha-2", origin: "user" },
      workspace: { ...workspace, revision: 2 },
    })
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.get(".markdown-preview").text()).toContain("Original")
    await wrapper.get("button[role='tab'][aria-selected='false']").trigger("click")
    const editor = wrapper.get("textarea[aria-label='AGENT.md content']")
    await editor.setValue("# Updated\n")
    await wrapper.get(".editor-actions button").trigger("click")
    await flushPromises()

    expect(filesApi.updateTextFile).toHaveBeenCalledWith(
      "sess-1",
      "file-agent",
      "# Updated\n",
      "agent-sha",
      1,
    )
    expect(fileStore.workspaceBySession["sess-1"].revision).toBe(2)
  })
})
