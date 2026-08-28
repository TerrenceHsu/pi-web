// Workspace files API——上传、Markdown mutation 与 revision 乐观锁。

import type {
  DeleteFileResponse,
  FileListResponse,
  FileUploadResponse,
  UpdateTextFileResponse,
  WorkspaceFileMutationResponse,
  WorkspaceDocumentConversion,
  WorkspaceSnapshotResponse,
} from "../types"
import { requestBlob, requestJson, uploadForm } from "./client"

/** GET /api/sessions/{sid}/files——列出 session 内所有文件 metadata。 */
export function listFiles(sessionId: string) {
  return requestJson<FileListResponse>(`/api/sessions/${encodeURIComponent(sessionId)}/files`)
}

/** GET /api/sessions/{sid}/workspace——完整逻辑树 + revision。 */
export function getWorkspace(sessionId: string) {
  return requestJson<WorkspaceSnapshotResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/workspace`,
  )
}

/**
 * POST /api/sessions/{sid}/files——multipart 上传一个或多个文件。
 *
 * - 字段名 `files` 可重复（与后端 FastAPI UploadFile list 对齐）
 * - 单文件超 max_file_size → 413；session 总量超 → 413
 * - 全部失败时返回 4xx
 */
export function uploadFiles(
  sessionId: string,
  files: File[] | FileList,
  options: {
    relativeFolder?: string
    expectedWorkspaceRevision?: number
  } = {},
) {
  const formData = new FormData()
  for (const f of Array.from(files)) {
    formData.append("files", f, f.name)
  }
  return uploadForm<FileUploadResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/files`,
    formData,
    {
      query: {
        relative_folder: options.relativeFolder,
        expected_workspace_revision: options.expectedWorkspaceRevision,
      },
    },
  )
}

/** Retry an immutable Workspace document with the same fixed converter. */
export function convertWorkspaceDocument(sessionId: string, sourceFileId: string) {
  return requestJson<WorkspaceDocumentConversion>(
    `/api/sessions/${encodeURIComponent(sessionId)}` +
      `/documents/${encodeURIComponent(sourceFileId)}/convert`,
    { method: "POST" },
  )
}

/**
 * 构造文件下载 URL——FileChip 用 <a href> 触发浏览器下载。
 *
 * 不实际 fetch blob——避免大文件占内存。
 */
export function downloadFileUrl(sessionId: string, fileId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}` + `/files/${encodeURIComponent(fileId)}`
}

/** DELETE /api/sessions/{sid}/files/{fid}。 */
export function deleteFile(
  sessionId: string,
  fileId: string,
  options: {
    expectedSha256?: string
    expectedWorkspaceRevision?: number
  } = {},
) {
  return requestJson<DeleteFileResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}`,
    {
      method: "DELETE",
      query: {
        expected_sha256: options.expectedSha256,
        expected_workspace_revision: options.expectedWorkspaceRevision,
      },
    },
  )
}

/** 读取托管文本文件；当前用于 AGENT.md / Memory.md 编辑器。 */
export async function readTextFile(sessionId: string, fileId: string): Promise<string> {
  const { blob } = await requestBlob(
    `/api/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}`,
  )
  return blob.text()
}

/** 使用 sha256 乐观锁更新 AGENT.md / Memory.md。 */
export function updateTextFile(
  sessionId: string,
  fileId: string,
  content: string,
  expectedSha256: string,
  expectedWorkspaceRevision?: number,
) {
  return requestJson<UpdateTextFileResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}` +
      `/files/${encodeURIComponent(fileId)}/content`,
    {
      method: "PUT",
      body: {
        content,
        expected_sha256: expectedSha256,
        expected_workspace_revision: expectedWorkspaceRevision,
      },
    },
  )
}

/** 创建普通 Markdown；logicalPath 必须精确且不可与已有文件冲突。 */
export function createMarkdownFile(
  sessionId: string,
  logicalPath: string,
  content: string,
  expectedWorkspaceRevision?: number,
) {
  return requestJson<WorkspaceFileMutationResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/workspace/markdown`,
    {
      method: "POST",
      body: {
        logical_path: logicalPath,
        content,
        expected_workspace_revision: expectedWorkspaceRevision,
      },
    },
  )
}

/** 移动/重命名普通 Markdown 文件。 */
export function moveMarkdownFile(
  sessionId: string,
  fileId: string,
  logicalPath: string,
  options: {
    expectedSha256?: string
    expectedWorkspaceRevision?: number
  } = {},
) {
  return requestJson<WorkspaceFileMutationResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}` + `/files/${encodeURIComponent(fileId)}`,
    {
      method: "PATCH",
      body: {
        logical_path: logicalPath,
        expected_sha256: options.expectedSha256,
        expected_workspace_revision: options.expectedWorkspaceRevision,
      },
    },
  )
}
