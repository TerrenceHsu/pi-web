import type {
  SlashCommandListResponse,
  SlashCommandStartResponse,
} from "../types"
import { requestJson } from "./client"

export function listSlashCommands() {
  return requestJson<SlashCommandListResponse>("/api/slash-commands")
}

export function executeSlashCommand(sessionId: string, command: string) {
  return requestJson<SlashCommandStartResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/slash-commands`,
    {
      method: "POST",
      body: { command },
    },
  )
}
