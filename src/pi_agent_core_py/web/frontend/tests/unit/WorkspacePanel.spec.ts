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

const sandboxApi = vi.hoisted(() => ({
  readSandboxArtifactFile: vi.fn(),
  downloadSandboxArtifactFile: vi.fn(),
  publishSandboxOperation: vi.fn(),
  getSandboxEvents: vi.fn(),
}))

vi.mock("../../src/api/files", () => filesApi)
vi.mock("../../src/api/codingSandbox", () => sandboxApi)

import WorkspacePanel from "../../src/components/workspace/WorkspacePanel.vue"
import SandboxApprovalBar from "../../src/components/coding-sandbox/SandboxApprovalBar.vue"
import { useChatStore } from "../../src/stores/chatStore"
import { useCodingSandboxStore } from "../../src/stores/codingSandboxStore"
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
  localStorage.removeItem("pi-agent-workspace-files-pane-height")
  Object.values(filesApi).forEach((mock) => mock.mockReset())
  Object.values(sandboxApi).forEach((mock) => mock.mockReset())
  filesApi.downloadFileUrl.mockImplementation(
    (sessionId: string, fileId: string) => `/api/sessions/${sessionId}/files/${fileId}`,
  )
  filesApi.readTextFile.mockResolvedValue("print('delivered')\n")
  sandboxApi.readSandboxArtifactFile.mockResolvedValue(
    "def train(value=1):\n    print('pending')\n",
  )
  sandboxApi.downloadSandboxArtifactFile.mockResolvedValue(undefined)
  sandboxApi.getSandboxEvents.mockResolvedValue({ events: [], gap: false, has_more: false })
})

describe("Workspace result panel", () => {
  it("publishes an awaiting artifact directly from the chat approval bar", async () => {
    setup()
    const sandboxStore = useCodingSandboxStore()
    const awaiting = {
      operation_id: "sandbox-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      session_id: "sess-1",
      status: "awaiting_approval",
      changed_paths: ["scripts/ppo.py"],
      diff: {
        entries: [{ path: "scripts/ppo.py", status: "added" }],
        patch: "+print('pending')\n",
        patch_truncated: false,
      },
    } as any
    sandboxStore.operation = awaiting
    sandboxApi.publishSandboxOperation.mockResolvedValue({ ...awaiting, status: "published" })
    const wrapper = mount(SandboxApprovalBar)

    expect(wrapper.get("[data-testid='sandbox-chat-approval']").text()).toContain(
      "代码已验证，等待批准发布",
    )
    await wrapper.get("[data-testid='sandbox-chat-review']").trigger("click")
    expect(wrapper.emitted("open-workspace")).toHaveLength(1)
    await wrapper.get("[data-testid='sandbox-chat-publish']").trigger("click")
    await flushPromises()

    expect(sandboxApi.publishSandboxOperation).toHaveBeenCalledWith(awaiting.operation_id)
  })

  it("previews and downloads a frozen Sandbox file before approval", async () => {
    const { fileStore } = setup()
    fileStore.filesBySession["sess-1"] = [agentMd]
    const sandboxStore = useCodingSandboxStore()
    sandboxStore.operation = {
      operation_id: "sandbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      session_id: "sess-1",
      status: "awaiting_approval",
      diff: {
        entries: [
          {
            path: "scripts/ppo.py",
            status: "added",
            before_sha256: null,
            after_sha256: "a".repeat(64),
          },
        ],
        patch: "+print('pending')\n",
        patch_truncated: false,
      },
    } as any
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.getComponent({ name: "CodingSandboxModal" }).props("open")).toBe(false)
    await wrapper.get("[data-testid='workspace-row-resizer']").trigger("keydown", {
      key: "ArrowDown",
    })
    expect(wrapper.get("[data-testid='workspace-files-pane']").attributes("style")).toContain(
      "height: 276px",
    )
    await wrapper.get("[data-testid='workspace-tab-files']").trigger("click")
    expect(wrapper.get("[data-testid='workspace-pending-sandbox']").text()).toContain(
      "1 file(s) pending approval",
    )
    await wrapper.get("[aria-label='Open ppo.py']").trigger("click")
    await flushPromises()

    expect(sandboxApi.readSandboxArtifactFile).toHaveBeenCalledWith(
      "sandbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "scripts/ppo.py",
    )
    expect(wrapper.get("[aria-label='ppo.py code']").text()).toContain("print('pending')")
    expect(wrapper.get("[data-testid='python-syntax-preview']").exists()).toBe(true)
    expect(wrapper.get(".python-keyword").text()).toBe("def")
    expect(wrapper.get(".python-function").text()).toBe("train")
    expect(wrapper.get(".python-builtin").text()).toBe("print")
    expect(wrapper.get(".python-string").text()).toBe("'pending'")
    expect(wrapper.get("[data-testid='workspace-file-preview']").text()).toContain(
      "Pending approval",
    )
    await wrapper
      .get("[data-testid='workspace-file-preview'] [aria-label='Download ppo.py']")
      .trigger("click")
    await flushPromises()
    expect(sandboxApi.downloadSandboxArtifactFile).toHaveBeenCalledWith(
      "sandbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "scripts/ppo.py",
    )
  })

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
