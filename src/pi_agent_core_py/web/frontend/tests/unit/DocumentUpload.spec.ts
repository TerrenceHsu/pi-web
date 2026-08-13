// DocumentUpload 单元测试——P2-R5-C §18.2。
//
// 覆盖：PDF 接受（按 MIME / 按扩展名）、非 PDF 客户端拒绝、
// 上传错误（413 / 409 / 500）经 store.uploadError 表面化、按钮 disabled 条件。

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

vi.mock("../../src/api/client", () => ({
  ApiError: FakeApiError,
}))

import DocumentUpload from "../../src/components/knowledge/DocumentUpload.vue"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.uploadPdf.mockResolvedValue({})
  api.listDocuments.mockResolvedValue([])
})

function makeFile(name: string, type: string): File {
  return new File(["%PDF-1.4"], name, { type })
}

async function setContent(input: any, file: File) {
  Object.defineProperty(input.element, "files", { value: [file], configurable: true })
  await input.trigger("change")
  for (let i = 0; i < 4; i++) await Promise.resolve()
}

describe("DocumentUpload", () => {
  it("accepts PDF by extension (.pdf)", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const wrapper = mount(DocumentUpload)
    await setContent(wrapper.get('[data-testid="upload-input"]'), makeFile("x.pdf", ""))
    expect(api.uploadPdf).toHaveBeenCalled()
    expect(store.uploadError).toBeNull()
  })

  it("accepts PDF by MIME type", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const wrapper = mount(DocumentUpload)
    await setContent(wrapper.get('[data-testid="upload-input"]'), makeFile("x.bin", "application/pdf"))
    expect(api.uploadPdf).toHaveBeenCalled()
  })

  it("rejects non-PDF client-side (no uploadPdf call + uploadError set)", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const wrapper = mount(DocumentUpload)
    await setContent(
      wrapper.get('[data-testid="upload-input"]'),
      new File(["nope"], "x.txt", { type: "text/plain" }),
    )
    expect(api.uploadPdf).not.toHaveBeenCalled()
    expect(store.uploadError).toContain("Only PDF")
  })

  it("surfaces 413 upload_too_large via store.uploadError", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    api.uploadPdf.mockRejectedValue(new FakeApiError(413, "upload_too_large"))
    const wrapper = mount(DocumentUpload)
    await setContent(wrapper.get('[data-testid="upload-input"]'), makeFile("big.pdf", "application/pdf"))
    expect(store.uploadError).toBe("upload_too_large")
  })

  it("surfaces 409 duplicate_document via store.uploadError", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    api.uploadPdf.mockRejectedValue(new FakeApiError(409, "duplicate_document"))
    const wrapper = mount(DocumentUpload)
    await setContent(wrapper.get('[data-testid="upload-input"]'), makeFile("dup.pdf", "application/pdf"))
    expect(store.uploadError).toBe("duplicate_document")
  })

  it("surfaces 500 internal_knowledge_error via store.uploadError", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    api.uploadPdf.mockRejectedValue(new FakeApiError(500, "internal_knowledge_error"))
    const wrapper = mount(DocumentUpload)
    await setContent(wrapper.get('[data-testid="upload-input"]'), makeFile("err.pdf", "application/pdf"))
    expect(store.uploadError).toBe("internal_knowledge_error")
  })

  it("upload button disabled when no library is selected", () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = null
    const wrapper = mount(DocumentUpload)
    const btn = wrapper.get('[data-testid="upload-btn"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("upload button disabled while uploadingDocument=true", () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    store.uploadingDocument = true
    const wrapper = mount(DocumentUpload)
    const btn = wrapper.get('[data-testid="upload-btn"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })
})
