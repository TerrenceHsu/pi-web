// Knowledge Store 单元测试——P2-R5-C §18.1。
//
// 覆盖：
//   - Library CRUD（load / create / rename / delete）
//   - Document list / upload / retry / delete
//   - Polling（start for non-terminal / skip for terminal / stop on terminal / cleanup）
//   - Search（success / empty / error / stale-request / clear on library switch）
//   - Session binding（load / bind / unbind / rollback on failure）
//   - Lifecycle（onModalOpen / onModalClose cleanup）
//   - Stale-request safety（library switch aborts in-flight）

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

import { ApiError } from "../../src/api/client"
import * as knowledgeApi from "../../src/api/knowledge"
import { useKnowledgeStore } from "../../src/stores/knowledgeStore"
import type { Document, Library } from "../../src/types/knowledge"

// ----------------------------------------------------------------------------
// Mocks
// ----------------------------------------------------------------------------

vi.mock("../../src/api/knowledge", () => ({
  listLibraries: vi.fn(),
  createLibrary: vi.fn(),
  getLibrary: vi.fn(),
  updateLibrary: vi.fn(),
  deleteLibrary: vi.fn(),
  listDocuments: vi.fn(),
  getDocument: vi.fn(),
  deleteDocument: vi.fn(),
  uploadPdf: vi.fn(),
  getIngestionStatus: vi.fn(),
  retryIngestion: vi.fn(),
  getDocumentMarkdownUrl: vi.fn(),
  fetchDocumentMarkdown: vi.fn(),
  getSessionBindings: vi.fn(),
  replaceSessionBindings: vi.fn(),
  searchLibrary: vi.fn(),
}))

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
  requestJson: vi.fn(),
  uploadForm: vi.fn(),
  requestBlob: vi.fn(),
}))

// ----------------------------------------------------------------------------
// Fixtures
// ----------------------------------------------------------------------------

function makeLibrary(overrides: Partial<Library> = {}): Library {
  return {
    id: "lib_1",
    name: "Library 1",
    description: "",
    status: "active",
    created_at: 0,
    updated_at: 0,
    document_count: 0,
    binding_count: 0,
    ...overrides,
  }
}

function makeDocument(overrides: Partial<Document> = {}): Document {
  return {
    id: "doc_1",
    library_id: "lib_1",
    source_name: "report.pdf",
    source_sha256: "abc",
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
    ...overrides,
  }
}

function makeApiError(detail: string, status = 400): ApiError {
  return new ApiError(status, detail, { detail })
}

// Fake timers (vi.useFakeTimers) intercept setTimeout, so a Promise based on
// setTimeout(0) would never resolve under test. Microtasks (Promise.resolve)
// still drain — run enough of them to settle any chained `await` in the store.
async function flushAll() {
  for (let i = 0; i < 16; i++) {
    await Promise.resolve()
  }
}

// ----------------------------------------------------------------------------
// Setup
// ----------------------------------------------------------------------------

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

// ----------------------------------------------------------------------------
// Library CRUD
// ----------------------------------------------------------------------------

