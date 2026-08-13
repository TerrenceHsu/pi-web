// DocumentList + DocumentRow 单元测试——P2-R5-C §18.2。
//
// 覆盖：empty state、列表渲染、status 映射（DocumentRow）、retry 按钮可见性、
// delete confirm、view Markdown 仅 ready/indexing 显示。

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

import DocumentList from "../../src/components/knowledge/DocumentList.vue"
import DocumentRow from "../../src/components/knowledge/DocumentRow.vue"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"
import type { Document } from "../../src/types/knowledge"

function makeDoc(o: Partial<Document> = {}): Document {
  return {
    id: "doc_1",
    library_id: "lib_1",
    source_name: "report.pdf",
    source_sha256: "x",
    source_relpath: "x/report.pdf",
    markdown_relpath: "x/report.md",
    mime_type: "application/pdf",
    size_bytes: 1024,
    page_count: 1,
    status: "ready",
    parser_version: "pypdf-6.14.2",
    error_code: "",
    created_at: 0,
    updated_at: 0,
    ...o,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.retryIngestion.mockResolvedValue({
    document_id: "doc_1",
    job: {
      id: "j_1",
      document_id: "doc_1",
      stage: "extract",
      status: "queued",
      attempt: 1,
      started_at: 0,
      finished_at: null,
      safe_error_code: null,
    },
  })
  api.deleteDocument.mockResolvedValue(null)
  api.listDocuments.mockResolvedValue([])
})

describe("DocumentList (shell)", () => {
  it("renders empty state when documents list is empty", () => {
    const store = useKnowledgeStore()
    store.documents = []
    store.loadingDocuments = false
    const wrapper = mount(DocumentList)
    expect(wrapper.text()).toContain("No documents")
  })

  it("renders document rows + count", () => {
    const store = useKnowledgeStore()
    store.documents = [
      makeDoc({ id: "doc_a", source_name: "a.pdf" }),
      makeDoc({ id: "doc_b", source_name: "b.pdf" }),
    ]
    const wrapper = mount(DocumentList)
    expect(wrapper.findAllComponents(DocumentRow)).toHaveLength(2)
    expect(wrapper.text()).toContain("Documents (2)")
  })
})

describe("DocumentRow status mapping", () => {
  it("ready → green pill + View MD + no Retry", () => {
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc({ status: "ready" }) } })
    const pill = wrapper.get(".status-pill")
    expect(pill.classes()).toContain("ready")
    expect(wrapper.find('[data-testid="doc-retry-btn"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="doc-view-md-btn"]').exists()).toBe(true)
  })

  it("failed → red pill + Retry + no View MD", () => {
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc({ status: "failed" }) } })
    const pill = wrapper.get(".status-pill")
    expect(pill.classes()).toContain("failed")
    expect(wrapper.find('[data-testid="doc-retry-btn"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="doc-view-md-btn"]').exists()).toBe(false)
  })

  it("needs_ocr → yellow pill + Retry", () => {
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc({ status: "needs_ocr" }) } })
    const pill = wrapper.get(".status-pill")
    expect(pill.classes()).toContain("needs_ocr")
    expect(wrapper.find('[data-testid="doc-retry-btn"]').exists()).toBe(true)
  })

  it("indexing → blue pill + View MD", () => {
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc({ status: "indexing" }) } })
    const pill = wrapper.get(".status-pill")
    expect(pill.classes()).toContain("indexing")
    expect(wrapper.find('[data-testid="doc-view-md-btn"]').exists()).toBe(true)
  })

  it("unknown status → unknown pill + raw text preserved", () => {
    const wrapper = mount(DocumentRow, {
      props: { doc: makeDoc({ status: "weird_custom" }) },
    })
    const pill = wrapper.get(".status-pill")
    expect(pill.classes()).toContain("unknown")
    expect(pill.text()).toBe("weird_custom")
  })
})

describe("DocumentRow actions", () => {
  it("retry click calls API retryIngestion", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc({ id: "doc_x", status: "failed" }) } })
    await wrapper.get('[data-testid="doc-retry-btn"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.retryIngestion).toHaveBeenCalledWith("doc_x")
  })

  it("delete requires confirm; declined → no call", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false)
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc() } })
    await wrapper.get('[data-testid="doc-delete-btn"]').trigger("click")
    expect(api.deleteDocument).not.toHaveBeenCalled()
  })

  it("delete proceeds when user confirms", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true)
    const wrapper = mount(DocumentRow, { props: { doc: makeDoc({ id: "doc_y" }) } })
    await wrapper.get('[data-testid="doc-delete-btn"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.deleteDocument).toHaveBeenCalledWith("doc_y")
  })
})
