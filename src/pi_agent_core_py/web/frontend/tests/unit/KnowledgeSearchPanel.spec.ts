// KnowledgeSearchPanel 单元测试——P2-R5-C §18.2。
//
// 覆盖：空查询禁用、按钮 click 调 searchLibrary、Enter、loading、no results、
// hit 渲染 source + page + heading + content、单页/多页格式、clear。

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

import KnowledgeSearchPanel from "../../src/components/knowledge/KnowledgeSearchPanel.vue"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"
import type { LibrarySearchResultItem } from "../../src/types/knowledge"

function makeHit(o: Partial<LibrarySearchResultItem> = {}): LibrarySearchResultItem {
  return {
    document_id: "doc_1",
    source_name: "report.pdf",
    chunk_id: "chk_1",
    heading_path: ["H1", "H2"],
    page_start: 1,
    page_end: 2,
    content: "snippet text",
    rank: -1.5,
    ...o,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.searchLibrary.mockResolvedValue({ library_id: "lib_1", query: "X", results: [] })
})

describe("KnowledgeSearchPanel", () => {
  it("search button disabled when query is empty", () => {
    const wrapper = mount(KnowledgeSearchPanel)
    const btn = wrapper.get('[data-testid="search-run-btn"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("search button enabled when query has text", async () => {
    const wrapper = mount(KnowledgeSearchPanel)
    await wrapper.get('[data-testid="search-query-input"]').setValue("IMRT")
    const btn = wrapper.get('[data-testid="search-run-btn"]').element as HTMLButtonElement
    expect(btn.disabled).toBe(false)
  })

  it("clicking search calls API searchLibrary with query + limit", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const wrapper = mount(KnowledgeSearchPanel)
    await wrapper.get('[data-testid="search-query-input"]').setValue("IMRT")
    await wrapper.get('[data-testid="search-limit-select"]').setValue("20")
    await wrapper.get('[data-testid="search-run-btn"]').trigger("click")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.searchLibrary).toHaveBeenCalledWith("lib_1", { query: "IMRT", limit: 20 })
  })

  it("Enter in query input triggers search", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const wrapper = mount(KnowledgeSearchPanel)
    await wrapper.get('[data-testid="search-query-input"]').setValue("X")
    await wrapper.get('[data-testid="search-query-input"]').trigger("keyup.enter")
    for (let i = 0; i < 4; i++) await Promise.resolve()
    expect(api.searchLibrary).toHaveBeenCalled()
  })

  it("renders loading state when searching", () => {
    const store = useKnowledgeStore()
    store.searching = true
    const wrapper = mount(KnowledgeSearchPanel)
    expect(wrapper.text()).toContain("Searching")
  })

  it("renders no-results state when query present + empty results", () => {
    const store = useKnowledgeStore()
    store.searchQuery = "X"
    store.searchResults = []
    store.searching = false
    const wrapper = mount(KnowledgeSearchPanel)
    expect(wrapper.text()).toContain("No results")
  })

  it("renders hits with source + page range + heading + content", () => {
    const store = useKnowledgeStore()
    store.searchQuery = "IMRT"
    store.searchResults = [
      makeHit({
        source_name: "radiotherapy.pdf",
        heading_path: ["Radiation", "IMRT"],
        page_start: 12,
        page_end: 13,
        content: "IMRT uses modulated beam intensity",
      }),
    ]
    const wrapper = mount(KnowledgeSearchPanel)
    const hit = wrapper.get('[data-testid="search-hit-0"]')
    expect(hit.text()).toContain("radiotherapy.pdf")
    expect(hit.text()).toContain("pp.12–13")
    expect(hit.text()).toContain("Radiation > IMRT")
    expect(hit.text()).toContain("IMRT uses modulated beam intensity")
  })

  it("single-page hit renders 'p.N' not 'pp.N–N'", () => {
    const store = useKnowledgeStore()
    store.searchQuery = "X"
    store.searchResults = [makeHit({ page_start: 5, page_end: 5 })]
    const wrapper = mount(KnowledgeSearchPanel)
    expect(wrapper.get('[data-testid="search-hit-0"]').text()).toContain("p.5")
    expect(wrapper.get('[data-testid="search-hit-0"]').text()).not.toContain("pp.5")
  })

  it("clear button wipes input + calls store.clearSearch", async () => {
    const store = useKnowledgeStore()
    store.searchQuery = "X"
    store.searchResults = [makeHit()]
    const spy = vi.spyOn(store, "clearSearch")
    const wrapper = mount(KnowledgeSearchPanel)
    await wrapper.get('[data-testid="search-query-input"]').setValue("X")
    await wrapper.get('[data-testid="search-clear-btn"]').trigger("click")
    expect(spy).toHaveBeenCalled()
    expect((wrapper.get('[data-testid="search-query-input"]').element as HTMLInputElement).value).toBe("")
  })
})
