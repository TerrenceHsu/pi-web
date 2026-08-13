// LibraryList 单元测试——P2-R5-C §18.2。
//
// 覆盖：empty state、列表渲染、select、create（按钮 disabled / Enter / click）、
// rename（prompt）、delete（confirm / cancel）。
//
// Store action 通过 mock API 层驱动（setup store 内部 closure 无法直接覆盖）。

import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

const { api } = vi.hoisted(() => ({
  api: {
    listLibraries: vi.fn().mockResolvedValue([]),
    createLibrary: vi.fn(),
    getLibrary: vi.fn(),
    updateLibrary: vi.fn(),
    deleteLibrary: vi.fn(),
    listDocuments: vi.fn().mockResolvedValue([]),
    getDocument: vi.fn(),
    deleteDocument: vi.fn(),
    uploadPdf: vi.fn(),
    getIngestionStatus: vi.fn(),
    retryIngestion: vi.fn(),
    getDocumentMarkdownUrl: vi.fn(),
    fetchDocumentMarkdown: vi.fn(),
    getSessionBindings: vi.fn().mockResolvedValue({ session_id: "sess_1", library_ids: [] }),
    replaceSessionBindings: vi.fn(),
    searchLibrary: vi.fn(),
  },
}))

vi.mock("../../src/api/knowledge", () => api)

vi.mock("../../src/api/client", () => ({
  ApiError: class ApiError extends Error {
    readonly status: number
    readonly detail: string
    readonly payload: any
    constructor(status: number, detail: string, payload: any) {
      super(detail)
      this.status = status
      this.detail = detail
      this.payload = payload
    }
  },
}))

import LibraryList from "../../src/components/knowledge/LibraryList.vue"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"
import type { Library } from "../../src/types/knowledge"

function makeLibrary(o: Partial<Library> = {}): Library {
  return {
    id: "lib_1",
    name: "Library 1",
    description: "",
    status: "active",
    created_at: 0,
    updated_at: 0,
    document_count: 0,
    binding_count: 0,
    ...o,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.createLibrary.mockImplementation(async (body: any) =>
    makeLibrary({ id: "lib_new", name: body.name }),
  )
  api.updateLibrary.mockImplementation(async (_id: string, body: any) =>
    makeLibrary({ name: body.name }),
  )
  api.deleteLibrary.mockResolvedValue(null)
  api.listDocuments.mockResolvedValue([])
})

describe("LibraryList", () => {
  it("renders empty state when libraries list is empty", () => {
    const store = useKnowledgeStore()
    store.libraries = []
    store.loadingLibraries = false
    const wrapper = mount(LibraryList)
    expect(wrapper.text()).toContain("No libraries")
  })

  it("renders libraries + document count", () => {
    const store = useKnowledgeStore()
    store.libraries = [
      makeLibrary({ id: "lib_a", name: "Alpha", document_count: 2 }),
      makeLibrary({ id: "lib_b", name: "Beta", document_count: 1 }),
    ]
    const wrapper = mount(LibraryList)
    const items = wrapper.findAll('[data-testid="library-item"]')
    expect(items).toHaveLength(2)
    expect(wrapper.text()).toContain("Alpha")
    expect(wrapper.text()).toContain("2 docs")
    expect(wrapper.text()).toContain("1 doc")
  })

  it("clicking a library calls selectLibrary", async () => {
    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_a" })]
    const spy = vi.spyOn(store, "selectLibrary")
    const wrapper = mount(LibraryList)
    await wrapper.find('[data-testid="library-item"]').trigger("click")
    expect(spy).toHaveBeenCalledWith("lib_a")
  })

  it("create button disabled when name empty; enabled + creates when filled", async () => {
    const wrapper = mount(LibraryList)
    const btn = wrapper.get('[data-testid="library-create-btn"]')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)

    await wrapper.get('[data-testid="library-name-input"]').setValue("My Lib")
    expect((btn.element as HTMLButtonElement).disabled).toBe(false)

    await btn.trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.createLibrary).toHaveBeenCalledWith({ name: "My Lib", description: "" })
  })

  it("Enter in name input triggers create", async () => {
    const wrapper = mount(LibraryList)
    await wrapper.get('[data-testid="library-name-input"]').setValue("X")
    await wrapper.get('[data-testid="library-name-input"]').trigger("keyup.enter")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.createLibrary).toHaveBeenCalledWith({ name: "X", description: "" })
  })

  it("delete requires confirm; cancels when user declines", async () => {
    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_a", name: "Alpha" })]
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false)
    const wrapper = mount(LibraryList)
    await wrapper.get('[data-testid="library-delete-btn"]').trigger("click")
    expect(confirmSpy).toHaveBeenCalled()
    expect(api.deleteLibrary).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it("delete proceeds when user confirms", async () => {
    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_a", name: "Alpha" })]
    vi.spyOn(window, "confirm").mockReturnValue(true)
    const wrapper = mount(LibraryList)
    await wrapper.get('[data-testid="library-delete-btn"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.deleteLibrary).toHaveBeenCalledWith("lib_a")
  })

  it("rename prompts; skips when user cancels", async () => {
    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_a", name: "Alpha" })]
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue(null)
    const wrapper = mount(LibraryList)
    await wrapper.get('[data-testid="library-rename-btn"]').trigger("click")
    expect(promptSpy).toHaveBeenCalled()
    expect(api.updateLibrary).not.toHaveBeenCalled()
    promptSpy.mockRestore()
  })

  it("rename proceeds when user enters a new name", async () => {
    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_a", name: "Alpha" })]
    vi.spyOn(window, "prompt").mockReturnValue("Beta")
    const wrapper = mount(LibraryList)
    await wrapper.get('[data-testid="library-rename-btn"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.updateLibrary).toHaveBeenCalledWith("lib_a", expect.objectContaining({ name: "Beta" }))
  })
})
