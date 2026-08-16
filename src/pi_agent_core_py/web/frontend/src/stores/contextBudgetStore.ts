import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as contextApi from "../api/contextBudget"
import { ApiError } from "../api/client"
import type {
  ContextBudgetEstimateRequest,
  ContextBudgetResponse,
  ContextEstimate,
} from "../types"

export const useContextBudgetStore = defineStore("contextBudget", () => {
  const budgetsBySession = ref<Record<string, ContextBudgetResponse>>({})
  const loadingSessionId = ref<string | null>(null)
  const compactingSessionId = ref<string | null>(null)
  const error = ref<string | null>(null)
  let requestVersion = 0

  const loading = computed(() => loadingSessionId.value !== null)
  const compacting = computed(() => compactingSessionId.value !== null)

  function getBudget(sessionId: string | null): ContextBudgetResponse | null {
    return sessionId ? budgetsBySession.value[sessionId] ?? null : null
  }

  async function load(sessionId: string): Promise<ContextBudgetResponse | null> {
    const version = ++requestVersion
    loadingSessionId.value = sessionId
    error.value = null
    try {
      const response = await contextApi.getContextBudget(sessionId)
      if (version !== requestVersion || response.session_id !== sessionId) return null
      budgetsBySession.value = { ...budgetsBySession.value, [sessionId]: response }
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
    error.value = null
    try {
      const response = await contextApi.estimateContextBudget(sessionId, payload)
      if (response.session_id !== sessionId) return null
      budgetsBySession.value = { ...budgetsBySession.value, [sessionId]: response }
      return response
    } catch (cause) {
      error.value = cause instanceof ApiError ? cause.detail : "Context estimate failed."
      return null
    }
  }

  function applyEvent(sessionId: string | null, event: Record<string, any>): void {
    if (!sessionId || !event.estimate) return
    const current = budgetsBySession.value[sessionId]
    budgetsBySession.value = {
      ...budgetsBySession.value,
      [sessionId]: {
        session_id: sessionId,
        provider_id: String(event.provider_id ?? current?.provider_id ?? "legacy"),
        model_id: String(event.model_id ?? current?.model_id ?? "unknown"),
        capability_source: event.capability_source ?? current?.capability_source ?? "unknown",
        estimate: event.estimate as ContextEstimate,
      },
    }
  }

  async function compact(sessionId: string): Promise<boolean> {
    if (compactingSessionId.value) return false
    compactingSessionId.value = sessionId
    error.value = null
    try {
      const response = await contextApi.compactContext(sessionId)
      budgetsBySession.value = {
        ...budgetsBySession.value,
        [sessionId]: response.budget,
      }
      return true
    } catch (cause) {
      error.value = cause instanceof ApiError ? cause.detail : "Context compaction failed."
      return false
    } finally {
      compactingSessionId.value = null
    }
  }

  function resetForSession(): void {
    requestVersion += 1
    loadingSessionId.value = null
    error.value = null
  }

  function resetWorkspace(): void {
    requestVersion += 1
    budgetsBySession.value = {}
    loadingSessionId.value = null
    compactingSessionId.value = null
    error.value = null
  }

  return {
    budgetsBySession,
    loadingSessionId,
    compactingSessionId,
    error,
    loading,
    compacting,
    getBudget,
    load,
    preview,
    applyEvent,
    compact,
    resetForSession,
    resetWorkspace,
  }
})
