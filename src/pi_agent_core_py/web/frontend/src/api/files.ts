// Files API——P0-2 VirtualFileStore + P0-3 上传 / 下载 / 删除。

import type {
  DeleteFileResponse,
  FileListResponse,
  FileUploadResponse,
} from "../types"
import { requestJson, uploadForm } from "./client"

/** GET /api/sessions/{sid}/files——列出 session 内所有文件 metadata。 */
export function listFiles(sessionId: string) {
  return requestJson<FileListResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/files`,
  )
}

/**
 * POST /api/sessions/{sid}/files——multipart 上传一个或多个文件。
 *
 * - 字段名 `files` 可重复（与后端 FastAPI UploadFile list 对齐）
 * - 单文件超 max_file_size → 413；session 总量超 → 413
 * - 全部失败时返回 4xx
 */
export function uploadFiles(sessionId: string, files: File[] | FileList) {
  const formData = new FormData()
  for (const f of Array.from(files)) {
    formData.append("files", f, f.name)
  }
  return uploadForm<FileUploadResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/files`,
    formData,
  )
}

/**
 * 构造文件下载 URL——FileChip 用 <a href> 触发浏览器下载。
 *
 * 不实际 fetch blob——避免大文件占内存。
 */
export function downloadFileUrl(sessionId: string, fileId: string): string {
  return (
    `/api/sessions/${encodeURIComponent(sessionId)}` +
    `/files/${encodeURIComponent(fileId)}`
  )
}

/** DELETE /api/sessions/{sid}/files/{fid}。 */
export function deleteFile(sessionId: string, fileId: string) {
  return requestJson<DeleteFileResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  )
}
