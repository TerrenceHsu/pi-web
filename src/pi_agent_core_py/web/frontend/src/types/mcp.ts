// MCP 类型（Step 16-17 + P0-4 Step 2 server CRUD / tool enable-disable）。
//
// 重要：response 类型（MCPServerSummary / MCPToolSummary 等）**绝不**包含 env values；
//      只有 create request 允许 env。

import type { JsonValue } from "./state"

/**
 * 单个 MCP server 摘要——**response 类型**。
 *
 * - env_keys：只暴露 key 名（sorted）；env value 永远不在 response 中
 * - enabled：用户期望状态；enabled=true 不一定代表 runtime attach 成功
 *   （last_error 反映失败）
 */
export interface MCPServerSummary {
  name: string
  command: string
  args: string[]
  enabled: boolean
  desired_enabled?: boolean
  attached?: boolean
  restore_status?: string
  missing_env_keys?: string[]
  last_error: string | null
  tool_count: number
  env_keys: string[]
  builtin?: boolean
  deletable?: boolean
  settings?: DDGSSearchSettings | Record<string, never>
}

/** Built-in DDGS defaults. These values are enforced by the MCP process. */
export interface DDGSSearchSettings {
  max_results: number
  region: string
  safesearch: "on" | "moderate" | "off"
  timelimit: "d" | "w" | "m" | "y" | null
  timeout_seconds: number
  backend: "auto" | "duckduckgo"
}

/** POST /api/mcp/servers body——允许传 env values。 */
export interface MCPServerCreateRequest {
  name: string
  command: string
  args?: string[]
  env?: Record<string, string>
  enabled?: boolean
}

/** GET /api/mcp/servers response。 */
export interface MCPServerListResponse {
  count: number
  servers: MCPServerSummary[]
}

/** MCP tool schema——来自 server tools/list。 */
export interface MCPToolSchema {
  name: string
  description: string
  input_schema: JsonValue
}

/** POST /api/mcp/servers/{name}/test 成功 response。 */
export interface MCPServerTestResponse {
  ok: boolean
  server: string
  tools?: MCPToolSchema[]
  tool_count?: number
  /** 失败时（ok=false）含 error */
  error?: string
}

/** POST /api/mcp/servers/{name}/enable | disable response。 */
export type MCPServerToggleResponse = MCPServerSummary

/**
 * MCP tool 摘要——GET /api/mcp/tools。
 *
 * enabled = (tool_name not in disabled_mcp_tools set)
 *           AND (tool 当前在 agent.tools 中)
 */
export interface MCPToolSummary {
  name: string
  server: string
  mcp_tool: string
  description: string
  enabled: boolean
}

/** GET /api/mcp/tools response。 */
export interface MCPToolListResponse {
  attached: boolean
  tools: MCPToolSummary[]
  count: number
}

/** POST /api/mcp/tools/{tool_name}/enable | disable response。 */
export interface MCPToolToggleResponse {
  tool_name: string
  enabled: boolean
}

/** DELETE /api/mcp/servers/{name} response。 */
export interface MCPServerDeleteResponse {
  deleted: boolean
  name: string
  cleaned_disabled_tools: string[]
}

// ============================================================================
// 旧 DeveloperDrawer 用的兼容类型（保留以让 McpPanel.vue 编译过）
// ============================================================================

/** 旧 GET /api/mcp response 中的 server 项。 */
export interface McpServerStateCompat {
  name: string
  connected: boolean
  tool_count: number
  last_error: string | null
  metadata: JsonValue
}

/** 旧 GET /api/mcp response 中的 tool 项（不含 enabled 字段）。 */
export interface McpToolSummaryCompat {
  name: string
  server: string
  mcp_tool: string
  description: string
}

/** 旧 GET /api/mcp response 中的 prompt 项。 */
export interface McpPromptSummaryCompat {
  server: string
  name: string
  description: string
}

/** 旧 GET /api/mcp response——DeveloperDrawer/McpPanel 用。 */
export interface McpResponse {
  attached: boolean
  servers: McpServerStateCompat[]
  tools: McpToolSummaryCompat[]
  prompts: McpPromptSummaryCompat[]
}
