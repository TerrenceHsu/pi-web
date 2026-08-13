// Knowledge Store — P2-R5-C.
//
// Owns:
//   - libraries[] + selectedLibraryId
//   - documents[] for the selected library
//   - per-document polling timers (terminal-status stop, unmount cleanup)
//   - upload state (PDF)
//   - search query + results
//   - current session's bound library IDs
//
// Stale-request safety (per P2-R5-A §10):
//   - All library-scoped requests carry AbortController keyed to selectedLibraryId
//   - On switch, abort all in-flight requests for previous library
//   - Response handlers verify currentLibraryId === request.libraryId before commit
//
// Polling cleanup (per P2-R5-A §9):
//   - stopAllPolling() called on modal close + library switch + unmount
//   - No orphan setInterval timers

import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as knowledgeApi from "../api/knowledge"
import { ApiError } from "../api/client"
import {
  TERMINAL_DOCUMENT_STATUSES,
  type Document,
  type Library,
  type LibrarySearchResultItem,
} from "../types/knowledge"

const POLL_INTERVAL_MS = 2000

export const useKnowledgeStore = defineStore("knowledge", () => {
  // ------------------------------------------------------------
  // State
  // ------------------------------------------------------------
  const libraries = ref<Library[]>([])
  const selectedLibraryId = ref<string | null>(null)
  const documents = ref<Document[]>([])
  const sessionBindingIds = ref<string[]>([])

  const loadingLibraries = ref(false)
  const loadingDocuments = ref(false)
  const uploadingDocument = ref(false)
  const searching = ref(false)
  const loadingBindings = ref(false)

  const error = ref<string | null>(null)
  const uploadError = ref<string | null>(null)
  const searchError = ref<string | null>(null)

  const searchQuery = ref("")
  const searchResults = ref<LibrarySearchResultItem[]>([])

  // Monotonic counter — only the latest search response is committed.
  let searchRequestId = 0

  // Map: documentId -> timer handle. Only non-terminal docs are polled.
  const pollingTimers = new Map<string, ReturnType<typeof setInterval>>()

  // AbortControllers for in-flight library-scoped requests.
  let documentsAbort: AbortController | null = null
  let searchAbort: AbortController | null = null

  // ------------------------------------------------------------
  // Getters
  // ------------------------------------------------------------

  const selectedLibrary = computed(() =>
    libraries.value.find((l) => l.id === selectedLibraryId.value) ?? null,
  )

  /** Documents that should be polled (non-terminal + belong to current library). */
  const pollingCandidates = computed(() =>
    documents.value.filter(
      (d) => !TERMINAL_DOCUMENT_STATUSES.has(d.status),
    ),
  )

  function isLibraryBound(libraryId: string): boolean {
    return sessionBindingIds.value.includes(libraryId)
  }

  // ------------------------------------------------------------
  // Library actions
  // ------------------------------------------------------------

  async function loadLibraries() {
    loadingLibraries.value = true
    error.value = null
    try {
      libraries.value = await knowledgeApi.listLibraries()
    } catch (e: any) {
      error.value = apiErrorMsg(e)
    } finally {
      loadingLibraries.value = false
    }
  }

  async function createLibrary(name: string, description = "") {
    error.value = null
    try {
      const lib = await knowledgeApi.createLibrary({ name, description })
      libraries.value = [...libraries.value, lib]
      selectedLibraryId.value = lib.id
      return lib
    } catch (e: any) {
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  async function renameLibrary(libraryId: string, name: string, description?: string) {
    error.value = null
    try {
      const lib = await knowledgeApi.updateLibrary(libraryId, {
        name,
        ...(description !== undefined ? { description } : {}),
      })
      const idx = libraries.value.findIndex((l) => l.id === libraryId)
      if (idx >= 0) libraries.value[idx] = lib
      return lib
    } catch (e: any) {
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  async function deleteLibrary(libraryId: string) {
    error.value = null
    try {
      await knowledgeApi.deleteLibrary(libraryId)
      libraries.value = libraries.value.filter((l) => l.id !== libraryId)
      // Clear selection if it pointed at the deleted library.
      if (selectedLibraryId.value === libraryId) {
        selectLibrary(null)
      }
      // Clear binding if it included this library.
      sessionBindingIds.value = sessionBindingIds.value.filter(
        (id) => id !== libraryId,
      )
    } catch (e: any) {
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  function selectLibrary(libraryId: string | null) {
    if (selectedLibraryId.value === libraryId) return
    // Stale-request safety: abort in-flight requests for previous library.
    abortDocumentsRequest()
    abortSearchRequest()
    stopAllPolling()
    selectedLibraryId.value = libraryId
    documents.value = []
    searchResults.value = []
    searchQuery.value = ""
    if (libraryId !== null) {
      void loadDocuments(libraryId)
    }
  }

  // ------------------------------------------------------------
  // Document actions
  // ------------------------------------------------------------

  async function loadDocuments(libraryId: string) {
    loadingDocuments.value = true
    error.value = null
    abortDocumentsRequest()
    documentsAbort = new AbortController()
    const requestLibId = libraryId
    try {
      const docs = await knowledgeApi.listDocuments(libraryId)
      // Stale-request check: only commit if still selected.
      if (selectedLibraryId.value !== requestLibId) return
      documents.value = docs
      // Start polling for non-terminal docs.
      schedulePollingForCurrent()
    } catch (e: any) {
      if (e?.name === "AbortError") return
      if (selectedLibraryId.value !== requestLibId) return
      error.value = apiErrorMsg(e)
    } finally {
      loadingDocuments.value = false
    }
  }

  async function uploadPdf(file: File) {
    if (!selectedLibraryId.value) return
    uploadingDocument.value = true
    uploadError.value = null
    const libId = selectedLibraryId.value
    try {
      const resp = await knowledgeApi.uploadPdf(libId, file)
      // Refresh documents list (cheaper than optimistic merge — server may
      // have applied status transitions between upload + read-back).
      if (selectedLibraryId.value === libId) {
        await loadDocuments(libId)
      }
      return resp
    } catch (e: any) {
      uploadError.value = apiErrorMsg(e)
      throw e
    } finally {
      uploadingDocument.value = false
    }
  }

  async function retryDocument(documentId: string) {
    error.value = null
    try {
      await knowledgeApi.retryIngestion(documentId)
      // Refresh the doc's status from the response, but a loadDocuments
      // refresh is more straightforward (single source of truth).
      if (selectedLibraryId.value) {
        await loadDocuments(selectedLibraryId.value)
      }
    } catch (e: any) {
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  async function deleteDocument(documentId: string) {
    error.value = null
    stopPolling(documentId)
    try {
      await knowledgeApi.deleteDocument(documentId)
      documents.value = documents.value.filter((d) => d.id !== documentId)
    } catch (e: any) {
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  // ------------------------------------------------------------
  // Polling
  // ------------------------------------------------------------

  function schedulePollingForCurrent() {
    // Stop polling for docs no longer in the list.
    const currentIds = new Set(documents.value.map((d) => d.id))
    for (const docId of pollingTimers.keys()) {
      if (!currentIds.has(docId)) {
        stopPolling(docId)
      }
    }
    // Start polling for non-terminal docs.
    for (const doc of pollingCandidates.value) {
      if (!pollingTimers.has(doc.id)) {
        startPolling(doc.id)
      }
    }
  }

  function startPolling(documentId: string) {
    if (pollingTimers.has(documentId)) return
    const handle = setInterval(() => {
      void pollOnce(documentId)
    }, POLL_INTERVAL_MS)
    pollingTimers.set(documentId, handle)
  }

  function stopPolling(documentId: string) {
    const handle = pollingTimers.get(documentId)
    if (handle !== undefined) {
      clearInterval(handle)
      pollingTimers.delete(documentId)
    }
  }

  function stopAllPolling() {
    for (const handle of pollingTimers.values()) {
      clearInterval(handle)
    }
    pollingTimers.clear()
  }

  async function pollOnce(documentId: string) {
    try {
      const status = await knowledgeApi.getIngestionStatus(documentId)
      // Find doc in current view; if not found (deleted / library switched), stop.
      const idx = documents.value.findIndex((d) => d.id === documentId)
      if (idx < 0) {
        stopPolling(documentId)
        return
      }
      const newDocStatus = status.document_status
      const oldDoc = documents.value[idx]
      if (newDocStatus !== oldDoc.status) {
        documents.value[idx] = { ...oldDoc, status: newDocStatus }
      }
      if (TERMINAL_DOCUMENT_STATUSES.has(newDocStatus)) {
        stopPolling(documentId)
      }
    } catch {
      // Network error during poll — stop polling this doc; surface no error.
      stopPolling(documentId)
    }
  }

  // ------------------------------------------------------------
  // Search
  // ------------------------------------------------------------

  async function runSearch(query: string, limit = 10) {
    if (!selectedLibraryId.value) return
    if (!query || !query.trim()) {
      searchError.value = "Query is empty."
      return
    }
    searching.value = true
    searchError.value = null
    abortSearchRequest()
    searchAbort = new AbortController()
    const requestId = ++searchRequestId
    const requestLibId = selectedLibraryId.value
    try {
      const resp = await knowledgeApi.searchLibrary(requestLibId, { query, limit })
      // Stale-request check.
      if (requestId !== searchRequestId) return
      if (selectedLibraryId.value !== requestLibId) return
      searchQuery.value = query
      searchResults.value = resp.results
    } catch (e: any) {
      if (e?.name === "AbortError") return
      if (requestId !== searchRequestId) return
      if (selectedLibraryId.value !== requestLibId) return
      searchError.value = apiErrorMsg(e)
    } finally {
      // Only clear searching if this is still the latest request.
      if (requestId === searchRequestId) {
        searching.value = false
      }
    }
  }

  function clearSearch() {
    abortSearchRequest()
    searchResults.value = []
    searchQuery.value = ""
    searchError.value = null
    searchRequestId++ // invalidate any in-flight
  }

  // ------------------------------------------------------------
  // Session binding
  // ------------------------------------------------------------

  async function loadSessionBindings(sessionId: string) {
    loadingBindings.value = true
    error.value = null
    try {
      const resp = await knowledgeApi.getSessionBindings(sessionId)
      sessionBindingIds.value = resp.library_ids
    } catch (e: any) {
      error.value = apiErrorMsg(e)
    } finally {
      loadingBindings.value = false
    }
  }

  async function bindLibraryToSession(sessionId: string, libraryId: string) {
    const before = sessionBindingIds.value
    // Optimistic update
    sessionBindingIds.value = Array.from(new Set([...before, libraryId]))
    try {
      const resp = await knowledgeApi.replaceSessionBindings(
        sessionId,
        sessionBindingIds.value,
      )
      sessionBindingIds.value = resp.library_ids
    } catch (e: any) {
      // Rollback
      sessionBindingIds.value = before
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  async function unbindLibraryFromSession(sessionId: string, libraryId: string) {
    const before = sessionBindingIds.value
    sessionBindingIds.value = before.filter((id) => id !== libraryId)
    try {
      const resp = await knowledgeApi.replaceSessionBindings(
        sessionId,
        sessionBindingIds.value,
      )
      sessionBindingIds.value = resp.library_ids
    } catch (e: any) {
      sessionBindingIds.value = before
      error.value = apiErrorMsg(e)
      throw e
    }
  }

  // ------------------------------------------------------------
  // Lifecycle — modal open / close
  // ------------------------------------------------------------

  async function onModalOpen(sessionId: string | null) {
    error.value = null
    uploadError.value = null
    searchError.value = null
    await loadLibraries()
    if (sessionId) {
      await loadSessionBindings(sessionId)
    }
  }

  function onModalClose() {
    stopAllPolling()
    abortDocumentsRequest()
    abortSearchRequest()
  }

  // ------------------------------------------------------------
  // Internal — abort helpers
  // ------------------------------------------------------------

  function abortDocumentsRequest() {
    if (documentsAbort) {
      documentsAbort.abort()
      documentsAbort = null
    }
  }

  function abortSearchRequest() {
    if (searchAbort) {
      searchAbort.abort()
      searchAbort = null
    }
  }

  // ------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------

  return {
    // state
    libraries,
    selectedLibraryId,
    documents,
    sessionBindingIds,
    loadingLibraries,
    loadingDocuments,
    uploadingDocument,
    searching,
    loadingBindings,
    error,
    uploadError,
    searchError,
    searchQuery,
    searchResults,
    // getters
    selectedLibrary,
    pollingCandidates,
    isLibraryBound,
    // library actions
    loadLibraries,
    createLibrary,
    renameLibrary,
    deleteLibrary,
    selectLibrary,
    // document actions
    loadDocuments,
    uploadPdf,
    retryDocument,
    deleteDocument,
    // search
    runSearch,
    clearSearch,
    // session binding
    loadSessionBindings,
    bindLibraryToSession,
    unbindLibraryFromSession,
    // lifecycle
    onModalOpen,
    onModalClose,
    // exposed for tests
    _pollingTimersCount: () => pollingTimers.size,
  }
})

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------

function apiErrorMsg(e: any): string {
  if (e instanceof ApiError) return e.detail
  return String(e?.message ?? e)
}
