// API barrel——re-export 全部新模块 + 保留旧 api.ts 的函数签名。
//
// 旧 .vue 用 `import * as api from "../api"`，barrel 必须包含：
//   - getState / getEvents / clearEvents / getSnapshots / getSnapshot
//   - getSession（旧单数 endpoint）
//   - getMcp / getSkills（旧函数名）
//   - getPolicyAudit / abortRun / resetState
//   - sendPrompt（旧签名：text + skillSelection）
//   - getMessages
//
// 新 stores 用细粒度 import：import { listSessions } from "../api/sessions"

import type { PromptRequest, PromptResponse, SkillSelection } from "../types"
import { ApiError, requestJson, uploadForm } from "./client"
import * as client from "./client"
import * as filesApi from "./files"
import * as mcpApi from "./mcp"
import * as messagesApi from "./messages"
import * as sessionsApi from "./sessions"
import * as skillsApi from "./skills"
import * as stateApi from "./state"

// ----- 新模块细粒度 re-export -----
export * from "./client"
export * from "./auth"
export * from "./sessions"
export * from "./messages"
export * from "./files"
export * from "./skills"
export * from "./mcp"
export * from "./websocket"
export * from "./state"
export * from "./regenerate"
export * from "./providers"
export * from "./slashCommands"
export * from "./approvals"
export * from "./contextBudget"
export * from "./codingSandbox"

// ----- 旧 api.ts 兼容签名（让 9 个 .vue 不改一行） -----
// 注意：旧 .vue 直接 import * as api 然后 api.getState() / api.sendPrompt(text, sel)
// 这里的函数名 / 签名必须与旧 api.ts 完全一致。

// State 相关（旧 api.ts 原样）
export const getState = stateApi.getState
export const getEvents = stateApi.getEvents
export const clearEvents = stateApi.clearEvents
export const getSnapshots = stateApi.getSnapshots
export const getSnapshot = stateApi.getSnapshot
export const getMcp = stateApi.getMcp
export const getPolicyAudit = stateApi.getPolicyAudit
export const abortRun = stateApi.abortRun
export const resetState = stateApi.resetState

// Sessions 旧函数名：getSession（旧 api.ts 用的是 GET /api/session 单数）
export function getSession() {
  return stateApi.getSessionLegacy()
}

// Messages / Prompt 旧签名——保留以让 ChatPanel.vue 调
// api.sendPrompt(text, skillSelection) 仍工作。
//
// 旧签名返回 { ok, messages, error? }——新 sendPrompt 返回 PromptResponse
// （超集，旧 .vue 读 resp.ok / resp.messages / resp.error 仍兼容）。
export function getMessages(sessionId?: string) {
  return messagesApi.getMessages(sessionId)
}

export function sendPrompt(
  text: string,
  skillSelection?: SkillSelection | any,
): Promise<PromptResponse> {
  const payload: PromptRequest = { text }
  if (skillSelection) {
    payload.skill_selection = skillSelection
  }
  return messagesApi.sendPrompt(payload)
}

// Skills 旧函数名：getSkills（旧 api.ts 中即此名）
export function getSkills(includePrompt = false) {
  return skillsApi.listSkills(includePrompt)
}

// ----- 直接 re-export 模块本身（让 import * as api 拿到所有函数）-----
// 上面的具名 re-export 已覆盖旧用法；这里再 export default 一次模块组合，
// 便于新代码 `import { listSessions, sendPrompt as sendPromptNew } from "../api"`
export { client, sessionsApi, messagesApi, filesApi, skillsApi, mcpApi, stateApi }

// 标记 ApiError / requestJson / uploadForm 也通过 barrel 暴露——
// store 层 `import { ApiError } from "../api"` 也能工作
export { ApiError, requestJson, uploadForm }
