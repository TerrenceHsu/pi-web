// MCP Store —— 管理 server CRUD + tool enable/disable。
//
// 重要：response 类型不含 env values——只有 create payload 允许 env。
// testServer 不污染 harness 当前已启用 server。

import { defineStore } from "pinia"
import { ref } from "vue"

import * as mcpApi from "../api/mcp"
import { ApiError } from "../api/client"
import type {
  DDGSSearchSettings,
  MCPServerCreateRequest,
  MCPServerSummary,
  MCPToolSummary,
} from "../types"

export interface McpTestResult {
  toolCount?: number
  error?: string | null
}

export const useMcpStore = defineStore("mcp", () => {
  const servers = ref<MCPServerSummary[]>([])
  const tools = ref<MCPToolSummary[]>([])
  const loading = ref(false)
  const testingServerName = ref<string | null>(null)
  const error = ref<string | null>(null)
  /**
   * 按 server name 存最近一次 test connection 结果——多 server 连续 test
   * 不会互相覆盖。仅前端态，不进 persistence。
   */
  const lastTestResultByServer = ref<Record<string, McpTestResult>>({})

  async function loadServers() {
    loading.value = true
    error.value = null
    try {
      const resp = await mcpApi.listMCPServers()
      servers.value = resp.servers
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    } finally {
      loading.value = false
    }
  }

  /**
   * 静默 reload servers——不触发 loading flag，不动 error。
   * 用于 action 失败后 best-effort 同步后端真实状态（如 502 attach 失败时
   * 后端写到 cfg 的 last_error / tool_count）。
   */
  async function reloadServersSilent() {
    try {
      const resp = await mcpApi.listMCPServers()
      servers.value = resp.servers
    } catch {
      // best-effort——调用方已在 catch 中 set error
    }
  }

  async function createServer(payload: MCPServerCreateRequest) {
    error.value = null
    try {
      const server = await mcpApi.createMCPServer(payload)
      // 后端可能返回已有列表 + 新增项；这里简单 append / replace by name
      const idx = servers.value.findIndex((s) => s.name === server.name)
      if (idx >= 0) {
        servers.value[idx] = server
      } else {
        servers.value.push(server)
      }
      return server
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      // enabled=true + attach 失败 → 后端 502 但 cfg 已写 last_error，同步给 UI
      await reloadServersSilent()
      throw e
    }
  }

  async function testServer(name: string) {
    testingServerName.value = name
    error.value = null
    try {
      const resp = await mcpApi.testMCPServer(name)
      if (resp.ok) {
        lastTestResultByServer.value = {
          ...lastTestResultByServer.value,
          [name]: {
            toolCount: resp.tool_count ?? (resp.tools?.length ?? 0),
            error: null,
          },
        }
      } else {
        lastTestResultByServer.value = {
          ...lastTestResultByServer.value,
          [name]: {
            toolCount: 0,
            error: resp.error || "test failed",
          },
        }
      }
      return resp
    } catch (e: any) {
      const msg = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      lastTestResultByServer.value = {
        ...lastTestResultByServer.value,
        [name]: { toolCount: 0, error: msg },
      }
      error.value = msg
      // 502 时后端不会写 state（test 不污染 harness），但 server cfg 可能仍
      // 反映其它状态变化——best-effort 同步
      await reloadServersSilent()
      throw e
    } finally {
      testingServerName.value = null
    }
  }

  async function updateDDGSSettings(payload: DDGSSearchSettings) {
    error.value = null
    try {
      const server = await mcpApi.updateDDGSSettings(payload)
      const idx = servers.value.findIndex((item) => item.name === server.name)
      if (idx >= 0) servers.value[idx] = server
      await loadTools()
      return server
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      await reloadServersSilent()
      throw e
    }
  }

  async function enableServer(name: string) {
    error.value = null
    try {
      const server = await mcpApi.enableMCPServer(name)
      const idx = servers.value.findIndex((s) => s.name === name)
      if (idx >= 0) servers.value[idx] = server
      // 重新拉 tools 列表
      await loadTools()
      return server
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      // attach 失败 → 后端 cfg 写 last_error，同步给 UI
      await reloadServersSilent()
      throw e
    }
  }

  async function disableServer(name: string) {
    error.value = null
    try {
      const server = await mcpApi.disableMCPServer(name)
      const idx = servers.value.findIndex((s) => s.name === name)
      if (idx >= 0) servers.value[idx] = server
      await loadTools()
      return server
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      await reloadServersSilent()
      throw e
    }
  }

  async function deleteServer(name: string) {
    error.value = null
    try {
      await mcpApi.deleteMCPServer(name)
      servers.value = servers.value.filter((s) => s.name !== name)
      // 清掉该 server 的 lastTestResult——避免留下孤儿数据
      if (lastTestResultByServer.value[name]) {
        const next = { ...lastTestResultByServer.value }
        delete next[name]
        lastTestResultByServer.value = next
      }
      await loadTools()
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      await reloadServersSilent()
      throw e
    }
  }

  async function loadTools() {
    error.value = null
    try {
      const resp = await mcpApi.listMCPTools()
      tools.value = resp.tools
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    }
  }

  async function enableTool(toolName: string) {
    error.value = null
    try {
      const resp = await mcpApi.enableMCPTool(toolName)
      const idx = tools.value.findIndex((t) => t.name === toolName)
      if (idx >= 0) {
        tools.value[idx] = { ...tools.value[idx], enabled: resp.enabled }
      }
      return resp
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  async function disableTool(toolName: string) {
    error.value = null
    try {
      const resp = await mcpApi.disableMCPTool(toolName)
      const idx = tools.value.findIndex((t) => t.name === toolName)
      if (idx >= 0) {
        tools.value[idx] = { ...tools.value[idx], enabled: resp.enabled }
      }
      return resp
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
      throw e
    }
  }

  function resetWorkspace() {
    servers.value = []
    tools.value = []
    loading.value = false
    testingServerName.value = null
    lastTestResultByServer.value = {}
    error.value = null
  }

  return {
    servers,
    tools,
    loading,
    testingServerName,
    lastTestResultByServer,
    error,
    loadServers,
    createServer,
    testServer,
    updateDDGSSettings,
    enableServer,
    disableServer,
    deleteServer,
    loadTools,
    enableTool,
    disableTool,
    resetWorkspace,
  }
})