describe("Library CRUD", () => {
  it("loadLibraries success populates libraries", async () => {
    const libs = [makeLibrary({ id: "lib_1" }), makeLibrary({ id: "lib_2" })]
    vi.mocked(knowledgeApi.listLibraries).mockResolvedValue(libs)

    const store = useKnowledgeStore()
    await store.loadLibraries()
    await flushAll()

    expect(store.libraries).toHaveLength(2)
    expect(store.loadingLibraries).toBe(false)
    expect(store.error).toBeNull()
  })

  it("loadLibraries error sets error message", async () => {
    vi.mocked(knowledgeApi.listLibraries).mockRejectedValue(
      makeApiError("Library list failed", 500),
    )

    const store = useKnowledgeStore()
    await store.loadLibraries()
    await flushAll()

    expect(store.error).toBe("Library list failed")
    expect(store.loadingLibraries).toBe(false)
  })

  it("createLibrary success appends + auto-selects", async () => {
    const created = makeLibrary({ id: "lib_new", name: "New" })
    vi.mocked(knowledgeApi.createLibrary).mockResolvedValue(created)

    const store = useKnowledgeStore()
    const result = await store.createLibrary("New", "desc")
    await flushAll()

    expect(store.libraries).toContainEqual(created)
    expect(store.selectedLibraryId).toBe("lib_new")
    expect(result).toEqual(created)
  })

  it("createLibrary error sets error + rethrows", async () => {
    vi.mocked(knowledgeApi.createLibrary).mockRejectedValue(
      makeApiError("Validation error", 400),
    )

    const store = useKnowledgeStore()
    await expect(store.createLibrary("x")).rejects.toThrow("Validation error")
    await flushAll()

    expect(store.error).toBe("Validation error")
  })

  it("renameLibrary success updates list entry", async () => {
    vi.mocked(knowledgeApi.updateLibrary).mockResolvedValue(
      makeLibrary({ id: "lib_1", name: "Renamed" }),
    )

    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_1", name: "Old" })]
    await store.renameLibrary("lib_1", "Renamed")
    await flushAll()

    expect(store.libraries[0].name).toBe("Renamed")
  })

  it("deleteLibrary success removes from list + clears selection + clears binding", async () => {
    vi.mocked(knowledgeApi.deleteLibrary).mockResolvedValue(null)
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([])

    const store = useKnowledgeStore()
    store.libraries = [
      makeLibrary({ id: "lib_1" }),
      makeLibrary({ id: "lib_2" }),
    ]
    store.selectedLibraryId = "lib_1"
    store.sessionBindingIds = ["lib_1", "lib_2"]
    await store.deleteLibrary("lib_1")
    await flushAll()

    expect(store.libraries).toHaveLength(1)
    expect(store.selectedLibraryId).toBeNull()
    expect(store.sessionBindingIds).toEqual(["lib_2"])
  })

  it("deleteLibrary error rethrows + sets error", async () => {
    vi.mocked(knowledgeApi.deleteLibrary).mockRejectedValue(
      makeApiError("library_ingestion_active", 409),
    )

    const store = useKnowledgeStore()
    store.libraries = [makeLibrary({ id: "lib_1" })]
    await expect(store.deleteLibrary("lib_1")).rejects.toThrow()
    await flushAll()

    expect(store.libraries).toHaveLength(1)
    expect(store.error).toBe("library_ingestion_active")
  })
})

// ----------------------------------------------------------------------------
// Document actions
// ----------------------------------------------------------------------------

describe("Document actions", () => {
  it("loadDocuments success populates documents + starts polling for non-terminal", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "indexing" }),
      makeDocument({ id: "doc_b", status: "ready" }),
    ])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()

    expect(store.documents).toHaveLength(2)
    // Only the non-terminal doc is polled.
    expect(store._pollingTimersCount()).toBe(1)
  })

  it("loadDocuments error sets error", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockRejectedValue(
      makeApiError("Library not found", 404),
    )

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()

    expect(store.error).toBe("Library not found")
  })

  it("uploadPdf success reloads documents for the active library", async () => {
    vi.mocked(knowledgeApi.uploadPdf).mockResolvedValue({
      document: {
        id: "doc_new",
        library_id: "lib_1",
        source_name: "x.pdf",
        source_sha256: "x",
        size_bytes: 1,
        status: "uploaded",
        created_at: 0,
        updated_at: 0,
      },
      job: null,
    })
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const file = new File(["%PDF-1.4"], "x.pdf", { type: "application/pdf" })
    await store.uploadPdf(file)
    await flushAll()

    expect(knowledgeApi.uploadPdf).toHaveBeenCalledWith("lib_1", file)
    expect(knowledgeApi.listDocuments).toHaveBeenCalledWith("lib_1")
    expect(store.uploadingDocument).toBe(false)
  })

  it("uploadPdf error sets uploadError + rethrows", async () => {
    vi.mocked(knowledgeApi.uploadPdf).mockRejectedValue(
      makeApiError("upload_too_large", 413),
    )

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    const file = new File(["%PDF-1.4"], "big.pdf", { type: "application/pdf" })
    await expect(store.uploadPdf(file)).rejects.toThrow("upload_too_large")
    await flushAll()

    expect(store.uploadError).toBe("upload_too_large")
  })

  it("retryDocument success reloads documents", async () => {
    vi.mocked(knowledgeApi.retryIngestion).mockResolvedValue({
      document_id: "doc_a",
      job: {
        id: "job_1",
        document_id: "doc_a",
        stage: "extract",
        status: "queued",
        attempt: 1,
        started_at: 0,
        finished_at: null,
        safe_error_code: null,
      },
    })
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.retryDocument("doc_a")
    await flushAll()

    expect(knowledgeApi.retryIngestion).toHaveBeenCalledWith("doc_a")
    expect(knowledgeApi.listDocuments).toHaveBeenCalledWith("lib_1")
  })

  it("deleteDocument success removes from list + stops polling", async () => {
    vi.mocked(knowledgeApi.deleteDocument).mockResolvedValue(null)
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "indexing" }),
    ])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()
    expect(store._pollingTimersCount()).toBe(1)

    await store.deleteDocument("doc_a")
    await flushAll()

    expect(store.documents).toHaveLength(0)
    expect(store._pollingTimersCount()).toBe(0)
  })
})

