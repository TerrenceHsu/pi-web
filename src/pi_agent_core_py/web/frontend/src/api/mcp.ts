// MCP API——P0-4 Step 2 server CRUD / test / enable-disable + tool 状态。
//
// 重要：response 类型（MCPServerSummary 等）不含 env values——只有 create request 允许 env。

import type {
  DDGSSearchSettings,
  MCPServerCreateRequest,
  MCPServerDeleteResponse,
  MCPServerListResponse,
  MCPServerSummary,
  MCPServerTestResponse,
  MCPServerToggleResponse,
  MCPToolListResponse,
  MCPToolToggleResponse,
} from "../types"
import { requestJson } from "./client"

/** GET /api/mcp/servers——列表（env_keys 而非 env）。 */
export function listMCPServers() {
  return requestJson<MCPServerListResponse>("/api/mcp/servers")
}

/** POST /api/mcp/servers——添加 server 配置；name 重复 409。 */
export function createMCPServer(payload: MCPServerCreateRequest) {
  return requestJson<MCPServerSummary>("/api/mcp/servers", {
    method: "POST",
    body: payload,
  })
}

/** PUT /api/mcp/servers/ddgs/settings——更新当前用户的内置搜索参数。 */
export function updateDDGSSettings(payload: DDGSSearchSettings) {
  return requestJson<MCPServerSummary>("/api/mcp/servers/ddgs/settings", {
    method: "PUT",
    body: payload,
  })
}

/**
 * POST /api/mcp/servers/{name}/test——临时连接，不污染 harness。
 *
 * 成功返回 tools list；失败返回 502 + error 字段。
 */
export function testMCPServer(name: string) {
  return requestJson<MCPServerTestResponse>(
    `/api/mcp/servers/${encodeURIComponent(name)}/test`,
    { method: "POST" },
  )
}

/** POST /api/mcp/servers/{name}/enable。 */
export function enableMCPServer(name: string) {
  return requestJson<MCPServerToggleResponse>(
    `/api/mcp/servers/${encodeURIComponent(name)}/enable`,
    { method: "POST" },
  )
}

/** POST /api/mcp/servers/{name}/disable。 */
export function disableMCPServer(name: string) {
  return requestJson<MCPServerToggleResponse>(
    `/api/mcp/servers/${encodeURIComponent(name)}/disable`,
    { method: "POST" },
  )
}

/**
 * DELETE /api/mcp/servers/{name}——先 disable 释放 transport；清孤儿 disabled tools。
 */
export function deleteMCPServer(name: string) {
  return requestJson<MCPServerDeleteResponse>(
    `/api/mcp/servers/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  )
}

/** GET /api/mcp/tools——含 enabled 字段（双重判断）。 */
export function listMCPTools() {
  return requestJson<MCPToolListResponse>("/api/mcp/tools")
}

/**
 * POST /api/mcp/tools/{tool_name}/enable。
 *
 * tool_name 形如 mcp__server__tool——含双下划线，encodeURIComponent 不影响 __
 * 但保留原样最安全。
 */
export function enableMCPTool(toolName: string) {
  return requestJson<MCPToolToggleResponse>(
    `/api/mcp/tools/${encodeURIComponent(toolName)}/enable`,
    { method: "POST" },
  )
}

/** POST /api/mcp/tools/{tool_name}/disable——真实生效（从 agent.tools 移除）。 */
export function disableMCPTool(toolName: string) {
  return requestJson<MCPToolToggleResponse>(
    `/api/mcp/tools/${encodeURIComponent(toolName)}/disable`,
    { method: "POST" },
  )
}
