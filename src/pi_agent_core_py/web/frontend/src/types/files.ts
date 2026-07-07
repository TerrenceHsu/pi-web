// Files 类型（P0-2 VirtualFileStore + P0-3 FileBlock 注入）。

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
  /** 后端通过 _classify_format 推断——前端 FileChip 据此显示是否支持。 */
  format: FileFormat
  /** 上传时间（ms） */
  created_at?: number
  session_id?: string
}

/** GET /api/sessions/{sid}/files response。 */
export interface FileListResponse {
  count: number
  files: FileRef[]
}

/**
 * POST /api/sessions/{sid}/files response。
 *
 * 全部成功 200；部分失败 207（count=成功数 + errors 数组）；全失败 4xx。
 */
export interface FileUploadResponse {
  count: number
  files: FileRef[]
  errors?: Array<{
    filename: string
    error_type: string
    error: string
  }>
}

/** DELETE /api/sessions/{sid}/files/{fid} response。 */
export interface DeleteFileResponse {
  deleted: boolean
  file_id: string
}

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
