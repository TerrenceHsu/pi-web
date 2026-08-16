export interface SlashCommandDefinition {
  name: string
  description: string
  requires_provider: boolean
  accepts_arguments: boolean
}

export interface SlashCommandListResponse {
  count: number
  commands: SlashCommandDefinition[]
}

export interface SlashCommandStartResponse {
  ok: true
  command: string
  request_id: string
  session_id: string
  status: "queued"
  request_url: string
  abort_url: string
}
