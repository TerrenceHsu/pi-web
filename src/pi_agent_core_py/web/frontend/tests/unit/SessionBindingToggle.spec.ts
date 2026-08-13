// SessionBindingToggle 单元测试——P2-R5-C §18.2。
//
// 覆盖：ON / OFF 状态、toggle 调用 bind/unbind（经 API）、失败 rollback + ApiError 表面化、
// 无 session 时 disabled、loadingBindings 时 disabled。

import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { mount } from "@vue/test-utils"

const { api, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number
    readonly detail: string
    readonly payload: any
    constructor(status: number, detail: string, payload: any = null) {
      super(detail)
      this.status = status
      this.detail = detail
      this.payload = payload
    }
  }
  return {
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
    FakeApiError,
  }
})

vi.mock("../../src/api/knowledge", () => api)
vi.mock("../../src/api/client", () => ({ ApiError: FakeApiError }))

import SessionBindingToggle from "../../src/components/knowledge/SessionBindingToggle.vue"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.replaceSessionBindings.mockImplementation(async (_sid: string, ids: string[]) => ({
    session_id: _sid,
    library_ids: ids,
  }))
})

describe("SessionBindingToggle", () => {
  it("reflects OFF when library not in sessionBindingIds", () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = []
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: "sess_1", libraryId: "lib_1" },
    })
    const btn = wrapper.get('[data-testid="binding-toggle-lib_1"]')
    expect(btn.classes()).not.toContain("on")
  })

  it("reflects ON when library is in sessionBindingIds", () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1"]
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: "sess_1", libraryId: "lib_1" },
    })
    const btn = wrapper.get('[data-testid="binding-toggle-lib_1"]')
    expect(btn.classes()).toContain("on")
  })

  it("OFF → click calls API replaceSessionBindings with lib added", async () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = []
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: "sess_1", libraryId: "lib_1" },
    })
    await wrapper.get('[data-testid="binding-toggle-lib_1"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.replaceSessionBindings).toHaveBeenCalledWith("sess_1", ["lib_1"])
  })

  it("ON → click calls API replaceSessionBindings with lib removed", async () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1", "lib_other"]
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: "sess_1", libraryId: "lib_1" },
    })
    await wrapper.get('[data-testid="binding-toggle-lib_1"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.replaceSessionBindings).toHaveBeenCalledWith("sess_1", ["lib_other"])
  })

  it("disabled when sessionId is null", () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = []
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: null, libraryId: "lib_1" },
    })
    const btn = wrapper.get('[data-testid="binding-toggle-lib_1"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(wrapper.attributes("title") ?? wrapper.text()).toContain("Select a chat session")
  })

  it("disabled while loadingBindings", () => {
    const store = useKnowledgeStore()
    store.loadingBindings = true
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: "sess_1", libraryId: "lib_1" },
    })
    const btn = wrapper.get('[data-testid="binding-toggle-lib_1"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("bind failure rolls back (sessionBindingIds stays empty) + surfaces error", async () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = []
    api.replaceSessionBindings.mockRejectedValue(new FakeApiError(400, "validation_error"))
    const wrapper = mount(SessionBindingToggle, {
      props: { sessionId: "sess_1", libraryId: "lib_1" },
    })
    await wrapper.get('[data-testid="binding-toggle-lib_1"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(store.sessionBindingIds).toEqual([]) // rollback
    expect(store.error).toBe("validation_error")
  })
})