// ----------------------------------------------------------------------------
// Polling
// ----------------------------------------------------------------------------

describe("Polling", () => {
  it("non-terminal doc is polled at 2s interval; status update mutates doc", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "indexing" }),
    ])
    vi.mocked(knowledgeApi.getIngestionStatus).mockResolvedValue({
      document_id: "doc_a",
      document_status: "ready",
      latest_job: null,
    })

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()
    expect(store._pollingTimersCount()).toBe(1)

    // Advance 2s → pollOnce fires.
    vi.advanceTimersByTime(2000)
    await flushAll()

    expect(knowledgeApi.getIngestionStatus).toHaveBeenCalledWith("doc_a")
    expect(store.documents[0].status).toBe("ready")
    // Reached terminal → polling stopped.
    expect(store._pollingTimersCount()).toBe(0)
  })

  it("terminal doc on load is not polled", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "ready" }),
      makeDocument({ id: "doc_b", status: "failed" }),
      makeDocument({ id: "doc_c", status: "needs_ocr" }),
    ])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()

    expect(store._pollingTimersCount()).toBe(0)
  })

  it("pollOnce ignores network error (no surface) + stops polling that doc", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "indexing" }),
    ])
    vi.mocked(knowledgeApi.getIngestionStatus).mockRejectedValue(
      makeApiError("Network", 500),
    )

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()
    expect(store._pollingTimersCount()).toBe(1)

    vi.advanceTimersByTime(2000)
    await flushAll()

    expect(store._pollingTimersCount()).toBe(0)
    // Error swallowed (no surface).
    expect(store.error).toBeNull()
  })

  it("library switch stops all polling for previous library", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "indexing" }),
    ])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()
    expect(store._pollingTimersCount()).toBe(1)

    store.selectLibrary(null)
    expect(store._pollingTimersCount()).toBe(0)
    expect(store.documents).toHaveLength(0)
  })
})

// ----------------------------------------------------------------------------
// Search
// ----------------------------------------------------------------------------

