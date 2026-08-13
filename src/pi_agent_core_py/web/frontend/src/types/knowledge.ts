// Knowledge / RAG types — P2-R5-C.
//
// Mirrors backend Pydantic DTOs from web/knowledge/api.py:
// - LibraryResponse (R1)
// - DocumentResponse (R1 + R2-C3 metadata)
// - SessionBindingResponse / SessionBindingPutRequest (R1)
// - UploadResponse + UploadDocumentPart + UploadJobPart (R2-C3-A)
// - IngestionStatusResponse + JobSummary (R2-C3-B)
// - LibrarySearchRequest / LibrarySearchResponse / LibrarySearchResultItem (R5-B2)
//
// Status enums (per web/knowledge/models.py):
// - LibraryStatus: active | archived | deleting | failed
// - DocumentStatus: uploaded | extracting | normalizing | chunking |
//                   indexing | ready | failed | needs_ocr | deleting

export type LibraryStatus = "active" | "archived" | "deleting" | "failed"

export type DocumentStatus =
  | "uploaded"
  | "extracting"
  | "normalizing"
  | "chunking"
  | "indexing"
  | "ready"
  | "failed"
  | "needs_ocr"
  | "deleting"

export interface Library {
  id: string
  name: string
  description: string
  status: LibraryStatus | string
  created_at: number
  updated_at: number
  document_count: number
  binding_count: number
}

export interface LibraryCreateRequest {
  name: string
  description?: string
}

export interface LibraryPatchRequest {
  name?: string
  description?: string
}

export interface Document {
  id: string
  library_id: string
  source_name: string
  source_sha256: string
  source_relpath: string
  markdown_relpath: string
  mime_type: string
  size_bytes: number
  page_count: number
  status: DocumentStatus | string
  parser_version: string
  error_code: string
  created_at: number
  updated_at: number
}

export interface SessionBindingResponse {
  session_id: string
  library_ids: string[]
}

export interface UploadDocumentPart {
  id: string
  library_id: string
  source_name: string
  source_sha256: string
  size_bytes: number
  status: DocumentStatus | string
  created_at: number
  updated_at: number
}

export interface UploadJobPart {
  id: string | null
  document_id: string | null
  status: string | null
  attempt: number | null
}

export interface UploadResponse {
  document: UploadDocumentPart
  job: UploadJobPart | null
}

export interface JobSummary {
  id: string
  document_id: string
  stage: string
  status: string
  attempt: number
  started_at: number
  finished_at: number | null
  safe_error_code: string | null
}

export interface IngestionStatusResponse {
  document_id: string
  document_status: DocumentStatus | string
  latest_job: JobSummary | null
}

export interface RetryResponse {
  document_id: string
  job: JobSummary
}

export interface LibrarySearchRequest {
  query: string
  limit?: number
}

export interface LibrarySearchResultItem {
  document_id: string
  source_name: string
  chunk_id: string
  heading_path: string[]
  page_start: number
  page_end: number
  content: string
  rank: number
}

export interface LibrarySearchResponse {
  library_id: string
  query: string
  results: LibrarySearchResultItem[]
}

/**
 * UI display category derived from raw Document status.
 * Per P2-R5-A §8.2 — display only; raw status preserved on the object.
 */
export type DocumentStatusCategory =
  | "processing"
  | "indexing"
  | "ready"
  | "failed"
  | "needs_ocr"
  | "deleting"
  | "unknown"

export function categorizeDocumentStatus(status: string): DocumentStatusCategory {
  switch (status) {
    case "uploaded":
    case "extracting":
      return "processing"
    case "normalizing":
    case "chunking":
    case "indexing":
      return "indexing"
    case "ready":
      return "ready"
    case "failed":
      return "failed"
    case "needs_ocr":
      return "needs_ocr"
    case "deleting":
      return "deleting"
    default:
      return "unknown"
  }
}

/**
 * Terminal statuses — polling stops once a Document reaches one of these.
 * Per P2-R5-A §9.2.
 */
export const TERMINAL_DOCUMENT_STATUSES: ReadonlySet<string> = new Set([
  "ready",
  "failed",
  "needs_ocr",
  // 'deleting' is also terminal — the row vanishes from list view.
  "deleting",
])

export function isTerminalDocumentStatus(status: string): boolean {
  return TERMINAL_DOCUMENT_STATUSES.has(status)
}

/** Statuses that allow retry action (per backend C0 §12 + R2-C3 retry endpoint). */
export const RETRYABLE_DOCUMENT_STATUSES: ReadonlySet<string> = new Set([
  "failed",
  "needs_ocr",
])

export function isRetryableDocumentStatus(status: string): boolean {
  return RETRYABLE_DOCUMENT_STATUSES.has(status)
}
