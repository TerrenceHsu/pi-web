// Knowledge / RAG API client — P2-R5-C.
//
// Typed wrappers around the 15 Knowledge REST endpoints (14 R1/R2/R5-B2 reused +
// 1 R5-B2 Search). All requests go through the shared client.ts (UI header
// auto-injected; ApiError raised on non-2xx).

import {
  requestJson,
  requestBlob,
  uploadForm,
} from "./client"
import type {
  Document,
  IngestionStatusResponse,
  Library,
  LibraryCreateRequest,
  LibraryPatchRequest,
  LibrarySearchRequest,
  LibrarySearchResponse,
  RetryResponse,
  SessionBindingResponse,
  UploadResponse,
} from "../types/knowledge"

// ============================================================================
// Library CRUD (R1)
// ============================================================================

export function listLibraries() {
  return requestJson<Library[]>("/api/knowledge/libraries")
}

export function createLibrary(body: LibraryCreateRequest) {
  return requestJson<Library>("/api/knowledge/libraries", {
    method: "POST",
    body,
  })
}

export function getLibrary(libraryId: string) {
  return requestJson<Library>(
    `/api/knowledge/libraries/${encodeURIComponent(libraryId)}`,
  )
}

export function updateLibrary(libraryId: string, body: LibraryPatchRequest) {
  return requestJson<Library>(
    `/api/knowledge/libraries/${encodeURIComponent(libraryId)}`,
    { method: "PATCH", body },
  )
}

export function deleteLibrary(libraryId: string) {
  return requestJson<null>(
    `/api/knowledge/libraries/${encodeURIComponent(libraryId)}`,
    { method: "DELETE" },
  )
}

// ============================================================================
// Document metadata + upload + status + retry + markdown (R2-C3)
// ============================================================================

export function listDocuments(libraryId: string) {
  return requestJson<Document[]>(
    `/api/knowledge/libraries/${encodeURIComponent(libraryId)}/documents`,
  )
}

export function getDocument(documentId: string) {
  return requestJson<Document>(
    `/api/knowledge/documents/${encodeURIComponent(documentId)}`,
  )
}

export function deleteDocument(documentId: string) {
  return requestJson<null>(
    `/api/knowledge/documents/${encodeURIComponent(documentId)}`,
    { method: "DELETE" },
  )
}

export function uploadPdf(libraryId: string, file: File) {
  const formData = new FormData()
  formData.append("file", file, file.name)
  return uploadForm<UploadResponse>(
    `/api/knowledge/libraries/${encodeURIComponent(libraryId)}/documents/upload`,
    formData,
  )
}

export function getIngestionStatus(documentId: string) {
  return requestJson<IngestionStatusResponse>(
    `/api/knowledge/documents/${encodeURIComponent(documentId)}/ingestion`,
  )
}

export function retryIngestion(documentId: string) {
  return requestJson<RetryResponse>(
    `/api/knowledge/documents/${encodeURIComponent(documentId)}/retry`,
    { method: "POST" },
  )
}

export function getDocumentMarkdownUrl(documentId: string): string {
  // Returns a URL string suitable for fetch via requestBlob (UI header injected there).
  return `/api/knowledge/documents/${encodeURIComponent(documentId)}/markdown`
}

export function fetchDocumentMarkdown(documentId: string) {
  return requestBlob(getDocumentMarkdownUrl(documentId))
}

// ============================================================================
// Session Binding (R1)
// ============================================================================

export function getSessionBindings(sessionId: string) {
  return requestJson<SessionBindingResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/knowledge-libraries`,
  )
}

export function replaceSessionBindings(sessionId: string, libraryIds: string[]) {
  return requestJson<SessionBindingResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/knowledge-libraries`,
    { method: "PUT", body: { library_ids: libraryIds } },
  )
}

// ============================================================================
// Library-scoped Search REST (R5-B2)
// ============================================================================

export function searchLibrary(libraryId: string, body: LibrarySearchRequest) {
  return requestJson<LibrarySearchResponse>(
    `/api/knowledge/libraries/${encodeURIComponent(libraryId)}/search`,
    { method: "POST", body },
  )
}