describe("Search", () => {
  it("runSearch success populates results", async () => {
    vi.mocked(knowledgeApi.searchLibrary).mockResolvedValue({
      library_id: "lib_1",
      query: "IMRT",
      results: [
        {
          document_id: "doc_a",
          source_name: "x.pdf",
          chunk_id: "chk_1",
          heading_path: ["H1", "H2"],
          page_start: 1,
          page_end: 2,
          content: "snippet",
          rank: -1.5,
        },
      ],
    })

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.runSearch("IMRT")
    await flushAll()

    expect(store.searchResults).toHaveLength(1)
    expect(store.searchQuery).toBe("IMRT")
    expect(store.searching).toBe(false)
    expect(store.searchError).toBeNull()
  })

  it("runSearch with empty query short-circuits (no API call)", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.runSearch("   ")
    await flushAll()

    expect(knowledgeApi.searchLibrary).not.toHaveBeenCalled()
    expect(store.searchError).toBe("Query is empty.")
  })

  it("runSearch with no selected library returns early", async () => {
    const store = useKnowledgeStore()
    await store.runSearch("X")
    await flushAll()
    expect(knowledgeApi.searchLibrary).not.toHaveBeenCalled()
  })

  it("runSearch error sets searchError", async () => {
    vi.mocked(knowledgeApi.searchLibrary).mockRejectedValue(
      makeApiError("library_not_ready", 409),
    )

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.runSearch("X")
    await flushAll()

    expect(store.searchError).toBe("library_not_ready")
    expect(store.searching).toBe(false)
  })

  it("stale search response is dropped when newer request wins", async () => {
    let resolveOld: (v: any) => void = () => {}
    let resolveNew: (v: any) => void = () => {}
    vi.mocked(knowledgeApi.searchLibrary)
      .mockReturnValueOnce(
        new Promise((r) => {
          resolveOld = r
        }),
      )
      .mockReturnValueOnce(
        new Promise((r) => {
          resolveNew = r
        }),
      )

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"

    // Issue two searches; second one should win.
    void store.runSearch("old")
    await flushAll()
    void store.runSearch("new")
    await flushAll()

    // Old resolves late with stale data.
    resolveOld({
      library_id: "lib_1",
      query: "old",
      results: [
        {
          document_id: "stale",
          source_name: "s.pdf",
          chunk_id: "c",
          heading_path: [],
          page_start: 0,
          page_end: 0,
          content: "stale",
          rank: 0,
        },
      ],
    })
    resolveNew({
      library_id: "lib_1",
      query: "new",
      results: [
        {
          document_id: "fresh",
          source_name: "f.pdf",
          chunk_id: "c2",
          heading_path: [],
          page_start: 0,
          page_end: 0,
          content: "fresh",
          rank: 0,
        },
      ],
    })
    await flushAll()

    expect(store.searchResults.map((r) => r.document_id)).toEqual(["fresh"])
    expect(store.searchQuery).toBe("new")
  })

  it("clearSearch wipes results + bumps request id (invalidates in-flight)", async () => {
    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    store.searchResults = [
      {
        document_id: "x",
        source_name: "x",
        chunk_id: "x",
        heading_path: [],
        page_start: 0,
        page_end: 0,
        content: "",
        rank: 0,
      },
    ]
    store.searchQuery = "old"

    store.clearSearch()

    expect(store.searchResults).toHaveLength(0)
    expect(store.searchQuery).toBe("")
    expect(store.searchError).toBeNull()
  })
})

// ----------------------------------------------------------------------------
// Session binding
// ----------------------------------------------------------------------------

describe("Session binding", () => {
  it("loadSessionBindings success populates sessionBindingIds", async () => {
    vi.mocked(knowledgeApi.getSessionBindings).mockResolvedValue({
      session_id: "sess_1",
      library_ids: ["lib_1", "lib_2"],
    })

    const store = useKnowledgeStore()
    await store.loadSessionBindings("sess_1")
    await flushAll()

    expect(store.sessionBindingIds).toEqual(["lib_1", "lib_2"])
  })

  it("bindLibraryToSession optimistic update + server confirm", async () => {
    vi.mocked(knowledgeApi.replaceSessionBindings).mockResolvedValue({
      session_id: "sess_1",
      library_ids: ["lib_1", "lib_new"],
    })

    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1"]
    await store.bindLibraryToSession("sess_1", "lib_new")
    await flushAll()

    expect(knowledgeApi.replaceSessionBindings).toHaveBeenCalledWith("sess_1", [
      "lib_1",
      "lib_new",
    ])
    expect(store.sessionBindingIds).toEqual(["lib_1", "lib_new"])
  })

  it("bindLibraryToSession error rolls back to before", async () => {
    vi.mocked(knowledgeApi.replaceSessionBindings).mockRejectedValue(
      makeApiError("validation_error", 400),
    )

    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1"]
    await expect(
      store.bindLibraryToSession("sess_1", "lib_new"),
    ).rejects.toThrow()
    await flushAll()

    expect(store.sessionBindingIds).toEqual(["lib_1"])
  })

  it("unbindLibraryFromSession success removes from list", async () => {
    vi.mocked(knowledgeApi.replaceSessionBindings).mockResolvedValue({
      session_id: "sess_1",
      library_ids: ["lib_2"],
    })

    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1", "lib_2"]
    await store.unbindLibraryFromSession("sess_1", "lib_1")
    await flushAll()

    expect(knowledgeApi.replaceSessionBindings).toHaveBeenCalledWith("sess_1", [
      "lib_2",
    ])
    expect(store.sessionBindingIds).toEqual(["lib_2"])
  })

  it("unbindLibraryFromSession error rolls back", async () => {
    vi.mocked(knowledgeApi.replaceSessionBindings).mockRejectedValue(
      makeApiError("validation_error", 400),
    )

    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1", "lib_2"]
    await expect(
      store.unbindLibraryFromSession("sess_1", "lib_1"),
    ).rejects.toThrow()
    await flushAll()

    expect(store.sessionBindingIds).toEqual(["lib_1", "lib_2"])
  })

  it("isLibraryBound reflects current sessionBindingIds", () => {
    const store = useKnowledgeStore()
    store.sessionBindingIds = ["lib_1"]
    expect(store.isLibraryBound("lib_1")).toBe(true)
    expect(store.isLibraryBound("lib_other")).toBe(false)
  })
})

