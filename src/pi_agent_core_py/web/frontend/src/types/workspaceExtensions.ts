import type { MCPServerSummary } from "./mcp"
import type { SkillSummary } from "./skills"

export interface WorkspaceMCPServer extends MCPServerSummary {
  available: boolean
  selected: boolean
}

export interface WorkspaceSkill extends SkillSummary {
  available: boolean
  selected: boolean
}

export interface WorkspaceExtensionsResponse {
  session_id: string
  configured: boolean
  mcp_servers: WorkspaceMCPServer[]
  skills: WorkspaceSkill[]
  selected_mcp_server_names: string[]
  selected_skill_names: string[]
  tools?: { name: string; label: string; available: boolean; selected: boolean; reason: string | null }[]
  selected_tool_names?: string[]
}

export interface WorkspaceExtensionsUpdate {
  mcp_server_names: string[]
  skill_names: string[]
  tool_names?: string[]
}
