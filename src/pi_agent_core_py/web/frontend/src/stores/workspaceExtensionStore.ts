import { defineStore } from "pinia"
import { ref } from "vue"

import * as api from "../api/workspaceExtensions"
import { ApiError } from "../api/client"
import type { WorkspaceExtensionsResponse } from "../types"

export const useWorkspaceExtensionStore = defineStore("workspaceExtensions", () => {
  const snapshot = ref<WorkspaceExtensionsResponse | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)
  let snapshotVersion = 0
  let loadVersion = 0
  let saveVersion = 0

  async function load(sessionId: string) {
    const version = ++snapshotVersion
    const loadToken = ++loadVersion
    loading.value = true
    error.value = null
    try {
      const next = await api.getWorkspaceExtensions(sessionId)
      if (version === snapshotVersion) snapshot.value = next
      return next
    } catch (e: any) {
      if (version === snapshotVersion) {
        error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      }
      throw e
    } finally {
      if (loadToken === loadVersion) loading.value = false
    }
  }

  async function save(
    sessionId: string, mcpServerNames: string[], skillNames: string[],
    toolNames = snapshot.value?.selected_tool_names ?? [],
  ) {
    const version = ++snapshotVersion
    const saveToken = ++saveVersion
    saving.value = true
    error.value = null
    try {
      const next = await api.updateWorkspaceExtensions(sessionId, {
        mcp_server_names: mcpServerNames,
        skill_names: skillNames,
        tool_names: toolNames,
      })
      if (version === snapshotVersion) snapshot.value = next
      return next
    } catch (e: any) {
      if (version === snapshotVersion) {
        error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      }
      throw e
    } finally {
      if (saveToken === saveVersion) saving.value = false
    }
  }

  async function toggleMCP(sessionId: string, name: string) {
    const current = snapshot.value
    if (!current || current.session_id !== sessionId || saving.value) return
    const names = new Set(current.selected_mcp_server_names)
    if (names.has(name)) names.delete(name)
    else names.add(name)
    return save(sessionId, [...names], current.selected_skill_names)
  }

  async function toggleSkill(sessionId: string, name: string) {
    const current = snapshot.value
    if (!current || current.session_id !== sessionId || saving.value) return
    const names = new Set(current.selected_skill_names)
    if (names.has(name)) names.delete(name)
    else names.add(name)
    return save(sessionId, current.selected_mcp_server_names, [...names])
  }

  async function toggleTool(sessionId: string, name: string) {
    const current = snapshot.value
    if (!current || current.session_id !== sessionId || saving.value) return
    const names = new Set(current.selected_tool_names ?? [])
    if (names.has(name)) names.delete(name)
    else names.add(name)
    return save(sessionId, current.selected_mcp_server_names, current.selected_skill_names, [...names])
  }

  function reset() {
    snapshotVersion += 1
    loadVersion += 1
    saveVersion += 1
    snapshot.value = null
    loading.value = false
    saving.value = false
    error.value = null
  }

  return { snapshot, loading, saving, error, load, save, toggleMCP, toggleSkill, toggleTool, reset }
})