// ----------------------------------------------------------------------------
// Lifecycle (onModalOpen / onModalClose)
// ----------------------------------------------------------------------------

describe("Modal lifecycle", () => {
  it("onModalOpen loads libraries + bindings (when sid provided)", async () => {
    vi.mocked(knowledgeApi.listLibraries).mockResolvedValue([])
    vi.mocked(knowledgeApi.getSessionBindings).mockResolvedValue({
      session_id: "sess_1",
      library_ids: ["lib_1"],
    })

    const store = useKnowledgeStore()
    await store.onModalOpen("sess_1")
    await flushAll()

    expect(knowledgeApi.listLibraries).toHaveBeenCalled()
    expect(knowledgeApi.getSessionBindings).toHaveBeenCalledWith("sess_1")
    expect(store.sessionBindingIds).toEqual(["lib_1"])
  })

  it("onModalOpen with null sid skips binding load", async () => {
    vi.mocked(knowledgeApi.listLibraries).mockResolvedValue([])

    const store = useKnowledgeStore()
    await store.onModalOpen(null)
    await flushAll()

    expect(knowledgeApi.listLibraries).toHaveBeenCalled()
    expect(knowledgeApi.getSessionBindings).not.toHaveBeenCalled()
  })

  it("onModalClose stops all polling + aborts in-flight", async () => {
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValue([
      makeDocument({ id: "doc_a", status: "indexing" }),
    ])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    await store.loadDocuments("lib_1")
    await flushAll()
    expect(store._pollingTimersCount()).toBe(1)

    store.onModalClose()
    expect(store._pollingTimersCount()).toBe(0)
  })
})

// ----------------------------------------------------------------------------
// Stale-request safety
// ----------------------------------------------------------------------------

describe("Stale-request safety", () => {
  it("library switch aborts in-flight documents request (late response dropped)", async () => {
    let resolveFirst: (v: any) => void = () => {}
    vi.mocked(knowledgeApi.listDocuments)
      .mockReturnValueOnce(
        new Promise((r) => {
          resolveFirst = r
        }),
      )
      .mockResolvedValueOnce([])

    const store = useKnowledgeStore()
    store.selectedLibraryId = "lib_1"
    // Fire loadDocuments for lib_1 (pending).
    void store.loadDocuments("lib_1")
    await flushAll()

    // Switch to lib_2 before lib_1 returns.
    vi.mocked(knowledgeApi.listDocuments).mockResolvedValueOnce([
      makeDocument({ id: "doc_b", library_id: "lib_2", status: "ready" }),
    ])
    store.selectLibrary("lib_2")
    await flushAll()

    // Late lib_1 response arrives — must NOT mutate store.documents.
    resolveFirst([
      makeDocument({ id: "stale_doc", library_id: "lib_1", status: "ready" }),
    ])
    await flushAll()

    const ids = store.documents.map((d) => d.id)
    expect(ids).not.toContain("stale_doc")
  })
})
