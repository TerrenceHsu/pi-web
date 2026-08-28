// Files 类型（WorkspaceStore + FileBlock 注入）。

/** Session Workspace 的持久化乐观锁状态。 */
export interface WorkspaceState {
  schema_version: 1
  session_id: string
  revision: number
  created_at: number
  updated_at: number
}

/** 文件被分类后的 format——后端 tools/view_file.py 的 _classify_format 决定。 */
export type FileFormat =
  | "markdown"
  | "html"
  | "csv"
  | "parquet"
  | "text"
  | "pdf"
  | "image_unsupported"
  | "binary"
  | "unsupported"
  | string

/** 单文件 metadata——后端 VirtualFileStore.FileRef。 */
export interface FileRef {
  id: string
  name: string
  mime: string
  size: number
  sha256: string
  /** Session 文件树中的逻辑路径；不会暴露后端物理路径。 */
  logical_path?: string
  origin?: "system" | "upload" | "agent" | "user" | "legacy"
  purpose?: "file" | "agent_instructions" | "memory" | "document_original" | "document_conversion"
  /** 后端通过 _classify_format 推断——前端 FileChip 据此显示是否支持。 */
  format: FileFormat
  /** 上传时间（ms） */
  created_at?: number
  updated_at?: number
  session_id?: string
}

/** GET /api/sessions/{sid}/files response。 */
export interface FileListResponse {
  count: number
  files: FileRef[]
  workspace: WorkspaceState
}

/** GET /api/sessions/{sid}/workspace response。 */
export type WorkspaceSnapshotResponse = FileListResponse

/**
 * POST /api/sessions/{sid}/files response。
 *
 * 全部成功 200；部分失败 207（count=成功数 + errors 数组）；全失败 4xx。
 */
export interface FileUploadResponse {
  count: number
  files: FileRef[]
  conversions?: WorkspaceDocumentConversion[]
  errors?: Array<{
    filename: string
    error_type: string
    error: string
    expected_revision?: number
    current_revision?: number
  }>
  workspace: WorkspaceState
}

/** Fixed PDF/DOCX/XLSX conversion outcome for one immutable source. */
export interface WorkspaceDocumentConversion {
  source_file_id: string
  document_id: string
  status: "succeeded" | "needs_ocr" | "failed"
  reused: boolean
  workspace_revision: number
  manifest_file_id?: string | null
  primary_file_id?: string | null
  files: FileRef[]
  warnings: string[]
  error_code?: string | null
}

/** DELETE /api/sessions/{sid}/files/{fid} response。 */
export interface DeleteFileResponse {
  deleted: boolean
  file_id: string
  workspace: WorkspaceState
}

/** PUT /api/sessions/{sid}/files/{fid}/content response。 */
export interface UpdateTextFileResponse {
  file: FileRef
  workspace: WorkspaceState
}

/** Markdown create/move 使用相同的文件 + revision envelope。 */
export type WorkspaceFileMutationResponse = UpdateTextFileResponse

/**
 * UserMessage.content 中 FileBlock——与 src/pi_agent_core_py/messages.py 同步。
 * 前端不构造 FileBlock（由后端 POST /api/prompt 时注入），只读。
 */
export interface FileBlock {
  type: "file"
  file_id: string
  name: string
  mime: string
  size: number
  sha256: string
  format: FileFormat
}
