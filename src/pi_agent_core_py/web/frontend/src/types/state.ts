// Shared JSON values plus the live chat runtime summary.

export type JsonValue = any

export type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"

export interface AgentModelState {
  id: string
  provider: string
  api: string
}

export interface AgentStateSummary {
  running: boolean
  last_error: string | null
  agent_status: string
  queue_size: number
  turn_count: number
  message_count: number
  model: AgentModelState
  thinking_level: ThinkingLevel
  is_streaming: boolean
  streaming_message: JsonValue | null
  pending_tool_calls: string[]
  error_message: string | null
  snapshot_count: number
  event_count: number
  durable_recovery: {
    scanned: number
    completed: number
    aborted: number
    conflicts: number
  }
  auto_memory: {
    enabled: boolean
    recovery: {
      scanned: number
      completed: number
      pending: number
      conflicts: number
    }
  }
  code_continuity: {
    enabled: boolean
  }
  intent_routing: {
    enabled: boolean
    routes: ["read_only", "coding", "knowledge", "bash"]
  }
  plan_mode: {
    enabled: boolean
    execution_modes: ["direct", "plan"]
  }
}
