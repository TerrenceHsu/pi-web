// File Store —— 管理每个 session 的上传文件 + 当前 composer 的待发附件。
//
// state：
//   - filesBySession: Record<sessionId, FileRef[]>
//   - pendingAttachments: 当前 composer 暂存的附件（发送时一并提交）
//
// actions:
//   - loadFiles(sessionId)
//   - uploadFiles(sessionId, FileList | File[])
//   - removePendingAttachment(fileId)
//   - clearPendingAttachments()
//   - deleteFile(sessionId, fileId)

import { defineStore } from "pinia"
import { ref } from "vue"

import * as filesApi from "../api/files"
import { ApiError } from "../api/client"
import type { FileRef } from "../types"

export const useFileStore = defineStore("files", () => {
  const filesBySession = ref<Record<string, FileRef[]>>({})
  const pendingAttachments = ref<FileRef[]>([])
  const loading = ref(false)
  const uploading = ref(false)
  const error = ref<string | null>(null)

  async function loadFiles(sessionId: string) {
    loading.value = true
    error.value = null
    try {
      const resp = await filesApi.listFiles(sessionId)
      filesBySession.value[sessionId] = resp.files
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    } finally {
      loading.value = false
    }
  }

  async function uploadFiles(sessionId: string, files: FileList | File[]) {
    uploading.value = true
    error.value = null
    try {
      const resp = await filesApi.uploadFiles(sessionId, files)
      // 合并到 session 缓存
      const existing = filesBySession.value[sessionId] ?? []
      filesBySession.value[sessionId] = [...existing, ...resp.files]
      // 同时进入 pending（用户可在 composer 中移除）
      pendingAttachments.value.push(...resp.files)
      return resp
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    } finally {
      uploading.value = false
    }
  }

  function removePendingAttachment(fileId: string) {
    pendingAttachments.value = pendingAttachments.value.filter(
      (f) => f.id !== fileId,
    )
  }

  function clearPendingAttachments() {
    pendingAttachments.value = []
  }

  async function deleteFile(sessionId: string, fileId: string) {
    error.value = null
    try {
      await filesApi.deleteFile(sessionId, fileId)
      const existing = filesBySession.value[sessionId] ?? []
      filesBySession.value[sessionId] = existing.filter((f) => f.id !== fileId)
      // 同时从 pending 中移除
      removePendingAttachment(fileId)
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  /** session 切换时调用——清空当前 pending 避免跨 session 污染。 */
  function resetForSession() {
    pendingAttachments.value = []
  }

  return {
    filesBySession,
    pendingAttachments,
    loading,
    uploading,
    error,
    loadFiles,
    uploadFiles,
    removePendingAttachment,
    clearPendingAttachments,
    deleteFile,
    resetForSession,
  }
})
