// Session Store —— 管理会话列表 + active session。
//
// actions：
//   - loadSessions()：拉 GET /api/sessions
//   - createNewSession()：POST /api/sessions
//   - renameSession(id, title)：PATCH
//   - deleteSession(id)：DELETE——确认成功后移除；删 active 时切到剩余第一项
//   - setActiveSession(id)：本地切换 + 触发 fileStore / chatStore 重新加载

import { defineStore } from "pinia"
import { ref } from "vue"

import * as sessionsApi from "../api/sessions"
import { ApiError } from "../api/client"
import type { SessionSummary } from "../types"

export const useSessionStore = defineStore("sessions", () => {
  const sessions = ref<SessionSummary[]>([])
  const activeSessionId = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const deletingSessionIds = ref<string[]>([])
  let workspaceVersion = 0
  let sessionsRevision = 0
  let listLoadVersion = 0

  async function loadSessions(preferredSessionId?: string | null) {
    const workspace = workspaceVersion
    const revision = sessionsRevision
    const load = ++listLoadVersion
    loading.value = true
    error.value = null
    try {
      const resp = await sessionsApi.listSessions()
      // An older list response must not resurrect a successfully deleted row,
      // or replace the session list after an account/workspace switch.
      if (workspace !== workspaceVersion || revision !== sessionsRevision || load !== listLoadVersion) return
      sessions.value = resp.sessions
      // URL 中经过语法校验的 preferred id 仍必须存在于当前账号列表；
      // 不存在时安全回退到后端 current，再回退到第一项。
      const preferred = preferredSessionId
        ? resp.sessions.find((s) => s.id === preferredSessionId)
        : undefined
      const current = resp.sessions.find((s) => s.is_current)
      activeSessionId.value =
        preferred?.id ?? current?.id ?? resp.sessions[0]?.id ?? null
    } catch (e: any) {
      if (workspace === workspaceVersion && revision === sessionsRevision && load === listLoadVersion) {
        error.value = e instanceof ApiError ? e.detail : "Sessions could not be loaded."
      }
    } finally {
      if (workspace === workspaceVersion && load === listLoadVersion) loading.value = false
    }
  }

  async function createNewSession(title?: string) {
    const workspace = workspaceVersion
    loading.value = true
    error.value = null
    try {
      const s = await sessionsApi.createSession(title ? { title } : {})
      if (workspace !== workspaceVersion) return s
      sessionsRevision += 1
      sessions.value.unshift(s)
      activeSessionId.value = s.id
      return s
    } catch (e: any) {
      if (workspace === workspaceVersion) {
        error.value = e instanceof ApiError ? e.detail : "A new session could not be created."
      }
      throw e
    } finally {
      if (workspace === workspaceVersion) loading.value = false
    }
  }

  async function renameSession(id: string, title: string) {
    const workspace = workspaceVersion
    error.value = null
    try {
      const updated = await sessionsApi.renameSession(id, title)
      if (workspace !== workspaceVersion) return updated
      const idx = sessions.value.findIndex((s) => s.id === id)
      if (idx >= 0) sessions.value[idx] = updated
      return updated
    } catch (e: any) {
      if (workspace === workspaceVersion) {
        error.value = e instanceof ApiError ? e.detail : "Session could not be renamed."
      }
      throw e
    }
  }

  async function deleteSession(id: string): Promise<boolean> {
    if (deletingSessionIds.value.includes(id)) return false
    const workspace = workspaceVersion
    deletingSessionIds.value = [...deletingSessionIds.value, id]
    error.value = null
    try {
      const result = await sessionsApi.deleteSession(id)
      if (!result?.ok) throw new Error("Session deletion was not confirmed")
      if (workspace !== workspaceVersion) return false
      sessionsRevision += 1
      sessions.value = sessions.value.filter((s) => s.id !== id)
      if (activeSessionId.value === id) {
        activeSessionId.value = sessions.value[0]?.id ?? null
      }
      return true
    } catch (e: unknown) {
      if (workspace === workspaceVersion) {
        // Never render or log arbitrary exception bodies from a destructive
        // operation. Failure leaves the row intact, so the user can retry.
        error.value = e instanceof ApiError && e.status === 409
          ? "Session is still busy or already being deleted. Please wait and try again."
          : "Session could not be deleted. Please try again."
      }
      throw e
    } finally {
      if (workspace === workspaceVersion) {
        deletingSessionIds.value = deletingSessionIds.value.filter((pending) => pending !== id)
      }
    }
  }

  function setActiveSession(id: string) {
    if (!sessions.value.some((session) => session.id === id)) return false
    if (id !== activeSessionId.value) {
      activeSessionId.value = id
    }
    return true
  }

  function resetWorkspace() {
    workspaceVersion += 1
    sessionsRevision += 1
    listLoadVersion += 1
    sessions.value = []
    activeSessionId.value = null
    loading.value = false
    error.value = null
    deletingSessionIds.value = []
  }

  return {
    sessions,
    activeSessionId,
    loading,
    error,
    deletingSessionIds,
    loadSessions,
    createNewSession,
    renameSession,
    deleteSession,
    setActiveSession,
    resetWorkspace,
  }
})
