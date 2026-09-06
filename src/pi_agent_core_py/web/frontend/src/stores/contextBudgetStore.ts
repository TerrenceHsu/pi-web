import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as contextApi from "../api/contextBudget"
import { ApiError } from "../api/client"
import type {
  ContextBudgetEstimateRequest,
  ContextBudgetResponse,
  ContextCompactionStatus,
  ContextEstimate,
} from "../types"

export const useContextBudgetStore = defineStore("contextBudget", () => {
  const budgetsBySession = ref<Record<string, ContextBudgetResponse>>({})
  const compactionsBySession = ref<Record<string, ContextCompactionStatus>>({})
  const loadingSessionId = ref<string | null>(null)
  const compactingSessionId = ref<string | null>(null)
  const settingsSessionId = ref<string | null>(null)
  const error = ref<string | null>(null)
  let requestVersion = 0
  let workspaceVersion = 0
  const statusVersions = new Map<string, number>()

  const loading = computed(() => loadingSessionId.value !== null)
  const compacting = computed(() => compactingSessionId.value !== null)

  function getBudget(sessionId: string | null): ContextBudgetResponse | null {
    return sessionId ? budgetsBySession.value[sessionId] ?? null : null
  }

  function getCompaction(sessionId: string | null): ContextCompactionStatus | null {
    return sessionId ? compactionsBySession.value[sessionId] ?? null : null
  }

  function applyCompaction(sessionId: string, status: ContextCompactionStatus | null): void {
    const previous = compactionsBySession.value[sessionId]
    if (!status) {
      const remaining = { ...compactionsBySession.value }
      delete remaining[sessionId]
      compactionsBySession.value = remaining
      return
    }
    // Lightweight budget events omit the summary. Keep it only for the exact same view.
    const sameProjection = status.active_projection_id !== null
      && status.active_projection_id === previous?.active_projection_id
    compactionsBySession.value = {
      ...compactionsBySession.value,
      [sessionId]: {
        ...(sameProjection ? previous : {}),
        ...status,
      },
    }
    const budget = budgetsBySession.value[sessionId]
    if (budget) budget.compaction = compactionsBySession.value[sessionId]
  }

  function applyBudget(sessionId: string, response: ContextBudgetResponse): void {
    if (response.compaction !== undefined
      && response.compaction?.active_projection_id !== compactionsBySession.value[sessionId]?.active_projection_id) {
      statusVersions.set(sessionId, (statusVersions.get(sessionId) ?? 0) + 1)
    }
    budgetsBySession.value = { ...budgetsBySession.value, [sessionId]: response }
    if (response.compaction !== undefined) applyCompaction(sessionId, response.compaction)
  }

  async function loadCompaction(sessionId: string): Promise<ContextCompactionStatus | null> {
    const workspace = workspaceVersion
    const version = (statusVersions.get(sessionId) ?? 0) + 1
    statusVersions.set(sessionId, version)
    try {
      const status = await contextApi.getContextCompaction(sessionId)
      if (workspace !== workspaceVersion || statusVersions.get(sessionId) !== version) return null
      if (!status) return null
      applyCompaction(sessionId, status)
      return status
    } catch (cause) {
      if (workspace === workspaceVersion && statusVersions.get(sessionId) === version) {
        error.value = cause instanceof ApiError ? cause.detail : "Working summary could not be loaded."
      }
      return null
    }
  }

  async function setAutoCompaction(sessionId: string, enabled: boolean): Promise<boolean> {
    if (settingsSessionId.value) return false
    const workspace = workspaceVersion
    settingsSessionId.value = sessionId
    // A previous GET must not restore a stale toggle after the PUT completes.
    statusVersions.set(sessionId, (statusVersions.get(sessionId) ?? 0) + 1)
    error.value = null
    try {
      const status = await contextApi.setContextAutoCompaction(sessionId, enabled)
      if (workspace !== workspaceVersion) return false
      applyCompaction(sessionId, status)
      return true
    } catch (cause) {
      if (workspace === workspaceVersion) {
        error.value = cause instanceof ApiError ? cause.detail : "Auto-compaction setting could not be saved."
      }
      return false
    } finally {
      if (workspace === workspaceVersion) settingsSessionId.value = null
    }
  }

  async function load(sessionId: string): Promise<ContextBudgetResponse | null> {
    const version = ++requestVersion
    loadingSessionId.value = sessionId
    error.value = null
    try {
      const response = await contextApi.getContextBudget(sessionId)
      if (version !== requestVersion || response.session_id !== sessionId) return null
      applyBudget(sessionId, response)
      return response
    } catch (cause) {
      if (version === requestVersion) {
        error.value = cause instanceof ApiError ? cause.detail : "Context budget could not be loaded."
      }
      return null
    } finally {
      if (version === requestVersion) loadingSessionId.value = null
    }
  }

  async function preview(
    sessionId: string,
    payload: ContextBudgetEstimateRequest,
  ): Promise<ContextBudgetResponse | null> {
    const workspace = workspaceVersion
    error.value = null
    try {
      const response = await contextApi.estimateContextBudget(sessionId, payload)
      if (workspace !== workspaceVersion || response.session_id !== sessionId) return null
      applyBudget(sessionId, response)
      return response
    } catch (cause) {
      if (workspace === workspaceVersion) {
        error.value = cause instanceof ApiError ? cause.detail : "Context estimate failed."
      }
      return null
    }
  }

  function applyEvent(sessionId: string | null, event: Record<string, any>): void {
    if (!sessionId || !event.estimate) return
    const current = budgetsBySession.value[sessionId]
    applyBudget(sessionId, {
        session_id: sessionId,
        provider_id: String(event.provider_id ?? current?.provider_id ?? "legacy"),
        model_id: String(event.model_id ?? current?.model_id ?? "unknown"),
        capability_source: event.capability_source ?? current?.capability_source ?? "unknown",
        estimate: event.estimate as ContextEstimate,
        workspace_context: current?.workspace_context ?? null,
        intent: event.intent ?? current?.intent ?? null,
        compaction: event.compaction === undefined ? current?.compaction : event.compaction,
    })
  }

  async function compact(sessionId: string): Promise<boolean> {
    if (compactingSessionId.value) return false
    const workspace = workspaceVersion
    compactingSessionId.value = sessionId
    error.value = null
    try {
      const response = await contextApi.compactContext(sessionId)
      if (workspace !== workspaceVersion || response.session_id !== sessionId) return false
      applyBudget(sessionId, response.budget)
      await loadCompaction(sessionId)
      return true
    } catch (cause) {
      if (workspace === workspaceVersion) {
        await loadCompaction(sessionId)
        if (workspace === workspaceVersion) {
          error.value = cause instanceof ApiError ? cause.detail : "Context compaction failed."
        }
      }
      return false
    } finally {
      if (workspace === workspaceVersion) compactingSessionId.value = null
    }
  }

  function resetForSession(): void {
    requestVersion += 1
    loadingSessionId.value = null
    error.value = null
  }

  function resetWorkspace(): void {
    workspaceVersion += 1
    requestVersion += 1
    statusVersions.clear()
    budgetsBySession.value = {}
    compactionsBySession.value = {}
    loadingSessionId.value = null
    compactingSessionId.value = null
    settingsSessionId.value = null
    error.value = null
  }

  return {
    budgetsBySession,
    compactionsBySession,
    loadingSessionId,
    compactingSessionId,
    settingsSessionId,
    error,
    loading,
    compacting,
    getBudget,
    getCompaction,
    loadCompaction,
    setAutoCompaction,
    load,
    preview,
    applyEvent,
    compact,
    resetForSession,
    resetWorkspace,
  }
})
