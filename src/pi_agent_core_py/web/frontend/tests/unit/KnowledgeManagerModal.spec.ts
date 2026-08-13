// KnowledgeManagerModal 单元测试——P2-R5-C §18.2。
//
// 覆盖：open 触发 onModalOpen（API 调用）、close 触发 onModalClose（polling cleanup）、
// Modal @close 透传、unmount 触发 onModalClose。
//
// Store action 通过 mock API 层间接驱动（setup store 的 closure 无法被外部覆盖）。

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

// Stub chatStore — modal imports it for future binding-status surfacing but
// does not read fields. Avoid real chatStore side effects during teardown.
vi.mock("../../src/stores/chatStore", () => ({
  useChatStore: () => ({}),
}))
vi.mock("../../src/stores/sessionStore", () => ({
  useSessionStore: () => ({ activeSessionId: "sess_1" }),
}))

import KnowledgeManagerModal from "../../src/components/knowledge/KnowledgeManagerModal.vue"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.listLibraries.mockResolvedValue([])
  api.listDocuments.mockResolvedValue([])
  api.getSessionBindings.mockResolvedValue({ session_id: "sess_1", library_ids: [] })
})

describe("KnowledgeManagerModal", () => {
  it("opening triggers store.onModalOpen (libraries + bindings load)", async () => {
    const wrapper = mount(KnowledgeManagerModal, { props: { open: false } })
    await wrapper.setProps({ open: true })
    // Let watch callback + async loadLibraries/loadSessionBindings resolve.
    for (let i = 0; i < 8; i++) await Promise.resolve()
    expect(api.listLibraries).toHaveBeenCalled()
    expect(api.getSessionBindings).toHaveBeenCalledWith("sess_1")
  })

  it("closing triggers store.onModalClose (polling cleanup)", async () => {
    const wrapper = mount(KnowledgeManagerModal, { props: { open: true } })
    for (let i = 0; i < 4; i++) await Promise.resolve()
    const store = useKnowledgeStore()
    const spy = vi.spyOn(store, "onModalClose")
    await wrapper.setProps({ open: false })
    expect(spy).toHaveBeenCalled()
  })

  it("emits close when Modal @close fires", async () => {
    const wrapper = mount(KnowledgeManagerModal, { props: { open: true } })
    wrapper.getComponent({ name: "Modal" }).vm.$emit("close")
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted("close")).toBeTruthy()
  })

  it("unmount triggers onModalClose (no orphan timers)", async () => {
    const wrapper = mount(KnowledgeManagerModal, { props: { open: true } })
    for (let i = 0; i < 4; i++) await Promise.resolve()
    const store = useKnowledgeStore()
    const spy = vi.spyOn(store, "onModalClose")
    wrapper.unmount()
    expect(spy).toHaveBeenCalled()
  })
})
