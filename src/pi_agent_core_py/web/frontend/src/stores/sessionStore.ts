// Session Store —— 管理会话列表 + active session。
//
// actions：
//   - loadSessions()：拉 GET /api/sessions
//   - createNewSession()：POST /api/sessions
//   - renameSession(id, title)：PATCH
//   - deleteSession(id)：DELETE——删 active 时自动切到 default
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

  async function loadSessions(preferredSessionId?: string | null) {
    loading.value = true
    error.value = null
    try {
      const resp = await sessionsApi.listSessions()
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
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    } finally {
      loading.value = false
    }
  }

  async function createNewSession(title?: string) {
    loading.value = true
    error.value = null
    try {
      const s = await sessionsApi.createSession(title ? { title } : {})
      sessions.value.unshift(s)
      activeSessionId.value = s.id
      return s
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    } finally {
      loading.value = false
    }
  }

  async function renameSession(id: string, title: string) {
    error.value = null
    try {
      const updated = await sessionsApi.renameSession(id, title)
      const idx = sessions.value.findIndex((s) => s.id === id)
      if (idx >= 0) sessions.value[idx] = updated
      return updated
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  async function deleteSession(id: string) {
    error.value = null
    try {
      await sessionsApi.deleteSession(id)
      sessions.value = sessions.value.filter((s) => s.id !== id)
      if (activeSessionId.value === id) {
        activeSessionId.value = sessions.value[0]?.id ?? null
      }
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
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
    sessions.value = []
    activeSessionId.value = null
    loading.value = false
    error.value = null
  }

  return {
    sessions,
    activeSessionId,
    loading,
    error,
    loadSessions,
    createNewSession,
    renameSession,
    deleteSession,
    setActiveSession,
    resetWorkspace,
  }
})
