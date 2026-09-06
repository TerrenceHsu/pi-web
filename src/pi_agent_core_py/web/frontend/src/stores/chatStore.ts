// Chat Store —— 中间消息流 + WS 连接 + prompt 发送。
//
// Step 5 重写：把 WS event 完整映射为 inline ChatStreamItem。
//
// 核心策略：
//   1. sendPrompt 时乐观 push user_message + turn_info（status="running"）
//   2. WS 事件实时更新 turn cards（tool_call / tool_result / file_read /
//      mcp_tool_call / assistant streaming / error）
//   3. POST /api/prompt 成功后**不**用 final messages 覆盖——保留本轮 turn cards
//   4. 仅当 WS 断连或事件不全时，调 loadMessages(sessionId) 兜底
//
// 防御式设计：任何字段缺失/类型错都不会让页面崩。

import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as messagesApi from "../api/messages"
import * as eventsApi from "../api/events"
import * as regenerateApi from "../api/regenerate"
import * as slashCommandsApi from "../api/slashCommands"
import * as approvalsApi from "../api/approvals"
import * as plansApi from "../api/plans"
import { ApiError } from "../api/client"
import { createEventSocket, type EventSocket } from "../api/websocket"
import { useContextBudgetStore } from "./contextBudgetStore"
import type {
  AgentMessage,
  ChatStreamItem,
  FileRef,
  MCPToolCallItem,
  MessageContentWarning,
  PersistedMessageDto,
  ToolCallItem,
  ToolResultItem,
  ToolApprovalDecision,
  ToolApprovalItem,
  ToolApprovalRecord,
  FileReadItem,
  ExecutionMode,
  PlanRun,
  PlanRunItem,
  WebEvent,
  WebEventEnvelope,
} from "../types"
import { isPersistedMessageDto } from "../types/messages"
import { isWebEventEnvelope } from "../types/events"

// ============================================================================
// 工具函数
// ============================================================================

function genId(prefix: string): string {
  ChatStoreSeed.counter += 1
  return `${prefix}-${Date.now().toString(36)}-${ChatStoreSeed.counter}`
}

const ChatStoreSeed = { counter: 0 }

/**
 * D2-8.1: 模块级同步 lock——防双击 race。
 *
 * **必须在 setup 闭包外**——Pinia setup 函数内的 `let` 在某些场景下可能被多次实例化
 * （HMR / 多 Pinia instance），模块级保证全局唯一。
 *
 * 在 regenerateAssistantMessage 入口设 true，finally 设 false。
 */
let _regenerateInFlight = false

function textOf(msg: AgentMessage): string {
  if (!Array.isArray(msg?.content)) return ""
  return msg.content
    .filter((c: any) => c && c.type === "text" && typeof c.text === "string")
    .map((c: any) => c.text)
    .join("\n")
}

function contentWarningsOf(msg: AgentMessage): MessageContentWarning[] | undefined {
  if (!Array.isArray(msg.content_warnings) || msg.content_warnings.length === 0) {
    return undefined
  }
  return msg.content_warnings.filter(
    (warning) =>
      warning &&
      typeof warning.code === "string" &&
      typeof warning.replacement_character_count === "number",
  )
}

/** JSON-safe 字符串预览——限制长度防 UI 撑爆。 */
function previewOf(value: unknown, max = 240): string | undefined {
  if (value === undefined || value === null) return undefined
  let s: string
  if (typeof value === "string") {
    s = value
  } else {
    try {
      s = JSON.stringify(value)
    } catch {
      s = String(value)
    }
  }
  if (s.length > max) return s.slice(0, max) + "…"
  return s
}

/** 从 event 中提取 tool_call 信息——兼容 tool_call / toolCall / arguments 字段。 */
function extractToolCall(event: any): {
  id?: string
  name?: string
  args?: unknown
} {
  const tc = event?.tool_call || event?.toolCall
  if (tc && typeof tc === "object") {
    return {
      id: tc.id ?? tc.tool_call_id,
      name: tc.name,
      args: tc.arguments ?? tc.args ?? tc.input,
    }
  }
  // 兜底：扁平字段
  return {
    id: event?.tool_call_id ?? event?.id,
    name: event?.tool_name ?? event?.name,
    args: event?.arguments ?? event?.args ?? event?.input,
  }
}

/** 从 event.message.content 中拼出 assistant text。 */
function assistantTextOf(message: any): string {
  if (!message) return ""
  if (typeof message.content === "string") return message.content
  if (!Array.isArray(message.content)) return ""
  return message.content
    .filter((c: any) => c && c.type === "text" && typeof c.text === "string")
    .map((c: any) => c.text)
    .join("")
}

function assistantThinkingOf(message: any): { content: string; redacted: boolean } {
  if (!message || !Array.isArray(message.content)) {
    return { content: "", redacted: false }
  }
  const parts: string[] = []
  let redacted = false
  for (const block of message.content) {
    if (!block || block.type !== "thinking") continue
    if (block.redacted === true) {
      redacted = true
      continue
    }
    if (typeof block.thinking === "string") parts.push(block.thinking)
  }
  return { content: parts.join(""), redacted }
}

/** 判断 toolName 是否是文件工具。 */
function isFileTool(name: string | undefined): boolean {
  return name === "view_file" || name === "list_files" || name === "write_file"
}

/** 判断 toolName 是否是 MCP 工具（mcp__server__tool）。 */
function isMcpTool(name: string | undefined): boolean {
  return !!name && name.startsWith("mcp__")
}

/** 解析 mcp__server__tool → { serverName, toolName }。 */
function parseMcpName(name: string): { serverName?: string; toolName: string } {
  const rest = name.slice("mcp__".length)
  const idx = rest.indexOf("__")
  if (idx < 0) return { toolName: rest }
  return {
    serverName: rest.slice(0, idx),
    toolName: rest.slice(idx + 2),
  }
}

// ============================================================================
// Store
// ============================================================================

export const useChatStore = defineStore("chat", () => {
  const streamItems = ref<ChatStreamItem[]>([])
  const sending = ref(false)
  const streaming = ref(false)
  const wsConnected = ref(false)
  const wsReconnecting = ref(false)
  const error = ref<string | null>(null)
  const checkpointing = ref(false)
  const checkpointNotice = ref<string | null>(null)

  // P1-B2: 事件去重 / 隔离 state
  /**
   * 已处理过的 envelope.event_id——避免重复处理同一事件（WS 重发 / 错误重连场景）。
   * 模块级私有 Set——Pinia 不暴露非序列化 Set；与现有 toolItemIds 一致。
   *
   * P1-B2.1: 加容量上限——避免长时间运行无限增长。与后端 event_buffer_max_size
   * 对齐（默认 1000）。
   *
   * P1-B3 前置 hardening: 改为真正的 FIFO 淘汰——只淘汰最旧 ID，不清空全部历史。
   * 清空策略会让边界后旧 event 被 replay 时重复处理；FIFO 保证窗口内去重稳定。
   */
  const SEEN_EVENT_IDS_MAX = 1000
  const seenEventIds: Set<string> = new Set()
  const seenEventQueue: string[] = []

  /** 把 event_id 记入 seen set + FIFO queue；超限时淘汰最旧。返回 true 表示新见。 */
  function rememberEventId(eventId: string): boolean {
    if (seenEventIds.has(eventId)) return false
    seenEventIds.add(eventId)
    seenEventQueue.push(eventId)
    while (seenEventQueue.length > SEEN_EVENT_IDS_MAX) {
      const expired = seenEventQueue.shift()
      if (expired !== undefined) {
        seenEventIds.delete(expired)
      }
    }
    return true
  }
  /**
   * 全局 sequence cursor——所有 session 共享；用于 gap 检测。
   *
   * 后端 sequence 是全局单调（state.next_event_sequence），不分 session；
   * 因此前端也用全局 cursor 判断"中间是否丢事件"，否则跨 session 事件会误报 gap。
   * 例如 session A 收 seq=10、session B 收 seq=11、session A 再收 seq=12：
   * per-session 判断会认为 A 缺 11，但 11 实际属于 B 没丢。
   *
   * P1-B3-4: 改为 ref + expose 到 store——便于 E2E 测试重置（resetForSession 不清）。
   */
  const lastGlobalSequence = ref(0)
  /** 每个 session 最近一次看到的 envelope.sequence——保留作统计 + B3 reconnect
   * per-session replay cursor 使用，**不参与 gap 判断**。 */
  const lastSequenceBySession = ref<Record<string, number>>({})
  /** 检测到 sequence 缺口——B2 仅标记，B3 触发 replay。 */
  const gapDetected = ref(false)
  /** 当前 active request id——B2 阶段 sync 路径用 req_sync_ prefix（不强制过滤）；
   * B3 切 async 后用于隔离旧 request 事件。 */
  const currentRequestId = ref<string | null>(null)
  /** 当前 active session id（用于事件隔离）。由 setActiveSession 更新——
   * sessionStore 切换 / 新建 / 删除 session 时通过 chatStore.setActiveSession() 同步。 */
  const activeSessionId = ref<string | null>(null)

  // P1-B3-1: async prompt 竞态 + request 隔离 state
  /** await sendPromptAsync 期间为 true——handleEvent 看到 turn-control envelope
   * 且 currentRequestId=null 时缓冲到 pendingEventsByRequest。 */
  const pendingRequest = ref(false)
  /** request_id → bufferred envelopes（await 202 期间 WS 提前到达的事件）。
   * 模块级私有 Map——容量上限 100 events per queue + 5min 超时清理（简化版）。 */
  const pendingEventsByRequest: Map<string, any[]> = new Map()
  /** 收到当前 request 的 request_end——触发 status poll + loadMessages。 */
  const terminalEventSeen = ref(false)
  /** gap=true 或 replay 分页超限——terminal 后强制 loadMessages 校正。 */
  const needsFinalResync = ref(false)
  /** 用户点 Stop——只 set 此 flag；等 status=aborted 后才 finalize。 */
  const aborting = ref(false)

  // D2-7: Regeneration state——单对象，避免多个 ref 不一致
  /**
   * Regenerate 路径的状态——currentRequestId 仍存普通 prompt 的 request id，
   * 但当 operation=regenerate 时，流式 delta 必须写入独立 draft item。
   *
   * `draftItemId` 是临时 ID（`regen-draft:{request_id}`），不能用 targetMessageId
   * 否则会覆盖原 active assistant。
   */
  type RegenerationStatus =
    | "idle"
    | "queued"
    | "running"
    | "syncing"
    | "completed"
    | "error"
    | "aborted"
  interface RegenerationState {
    regenerationId: string | null
    requestId: string | null
    targetMessageId: string | null
    draftItemId: string | null
    status: RegenerationStatus
    errorMessage: string | null
  }
  const regeneration = ref<RegenerationState>({
    regenerationId: null,
    requestId: null,
    targetMessageId: null,
    draftItemId: null,
    status: "idle",
    errorMessage: null,
  })

  /**
   * D2-7: request_id → operation metadata（prompt / regenerate）。
   *
   * 用于在 handleEvent 时判断 assistant delta 应路由到普通 draft 还是 regeneration draft。
   * 由 sendPrompt / regenerateAssistantMessage / findActiveRequest 填充。
   */
  const requestMetadataById = new Map<
    string,
    {
      operation: "prompt" | "regenerate" | "checkpointer"
      targetMessageId?: string | null
      sessionId?: string | null
    }
  >()
  const terminalPolls = new Map<string, Promise<any>>()

  /** turn-control 事件——会修改当前 turn 的 draft / sending / streaming 状态；
   * 必须属于 currentRequestId 才能处理。 */
  const TURN_CONTROL_TYPES = new Set([
    "message_start", "message_update", "message_end",
    "request_end", "agent_end", "error", "agent_abort",
    "tool_execution_start", "tool_execution_update", "tool_execution_end",
    "tool_approval_requested", "tool_approval_resolved",
    "context_budget_updated",
    "turn_end", "turn_start", "agent_start",
    "request_start", "request_queued",
    "plan_run_started", "plan_created", "plan_approved", "plan_sandbox_ready",
    "plan_task_started", "plan_task_execution_submitted", "plan_task_verified",
    "plan_task_rejected", "plan_task_blocked", "plan_blocked", "plan_failed",
    "plan_cancelled", "plan_artifact_ready", "plan_completed",
  ])

  // P1-B3-2: reconnect replay state
  /** replay 进行中——新 WS event 暂存到 liveEventsDuringReplay */
  const replaying = ref(false)
  /** replay 期间 WS 收到的 live envelope——合并到 replay events 后清空 */
  let liveEventsDuringReplay: any[] = []
  let activeRecoveryToken = 0
  let recoveringActiveRequestId: string | null = null
  let liveEventsDuringActiveRecovery: WebEventEnvelope[] = []

  let socket: EventSocket | null = null

  /** 当前 turn 收到的 raw events——用于 details 展开。 */
  const currentTurnEvents = ref<WebEvent[]>([])

  /** 当前 turn 的 TurnInfoItem id——配对更新用。 */
  let currentTurnInfoId: string | null = null
  /** 当前 streaming assistant draft item id——避免重复创建。 */
  let currentAssistantItemId: string | null = null
  /** tool_call.id → streamItem.id 映射——用于 start/end 配对。 */
  const toolItemIds: Record<string, string> = {}
  /** approval_id → streamItem.id；刷新 replay / pending API 共用同一张卡。 */
  const approvalItemIds: Record<string, string> = {}
  /** 已展示过的 SkillUsedItem signature——避免重复。 */
  let lastSkillSignature: string | null = null

  const itemCount = computed(() => streamItems.value.length)
  const pendingApprovalCount = computed(
    () => streamItems.value.filter(
      (item) => item.kind === "tool_approval" && item.status === "pending",
    ).length,
  )

  // ----------------------------------------------------------------------
  // 历史消息加载
  // ----------------------------------------------------------------------

  function upsertPlanRun(plan: PlanRun): void {
    const id = `plan:${plan.id}`
    const index = streamItems.value.findIndex((item) => item.id === id)
    const previous = index >= 0 ? (streamItems.value[index] as PlanRunItem) : null
    const item: PlanRunItem = {
      kind: "plan_run",
      id,
      plan,
      submitting: previous?.submitting ?? false,
      error: previous?.error ?? null,
    }
    if (index >= 0) {
      streamItems.value[index] = item
    } else {
      streamItems.value.push(item)
    }
  }

  function toolResultMessageToItem(msg: AgentMessage, id: string): ChatStreamItem {
    const toolName = msg.name || "unknown_tool"
    const toolCallId = msg.tool_call_id
    const status = msg.is_error ? "error" : "done"
    const resultPreview = previewOf(textOf(msg))
    const contentWarnings = contentWarningsOf(msg)
    const details = msg.details ?? msg

    if (isMcpTool(toolName)) {
      const parsed = parseMcpName(toolName)
      return {
        kind: "mcp_tool_call",
        id,
        serverName: parsed.serverName,
        toolName: parsed.toolName,
        toolCallId,
        status,
        resultPreview,
        details,
        contentWarnings,
      }
    }
    if (isFileTool(toolName)) {
      return {
        kind: "file_read",
        id,
        toolName,
        toolCallId,
        status,
        preview: resultPreview,
        details,
        contentWarnings,
      }
    }
    return {
      kind: "tool_result",
      id,
      toolName,
      toolCallId,
      status,
      resultPreview,
      details,
      contentWarnings,
    }
  }

  function messageToItem(msg: AgentMessage, id: string): ChatStreamItem | null {
    if (msg.role === "user") {
      return {
        kind: "user_message",
        id,
        content: textOf(msg) || "(empty user message)",
        contentWarnings: contentWarningsOf(msg),
      }
    }
    if (msg.role === "assistant") {
      const thinking = assistantThinkingOf(msg)
      return {
        kind: "assistant_message",
        id,
        content: textOf(msg),
        thinking: thinking.content || undefined,
        thinkingRedacted: thinking.redacted || undefined,
        usage: msg.usage ?? undefined,
        generationMetrics: msg.generation_metrics,
        contentWarnings: contentWarningsOf(msg),
      }
    }
    if (msg.role === "summary") {
      return {
        kind: "context_summary",
        id,
        content: textOf(msg),
        sourceMessageCount: msg.source_message_count ?? 0,
        sourceTurnCount: msg.source_turn_count ?? 0,
        createdAt: msg.created_at,
        contentWarnings: contentWarningsOf(msg),
      }
    }
    if (msg.role === "toolResult") {
      return toolResultMessageToItem(msg, id)
    }
    // 未知自定义消息保留为淡化信息卡，避免丢失可见历史。
    return {
      kind: "turn_info",
      id,
      title: msg.role || "info",
      summary: textOf(msg) || "(no text)",
      muted: true,
      contentWarnings: contentWarningsOf(msg),
    }
  }

  /**
   * D2-7: PersistedMessageDto → ChatStreamItem，使用 dto.message_id 作为 item id。
   *
   * **关键不变量**：regenerate 成功后服务器返回同 message_id 的 DTO——前端按
   * message_id 精确匹配，**就地更新 content**，不新增第二个 assistant bubble。
   *
   * `persisted: true` 让 MessageBubble 知道此 item 可显示 Regenerate 按钮。
   */
  function persistedMessageToItem(dto: PersistedMessageDto): ChatStreamItem | null {
    // 顶层 role/content 与嵌套 message 必须来自同一 dto——审核 §1 集成注意
    const msg = dto.message
    if (msg.role === "user") {
      return {
        kind: "user_message",
        id: dto.message_id,
        messageId: dto.message_id,
        messageIndex: dto.idx,
        persisted: true,
        content: textOf(msg) || "(empty user message)",
        contentWarnings: contentWarningsOf(msg),
      }
    }
    if (msg.role === "assistant") {
      const thinking = assistantThinkingOf(msg)
      return {
        kind: "assistant_message",
        id: dto.message_id,
        messageId: dto.message_id,
        messageIndex: dto.idx,
        persisted: true,
        content: textOf(msg),
        thinking: thinking.content || undefined,
        thinkingRedacted: thinking.redacted || undefined,
        usage: msg.usage ?? undefined,
        generationMetrics: msg.generation_metrics,
        contentWarnings: contentWarningsOf(msg),
      }
    }
    if (msg.role === "summary") {
      return {
        kind: "context_summary",
        id: dto.message_id,
        content: textOf(msg),
        sourceMessageCount: msg.source_message_count ?? 0,
        sourceTurnCount: msg.source_turn_count ?? 0,
        createdAt: msg.created_at,
        contentWarnings: contentWarningsOf(msg),
      }
    }
    if (msg.role === "toolResult") {
      return toolResultMessageToItem(msg, dto.message_id)
    }
    return {
      kind: "turn_info",
      id: dto.message_id,
      title: msg.role || "info",
      summary: textOf(msg) || "(no text)",
      muted: true,
      contentWarnings: contentWarningsOf(msg),
    }
  }

  async function loadMessages(sessionId?: string) {
    error.value = null
    try {
      const resp = await messagesApi.getMessages(sessionId)
      const items: ChatStreamItem[] = []
      resp.messages.forEach((m, i) => {
        // D2-7: 优先用 persisted DTO（含 message_id）；fallback 到旧 AgentMessage
        if (isPersistedMessageDto(m)) {
          const item = persistedMessageToItem(m)
          if (item) items.push(item)
        } else {
          const item = messageToItem(m, `hist-${i}`)
          if (item) items.push(item)
        }
      })
      if (sessionId) {
        try {
          const latest = await plansApi.getLatestPlanRun(sessionId)
          if (latest.plan) {
            const planItem: PlanRunItem = {
              kind: "plan_run",
              id: `plan:${latest.plan.id}`,
              plan: latest.plan,
            }
            let insertAt = items.length
            for (let index = items.length - 1; index >= 0; index -= 1) {
              if (items[index].kind === "assistant_message") {
                insertAt = index
                break
              }
            }
            items.splice(insertAt, 0, planItem)
          }
        } catch {
          // Message history remains usable if the optional Plan control plane is disabled.
        }
      }
      if (sessionId && activeSessionId.value !== sessionId) return
      streamItems.value = items
      // reset turn-tracking 状态
      currentTurnInfoId = null
      currentAssistantItemId = null
      Object.keys(toolItemIds).forEach((k) => delete toolItemIds[k])
      lastSkillSignature = null
    } catch (e: any) {
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    }
  }

  /**
   * P1-B3-3 + D2-7: 用服务端 messages 校正 streamItems——保留当前 request 的 turn cards，
   * 替换 user_message / assistant_message 为服务端最终事实，删除 streaming draft。
   *
   * **D2-7 关键**：按 message_id 精确匹配，regenerate 成功后服务器返回同 message_id
   * 的 DTO——前端**就地更新 content**，不新增第二个 assistant bubble。
   *
   * 调用时机：
   * - request status 进入 terminal（completed/error/aborted）后
   * - gap fallback 时（needsFinalResync=true）
   * - 用户手动刷新
   */
  async function reconcileMessagesFromServer(sessionId: string) {
    try {
      const resp = await messagesApi.getMessages(sessionId)
      if (activeSessionId.value !== sessionId) return
      // 收集服务端的 user_message / assistant_message item（顺序敏感）
      // D2-7: 优先用 persisted DTO 的 message_id；fallback 到 index
      const persistedItems: ChatStreamItem[] = []
      const canonicalTimeline: ChatStreamItem[] = []
      const persistedTools = new Map<string, ChatStreamItem[]>()
      resp.messages.forEach((m, i) => {
        const canonical = isPersistedMessageDto(m)
          ? persistedMessageToItem(m)
          : messageToItem(m, `srv-${i}`)
        if (canonical) {
          canonicalTimeline.push(canonical)
          if ("toolCallId" in canonical && canonical.toolCallId) {
            const matches = persistedTools.get(canonical.toolCallId) ?? []
            matches.push(canonical)
            persistedTools.set(canonical.toolCallId, matches)
          }
        }
        if (isPersistedMessageDto(m)) {
          if (m.message.role === "user" || m.message.role === "assistant") {
            const item = persistedMessageToItem(m)
            if (item) persistedItems.push(item)
          }
        } else {
          // legacy 路径——无 message_id，用 index fallback
          if (m.role === "user" || m.role === "assistant") {
            const item = messageToItem(m, `srv-${i}`)
            if (item) persistedItems.push(item)
          }
        }
      })

      // 就地校正 message bubble，绝不能把非消息卡片抽出后统一拼到尾部。
      //
      // 旧实现 `[...persistedItems, ...turnCards]` 会在每次请求终结时，把历史
      // toolResult / MCP / file 卡片全部搬到最新回答后面。这里按现有 timeline
      // 槽位替换消息：已有 persisted bubble 依 message_id 精确更新；本轮乐观
      // user / streaming assistant 则按 role 匹配尚未出现在视图中的 canonical
      // message。所有 turn card 保持原索引和相对顺序。
      const persistedById = new Map<string, ChatStreamItem>()
      for (const item of persistedItems) {
        const messageId = (item as any).messageId
        if (typeof messageId === "string") persistedById.set(messageId, item)
      }

      const representedIds = new Set<string>()
      for (const item of streamItems.value) {
        const messageId = (item as any).messageId
        if (typeof messageId === "string" && persistedById.has(messageId)) {
          representedIds.add(messageId)
        }
      }
      const unmatched = persistedItems.filter((item) => {
        const messageId = (item as any).messageId
        return typeof messageId !== "string" || !representedIds.has(messageId)
      })
      const consumedUnmatched = new Set<number>()
      const consumedTools = new Set<string>()

      const reconciled: ChatStreamItem[] = []
      for (const existing of streamItems.value) {
        if (existing.kind !== "user_message" && existing.kind !== "assistant_message") {
          // Reload/replay may miss tool_execution_end. Restore the durable
          // result in its existing slot; approval cards are not execution cards.
          if (
            ["tool_call", "tool_result", "file_read", "mcp_tool_call"].includes(existing.kind)
            && "toolCallId" in existing && existing.toolCallId
          ) {
            const canonical = persistedTools.get(existing.toolCallId)?.find(
              (item) => !consumedTools.has(item.id),
            )
            if (canonical) {
              consumedTools.add(canonical.id)
              // Keep the mounted card kind/ID and live arguments. Switching
              // tool_call to tool_result would remount its children and lose
              // an in-flight Save/expanded-details state during final sync.
              reconciled.push({
                ...existing, ...canonical, kind: existing.kind, id: existing.id,
              } as ChatStreamItem)
              continue
            }
          }
          reconciled.push(existing)
          continue
        }

        const existingMessageId = (existing as any).messageId
        if (typeof existingMessageId === "string") {
          const canonical = persistedById.get(existingMessageId)
          // Persisted rows deleted by a server-side rewrite must disappear; otherwise
          // the same assistant can survive beside its canonical replacement.
          if (canonical) reconciled.push(canonical)
          continue
        }

        const matchingIndex = unmatched.findIndex(
          (candidate, index) => !consumedUnmatched.has(index) && candidate.kind === existing.kind,
        )
        if (matchingIndex >= 0) {
          consumedUnmatched.add(matchingIndex)
          reconciled.push(unmatched[matchingIndex])
        } else if (!(existing as any).streaming && !(existing as any).isRegenerationDraft) {
          // A non-streaming legacy item has no stable message_id. Keep it rather than
          // deleting visible history solely because an older API omitted identifiers.
          reconciled.push(existing)
        }
      }

      // Missing WS message events can leave no local bubble slot. Append only those
      // canonical messages that could not be matched; normal complete streams consume
      // every item above and retain their tool-card anchors.
      unmatched.forEach((item, index) => {
        if (!consumedUnmatched.has(index)) reconciled.push(item)
      })
      // If both start/end were missed, place the result before its next
      // canonical message, not after the final answer or at the timeline tail.
      canonicalTimeline.forEach((item, index) => {
        if (!("toolCallId" in item) || !item.toolCallId || consumedTools.has(item.id)) return
        const next = canonicalTimeline.slice(index + 1).find(
          (candidate) => reconciled.some((local) => local.id === candidate.id),
        )
        const anchor = next ? reconciled.findIndex((local) => local.id === next.id) : -1
        if (anchor >= 0) reconciled.splice(anchor, 0, item)
        else reconciled.push(item)
        consumedTools.add(item.id)
      })
      streamItems.value = reconciled

      // reset turn-tracking 部分（保留 currentTurnInfoId/currentAssistantItemId）
      Object.keys(toolItemIds).forEach((k) => delete toolItemIds[k])
      lastSkillSignature = null
    } catch (e: any) {
      // reconcile 失败——不覆盖现有 streamItems；用户可手动刷新
      error.value = e instanceof ApiError ? e.detail : String(e?.message ?? e)
    }
  }

  /**
   * P1-B3-3: request_end 后轮询 GET /api/requests/{id} 直到 terminal——避免
   * request_end 早于 SQLite 持久化导致的 loadMessages 缺最终消息。
   *
   * 间隔 250ms → 500ms；总超时 10s。terminal 后调 reconcileMessagesFromServer。
   */
  async function pollRequestUntilTerminalCore(requestId: string) {
    const startedAt = Date.now()
    let delay = 250

    // request operation 决定 terminal 后的 UI 收敛策略。
    const meta = requestMetadataById.get(requestId)
    const isRegenerate = meta?.operation === "regenerate"
    const isCheckpointer = meta?.operation === "checkpointer"
    const maxTimeoutMs = isCheckpointer ? 120_000 : 10_000

    while (Date.now() - startedAt < maxTimeoutMs) {
      await new Promise((r) => setTimeout(r, delay))
      delay = Math.min(delay * 2, 500)
      try {
        const r = await messagesApi.getRequestStatus(requestId)
        if (
          r.status === "completed" ||
          r.status === "error" ||
          r.status === "aborted"
        ) {
          const ownsActiveView =
            currentRequestId.value === requestId &&
            (!r.session_id || activeSessionId.value === r.session_id)
          if (!ownsActiveView) {
            requestMetadataById.delete(requestId)
            return r
          }
          if (isCheckpointer) {
            if (r.status === "completed") {
              // Server has already committed Memory.md and cleared canonical messages.
              // Clear every transient turn card as well so the current window is empty.
              streamItems.value = []
              currentTurnEvents.value = []
              checkpointNotice.value = "Checkpoint saved to Memory.md"
            } else {
              error.value = r.error || "Checkpoint was not completed"
            }
            checkpointing.value = false
          // D2-7: regenerate 路径——completed 先 syncing 再 reconcile
          } else if (isRegenerate && r.status === "completed") {
            regeneration.value = {
              ...regeneration.value,
              status: "syncing",
            }
            // 同步失败时保持 syncing（审核 §9 reconcile 失败语义）
            if (r.session_id) {
              try {
                await reconcileMessagesFromServer(r.session_id)
                // reconcile 成功——删 draft + status=completed
                if (regeneration.value.draftItemId) {
                  streamItems.value = streamItems.value.filter(
                    (it: any) => it.id !== regeneration.value.draftItemId,
                  )
                }
                regeneration.value = {
                  ...regeneration.value,
                  status: "completed",
                }
              } catch {
                // reconcile 失败——保持 syncing + 显示安全提示（不删 draft）
                error.value =
                  "Response regenerated — waiting for server sync"
                // 仍清 currentRequestId 让用户可发新 prompt
                sending.value = false
                streaming.value = false
                aborting.value = false
                currentRequestId.value = null
                return
              }
            } else {
              // 无 session_id——直接完成
              if (regeneration.value.draftItemId) {
                streamItems.value = streamItems.value.filter(
                  (it: any) => it.id !== regeneration.value.draftItemId,
                )
              }
              regeneration.value = {
                ...regeneration.value,
                status: "completed",
              }
            }
          } else if (isRegenerate && (r.status === "error" || r.status === "aborted")) {
            // D2-7: error/abort → 删 draft + 原回答不变
            if (regeneration.value.draftItemId) {
              streamItems.value = streamItems.value.filter(
                (it: any) => it.id !== regeneration.value.draftItemId,
              )
            }
            regeneration.value = {
              ...regeneration.value,
              status: r.status,
              errorMessage: r.error,
            }
            // 普通 reconcile 不必要（messages 没变）；但调用一次保证一致性
            if (r.session_id) {
              await reconcileMessagesFromServer(r.session_id)
            }
          } else {
            // 普通 prompt 路径——reconcile messages
            if (r.session_id) {
              await reconcileMessagesFromServer(r.session_id)
            }
            if (r.status === "error" || r.status === "aborted") {
              const message =
                r.error ||
                (r.status === "aborted"
                  ? "Request aborted"
                  : "Request finalization failed")
              error.value = message
              finalizeTurnInfo("error")
              appendError(message, r)
            }
          }

          finalizeTurnInfo(r.status === "completed" ? "done" : "error")
          // 清 state
          sending.value = false
          streaming.value = false
          aborting.value = false
          currentRequestId.value = null
          // 清 metadata（避免长期累积）
          requestMetadataById.delete(requestId)
          return r
        }
      } catch {
        // 404 / 网络——继续 poll，由 timeout 兜底
      }
    }
    // timeout——保留 draft + 显示轻量错误
    if (currentRequestId.value === requestId) {
      checkpointing.value = false
      error.value = "Request finalization timeout — please refresh to sync"
    }
    return null
  }

  function pollRequestUntilTerminal(requestId: string): Promise<any> {
    const existing = terminalPolls.get(requestId)
    if (existing) return existing
    const pending = pollRequestUntilTerminalCore(requestId).finally(() => {
      terminalPolls.delete(requestId)
    })
    terminalPolls.set(requestId, pending)
    return pending
  }

  /**
   * P1-B3-3: Stop 按钮——优先调 request-scoped abort；fallback 到旧 /api/abort。
   * 点击后只 set aborting=true；等 status=aborted（pollRequestUntilTerminal 处理）。
   */
  async function abortRun(reason = "user_requested") {
    if (currentRequestId.value) {
      aborting.value = true
      try {
        await messagesApi.abortRequest(currentRequestId.value, reason)
      } catch (e) {
        // abort 失败——不 block UI；用户可重试
        aborting.value = false
        throw e
      }
    } else {
      // fallback——没有 currentRequestId（旧路径或 pendingRequest 期间）
      // 直接 fetch /api/abort（避免与 chatStore.abortRun 命名冲突的循环 import）
      try {
        const res = await fetch("/api/abort", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason }),
        })
        if (!res.ok) {
          // 忽略——abort 失败不 block UI
        }
      } catch {
        // 忽略
      }
    }
  }

  /**
   * P1-B3-3 + D2-7: 查询 session 的 active request——页面刷新 / session 切换恢复用。
   * 返回 request_id 或 null；同时记录 metadata（operation / targetMessageId）用于
   * 流式 delta 路由和恢复 regeneration state。
   */
  async function findActiveRequest(sessionId: string): Promise<string | null> {
    try {
      const resp = await messagesApi.listActiveRequests(sessionId, 1)
      if (resp.count > 0 && resp.requests[0]) {
        const r = resp.requests[0]
        // D2-7: 记录 metadata——让后续 WS delta 正确路由
        const op = r.operation ?? "prompt"
        requestMetadataById.set(r.request_id, {
          operation: op,
          targetMessageId: r.target_message_id ?? null,
          sessionId,
        })
        return r.request_id
      }
    } catch {
      // 忽略——降级为无 active request
    }
    return null
  }

  /**
   * P1-B3-4 + D2-7: 页面刷新恢复——set currentRequestId + sending/streaming=true，
   * 让 handleEvent 把后续 envelope 关联到该 request。
   * WS reconnect 后 replay 会补播该 request 的事件。
   *
   * D2-7：若 request 是 regenerate operation，恢复 regeneration state + 独立 draft。
   */
  function resumeActiveRequest(requestId: string) {
    const meta = requestMetadataById.get(requestId)
    currentRequestId.value = requestId
    pendingRequest.value = false
    sending.value = true
    streaming.value = meta?.operation !== "checkpointer"
    terminalEventSeen.value = false
    checkpointing.value = meta?.operation === "checkpointer"

    // D2-7: 恢复 regeneration draft——审核 §8 reload 顺序
    if (meta?.operation === "regenerate") {
      const draftId = `regen-draft:${requestId}`
      // 创建独立 draft（不覆盖原 active assistant）
      streamItems.value.push({
        kind: "assistant_message",
        id: draftId,
        content: "",
        streaming: true,
        isRegenerationDraft: true,
      })
      currentAssistantItemId = draftId
      regeneration.value = {
        regenerationId: meta.targetMessageId ?? null, // 不可靠——实际 regeneration_id 需另查
        requestId,
        targetMessageId: meta.targetMessageId ?? null,
        draftItemId: draftId,
        status: "running",
        errorMessage: null,
      }
    }
  }

  /**
   * 完整页面刷新后，从服务端事件缓冲重放当前 active request 已发生的事件。
   * 查询同时带 session_id + request_id，避免其它 Session 的事件写入视图；
   * 查询期间同一 request 的 WebSocket live 事件先缓冲，最后按 sequence 合并。
   */
  async function recoverActiveRequestEvents(
    sessionId: string,
    requestId: string,
  ): Promise<void> {
    if (
      activeSessionId.value !== sessionId ||
      currentRequestId.value !== requestId
    ) {
      return
    }

    const recoveryToken = ++activeRecoveryToken
    recoveringActiveRequestId = requestId
    liveEventsDuringActiveRecovery = []
    const recovered: WebEventEnvelope[] = []
    let recoveryNeedsFinalResync = false
    let afterSequence = 0

    try {
      for (let page = 0; page < 20; page++) {
        const response = await eventsApi.getEvents({
          afterSequence,
          limit: 200,
          sessionId,
          requestId,
        })
        if (response.gap) recoveryNeedsFinalResync = true
        if (response.events.length === 0) break
        recovered.push(...response.events)
        afterSequence = response.events[response.events.length - 1].sequence
        if (!response.has_more) break
        if (page === 19) recoveryNeedsFinalResync = true
      }
    } catch {
      recoveryNeedsFinalResync = true
    } finally {
      // Session 切换或另一次恢复开始后，旧请求不得清空新请求的缓冲区，
      // 也不得推进新工作区的事件 cursor。
      if (activeRecoveryToken === recoveryToken) {
        const live = liveEventsDuringActiveRecovery
        liveEventsDuringActiveRecovery = []
        recoveringActiveRequestId = null

        const stillOwnsRecovery =
          activeSessionId.value === sessionId &&
          currentRequestId.value === requestId
        if (stillOwnsRecovery) {
          if (recoveryNeedsFinalResync) needsFinalResync.value = true

          const merged: WebEventEnvelope[] = []
          for (const envelope of recovered) {
            if (
              envelope.session_id !== sessionId ||
              envelope.request_id !== requestId ||
              !rememberEventId(envelope.event_id)
            ) {
              continue
            }
            lastGlobalSequence.value = Math.max(
              lastGlobalSequence.value,
              envelope.sequence,
            )
            const previous = lastSequenceBySession.value[sessionId] ?? 0
            lastSequenceBySession.value = {
              ...lastSequenceBySession.value,
              [sessionId]: Math.max(previous, envelope.sequence),
            }
            merged.push(envelope)
          }
          merged.push(...live)
          merged.sort((left, right) => left.sequence - right.sequence)

          const unique = new Map<string, WebEventEnvelope>()
          for (const envelope of merged) unique.set(envelope.event_id, envelope)
        for (const envelope of unique.values()) {
          if (
            envelope.session_id !== sessionId ||
            envelope.request_id !== requestId
          ) {
            continue
          }
          applyEventToStreamItems({ ...envelope.payload, type: envelope.type })
        }
        }
      }
    }
  }

  // ----------------------------------------------------------------------
  // sendPrompt
  // ----------------------------------------------------------------------

  async function sendPrompt(input: {
    sessionId?: string
    text: string
    fileIds?: string[]
    /** 本轮附件 FileRef——仅用于 user_message item 显示 FileChip；不发到后端 */
    files?: FileRef[]
    skillNames?: string[]
    codingMode?: boolean
    executionMode?: ExecutionMode
  }) {
    if (sending.value) return
    if (!input.text.trim()) return

    // P1-B2: 记录当前 active session id——handleEvent 用它做 session 过滤
    if (input.sessionId) {
      activeSessionId.value = input.sessionId
    }

    sending.value = true
    streaming.value = true
    error.value = null
    currentTurnEvents.value = []
    // 切新 turn 前清 gap 标记（前一轮的 gap 不影响本轮 UI）
    gapDetected.value = false
    // P1-B3-1: async prompt 新 state——等 202 期间 pendingRequest=true
    pendingRequest.value = true
    currentRequestId.value = null
    terminalEventSeen.value = false
    needsFinalResync.value = false
    aborting.value = false

    // 1. 乐观 push user_message——含本轮附件 FileRef[]
    streamItems.value.push({
      kind: "user_message",
      id: genId("u"),
      content: input.text,
      files: input.files && input.files.length > 0 ? input.files.slice() : undefined,
    })

    // 2. 创建本轮 TurnInfoItem placeholder
    // 新 turn 开始前扫掉上一轮已终结（done/error）的 turn_info 卡——
    // 避免多次 sendPrompt 后旧 turn 卡堆叠（finalizeTurnInfo 只改 status 不移除）。
    // 当前正在 running/queued 的 turn_info 不动（理论上不应出现，但防御）。
    streamItems.value = streamItems.value.filter(
      (it: any) =>
        !(
          it.kind === "turn_info" &&
          (it.status === "done" || it.status === "error")
        ),
    )
    const turnId = genId("t")
    currentTurnInfoId = turnId
    streamItems.value.push({
      kind: "turn_info",
      id: turnId,
      title: "This turn",
      summary: "Running…",
      status: "running",
      muted: true,
    })

    // 3. 预 push SkillUsedItem——让用户看到本轮启用了哪些 skills
    if (input.skillNames && input.skillNames.length > 0) {
      const signature = [...input.skillNames].sort().join(",")
      if (signature !== lastSkillSignature) {
        lastSkillSignature = signature
        streamItems.value.push({
          kind: "skill_used",
          id: genId("s"),
          skillNames: [...input.skillNames],
          summary: `Skills: ${input.skillNames.join(", ")}`,
        })
      }
    }

    try {
      // P1-B3-1: 切换到 async API——立即返回 202 + request_id。
      // 等待期间 WS event 进 pendingEventsByRequest 缓冲；202 后 flush。
      const resp = await messagesApi.sendPromptAsync({
        text: input.text,
        session_id: input.sessionId,
        file_ids: input.fileIds,
        skill_names: input.skillNames,
        coding_mode: input.codingMode,
        execution_mode: input.executionMode,
      })

      currentRequestId.value = resp.request_id
      pendingRequest.value = false
      const routedIntent = resp.intent
      if (currentTurnInfoId && routedIntent) {
        updateItem(currentTurnInfoId, (it: any) => {
          if (it.kind === "turn_info") {
            it.intent = routedIntent
            it.summary = `Running · ${routedIntent.route}`
          }
        })
      }
      // D2-7: 记录 metadata——assistant delta 路由用
      requestMetadataById.set(resp.request_id, {
        operation: "prompt",
        sessionId: input.sessionId ?? activeSessionId.value,
      })

      // flush 该 request 的 pending envelopes（按 sequence 排序，已通过 event_id 去重）
      const pending = pendingEventsByRequest.get(resp.request_id) ?? []
      pendingEventsByRequest.delete(resp.request_id)
      if (pending.length > 0) {
        pending.sort((a, b) => a.sequence - b.sequence)
        for (const env of pending) {
          // 已通过 event_id 去重 / session 隔离——直接走下游 mapper
          applyEventToStreamItems({ ...env.payload, type: env.type })
        }
      }

      // 不清 sending/streaming——等 WS 推 request_end / status poll 决定
      // 不在 finally 内清——async 立即返回后 request 还在后台运行
      return resp
    } catch (e: any) {
      let msg: string
      if (e instanceof ApiError) {
        msg = e.status === 409 ? "Agent is already running" : e.detail
      } else {
        msg = String(e?.message ?? e)
      }
      error.value = msg
      // async 失败 → 回滚乐观 push 的 turn_info + user_message 标记失败
      pendingRequest.value = false
      sending.value = false
      streaming.value = false
      streamItems.value.push({
        kind: "error",
        id: genId("e"),
        message: msg,
        details: e instanceof ApiError ? { status: e.status, payload: e.payload } : undefined,
      })
      finalizeTurnInfo("error")
      // **不**自动 fallback 同步 POST /api/prompt（用户原指令 §3：避免双发）
      throw e
    }
  }

  async function executeCheckpointer(sessionId: string) {
    if (sending.value) return null

    activeSessionId.value = sessionId
    sending.value = true
    streaming.value = false
    checkpointing.value = true
    checkpointNotice.value = null
    error.value = null
    pendingRequest.value = true
    currentRequestId.value = null
    aborting.value = false

    try {
      const response = await slashCommandsApi.executeSlashCommand(
        sessionId,
        "/checkpointer",
      )
      currentRequestId.value = response.request_id
      pendingRequest.value = false
      requestMetadataById.set(response.request_id, {
        operation: "checkpointer",
        sessionId,
      })
      const terminal = await pollRequestUntilTerminal(response.request_id)
      if (!terminal || terminal.status !== "completed") {
        throw new Error(terminal?.error || "Checkpoint was not completed")
      }
      return terminal
    } catch (e: any) {
      const nestedMessage = e instanceof ApiError
        ? e.payload?.detail?.message
        : undefined
      error.value = typeof nestedMessage === "string"
        ? nestedMessage
        : e instanceof ApiError
          ? e.detail
          : String(e?.message ?? e)
      pendingRequest.value = false
      sending.value = false
      streaming.value = false
      checkpointing.value = false
      currentRequestId.value = null
      throw e
    }
  }

  // ----------------------------------------------------------------------
  // D2-7: regenerateAssistantMessage
  // ----------------------------------------------------------------------

  /**
   * D2-7: 触发 regenerate——保持原 active assistant 可见，独立 draft bubble 接收流式。
   *
   * 流程（审核 §6）：
   *   1. 同步 submitting guard（防双击）
   *   2. POST regenerate → 拿到 request_id + regeneration_id
   *   3. 设 currentRequestId + regeneration state + 创建独立空 draft
   *   4. WS delta 走 appendAssistantDelta → 由 requestMetadataById 路由到 draft
   *   5. pollRequestUntilTerminal 监听 status → completed → reconcileMessagesFromServer
   *
   * **不**用 targetMessageId 作 draft id——会覆盖原 active assistant。
   */
  async function regenerateAssistantMessage(input: {
    sessionId: string
    assistantMessageId: string
  }) {
    // 同步 guard——防快速双击在第一次 202 前发出第二个请求
    // D2-8.1: 用模块级同步 flag 兜底——ref 的响应式更新在两次 microtask 间
    // 可能尚未生效，模块级 boolean 立即生效
    if (_regenerateInFlight || sending.value || regeneration.value.status === "queued" ||
        regeneration.value.status === "running") {
      return
    }
    _regenerateInFlight = true

    activeSessionId.value = input.sessionId
    sending.value = true
    streaming.value = true
    error.value = null
    currentTurnEvents.value = []
    gapDetected.value = false
    pendingRequest.value = true
    currentRequestId.value = null
    terminalEventSeen.value = false
    needsFinalResync.value = false
    aborting.value = false

    // 重置 regeneration state
    regeneration.value = {
      regenerationId: null,
      requestId: null,
      targetMessageId: input.assistantMessageId,
      draftItemId: null,
      status: "queued",
      errorMessage: null,
    }

    try {
      const resp = await regenerateApi.regenerateMessage(
        input.sessionId,
        input.assistantMessageId,
      )

      currentRequestId.value = resp.request_id
      pendingRequest.value = false

      // 创建独立 draft item——不覆盖原 active assistant
      const draftId = `regen-draft:${resp.request_id}`
      streamItems.value.push({
        kind: "assistant_message",
        id: draftId,
        content: "",
        streaming: true,
        isRegenerationDraft: true,
      })
      // 让 appendAssistantDelta 写入这个 draft
      currentAssistantItemId = draftId

      // 记录 metadata——delta 路由用
      requestMetadataById.set(resp.request_id, {
        operation: "regenerate",
        targetMessageId: input.assistantMessageId,
        sessionId: input.sessionId,
      })

      // 更新 regeneration state
      regeneration.value = {
        regenerationId: resp.regeneration_id,
        requestId: resp.request_id,
        targetMessageId: input.assistantMessageId,
        draftItemId: draftId,
        status: "running",
        errorMessage: null,
      }

      // flush 该 request 的 pending envelopes（202 前到达的 delta）
      const pending = pendingEventsByRequest.get(resp.request_id) ?? []
      pendingEventsByRequest.delete(resp.request_id)
      if (pending.length > 0) {
        pending.sort((a, b) => a.sequence - b.sequence)
        for (const env of pending) {
          applyEventToStreamItems({ ...env.payload, type: env.type })
        }
      }

      return resp
    } catch (e: any) {
      let msg: string
      if (e instanceof ApiError) {
        // D2-5 错误响应是 {detail: {code, message}} 格式
        const detail = e.payload?.detail
        if (detail && typeof detail === "object" && detail.message) {
          msg = detail.message
        } else if (e.status === 409) {
          msg = "Agent is already running"
        } else {
          msg = e.detail
        }
      } else {
        msg = String(e?.message ?? e)
      }
      error.value = msg
      pendingRequest.value = false
      sending.value = false
      streaming.value = false
      // 回滚 regeneration state
      regeneration.value = {
        regenerationId: null,
        requestId: null,
        targetMessageId: null,
        draftItemId: null,
        status: "error",
        errorMessage: msg,
      }
      streamItems.value.push({
        kind: "error",
        id: genId("e"),
        message: msg,
        details: e instanceof ApiError ? { status: e.status, payload: e.payload } : undefined,
      })
      throw e
    } finally {
      // D2-8.1: 清同步 guard——允许下次 regenerate
      _regenerateInFlight = false
    }
  }

  /** 缓冲 envelope 到 pendingEventsByRequest——单 queue 容量上限 100。 */
  function bufferPendingEvent(requestId: string, envelope: WebEventEnvelope) {
    let queue = pendingEventsByRequest.get(requestId)
    if (!queue) {
      queue = []
      pendingEventsByRequest.set(requestId, queue)
    }
    queue.push(envelope)
    // 容量上限——超限淘汰最旧
    if (queue.length > 100) {
      queue.splice(0, queue.length - 100)
    }
  }

  /**
   * P1-B3-2: WS reconnect 后从 lastGlobalSequence 拉取缺失事件 + 合并 live buffer。
   *
   * 流程（用户原指令 §7.4）：
   * 1. socket 已进入 replaying（hello 后由 handleEvent 触发本函数）
   * 2. WS 收到的新 event 在 handleEvent 入口进 liveEventsDuringReplay
   * 3. 调 GET /api/events?after_sequence=N&limit=200（**不带 session_id**——全局补播）
   * 4. 分页直到 has_more=false；保护上限 20 页 / 4000 事件，超限 needsFinalResync
   * 5. 合并 replay + live → sort by sequence → dedupe by event_id
   * 6. 依次 applyEventToStreamItems（跳过 session/request 隔离外的 envelope-decompose）
   * 7. replaying=false，状态回 connected
   */
  async function replayFromCursor() {
    if (replaying.value) return
    replaying.value = true

    try {
      const MAX_PAGES = 20
      const MAX_EVENTS = 4000
      const PAGE_LIMIT = 200
      let afterSeq = lastGlobalSequence.value
      let totalEvents = 0
      const collected: WebEventEnvelope[] = []

      for (let page = 0; page < MAX_PAGES; page++) {
        const resp = await eventsApi.getEvents({
          afterSequence: afterSeq,
          limit: PAGE_LIMIT,
          // **不**传 sessionId——sequence 是全局的，必须拉全部 envelope
        })
        if (resp.gap) {
          needsFinalResync.value = true
        }
        if (resp.events.length === 0) break
        collected.push(...resp.events)
        totalEvents += resp.events.length
        if (totalEvents >= MAX_EVENTS) {
          needsFinalResync.value = true
          break
        }
        afterSeq = resp.events[resp.events.length - 1].sequence
        if (!resp.has_more) break
      }

      // 合并 replay + live buffer
      const merged = [...collected, ...liveEventsDuringReplay]
      liveEventsDuringReplay = []

      // sort by sequence
      merged.sort((a, b) => a.sequence - b.sequence)

      // dedupe by event_id——replay 和 live 可能有重复（同 event 走两条路到达）
      const unique = new Map<string, WebEventEnvelope>()
      for (const env of merged) {
        if (!unique.has(env.event_id)) unique.set(env.event_id, env)
      }

      // 走下游 mapper——session / request 隔离照常；
      // 不调 handleEvent（避免再次 rememberEventId + advance cursor + buffer）
      replaying.value = false
      for (const env of unique.values()) {
        if (
          env.session_id &&
          activeSessionId.value &&
          env.session_id !== activeSessionId.value
        ) {
          continue
        }
        const requestId = env.request_id
        const isTurnControl =
          requestId !== null && TURN_CONTROL_TYPES.has(env.type)
        if (isTurnControl && requestId !== currentRequestId.value) {
          continue
        }
        applyEventToStreamItems({ ...env.payload, type: env.type })
      }
    } catch {
      // replay 失败——降级为 needsFinalResync；用户可手动刷新
      needsFinalResync.value = true
      replaying.value = false
    }
  }

  function finalizeTurnInfo(status: "done" | "error") {
    if (!currentTurnInfoId) return
    updateItem(currentTurnInfoId, (it: any) => {
      if (it.kind === "turn_info") {
        it.status = status
        it.summary =
          status === "done"
            ? `Finished (${currentTurnEvents.value.length} events)`
            : `Errored (${currentTurnEvents.value.length} events)`
        if (it.intent?.route) it.summary += ` · ${it.intent.route}`
      }
    })
  }

  // ----------------------------------------------------------------------
  // 通用 item 更新工具
  // ----------------------------------------------------------------------

  function updateItem(id: string, mutator: (item: any) => void): boolean {
    const idx = streamItems.value.findIndex((it) => it.id === id)
    if (idx < 0) return false
    // 触发 reactivity——直接 mutate 数组元素 + 替换数组引用
    const target: any = streamItems.value[idx]
    mutator(target)
    streamItems.value = [...streamItems.value]
    return true
  }

  function upsertToolApproval(raw: unknown): void {
    if (!raw || typeof raw !== "object") return
    const record = raw as ToolApprovalRecord
    if (
      typeof record.approval_id !== "string" ||
      typeof record.request_id !== "string" ||
      typeof record.tool_call_id !== "string" ||
      typeof record.tool_name !== "string" ||
      !["pending", "approved", "denied", "cancelled"].includes(record.status)
    ) {
      return
    }
    if (
      activeSessionId.value &&
      record.session_id &&
      record.session_id !== activeSessionId.value
    ) {
      return
    }

    let itemId: string | undefined = approvalItemIds[record.approval_id]
    if (!itemId) {
      const existing = streamItems.value.find(
        (item) =>
          item.kind === "tool_approval" &&
          item.approvalId === record.approval_id,
      )
      itemId = existing?.id
    }
    if (itemId) {
      approvalItemIds[record.approval_id] = itemId
      updateItem(itemId, (item: ToolApprovalItem) => {
        item.status = record.status
        item.resolvedAt = record.resolved_at
        item.submitting = false
        item.error = null
      })
      return
    }

    const item: ToolApprovalItem = {
      kind: "tool_approval",
      id: genId("approval"),
      approvalId: record.approval_id,
      requestId: record.request_id,
      sessionId: record.session_id,
      toolCallId: record.tool_call_id,
      toolName: record.tool_name,
      toolLabel: record.tool_label || record.tool_name,
      arguments:
        record.arguments && typeof record.arguments === "object"
          ? record.arguments
          : {},
      reason: record.reason,
      policyName: record.policy_name,
      status: record.status,
      createdAt: record.created_at,
      resolvedAt: record.resolved_at,
      submitting: false,
      error: null,
    }
    approvalItemIds[record.approval_id] = item.id
    streamItems.value.push(item)
  }

  async function loadPendingApprovals(
    sessionId: string,
    requestId: string,
  ): Promise<void> {
    try {
      const response = await approvalsApi.listRequestApprovals(requestId, "pending")
      if (
        activeSessionId.value !== sessionId ||
        currentRequestId.value !== requestId ||
        response.request_id !== requestId ||
        response.session_id !== sessionId
      ) {
        return
      }
      for (const approval of response.approvals) {
        if (
          approval.request_id === requestId &&
          approval.session_id === sessionId &&
          approval.status === "pending"
        ) {
          upsertToolApproval(approval)
        }
      }
    } catch {
      if (
        activeSessionId.value === sessionId &&
        currentRequestId.value === requestId
      ) {
        needsFinalResync.value = true
      }
    }
  }

  async function resolveToolApproval(
    approvalId: string,
    decision: ToolApprovalDecision,
  ): Promise<void> {
    const itemId = approvalItemIds[approvalId]
    const item = streamItems.value.find(
      (candidate) => candidate.id === itemId && candidate.kind === "tool_approval",
    ) as ToolApprovalItem | undefined
    if (!item || item.status !== "pending" || item.submitting) return
    if (
      currentRequestId.value !== item.requestId ||
      activeSessionId.value !== item.sessionId
    ) {
      return
    }

    updateItem(item.id, (target: ToolApprovalItem) => {
      target.submitting = true
      target.error = null
    })
    try {
      const response = await approvalsApi.resolveToolApproval(
        item.requestId,
        approvalId,
        decision,
      )
      if (
        currentRequestId.value === item.requestId &&
        activeSessionId.value === item.sessionId
      ) {
        upsertToolApproval(response.approval)
      }
    } catch (e) {
      const message = e instanceof ApiError ? e.detail : String(e)
      updateItem(item.id, (target: ToolApprovalItem) => {
        target.submitting = false
        target.error = message
      })
      throw e
    }
  }

  async function approvePlan(runId: string): Promise<void> {
    const itemId = `plan:${runId}`
    updateItem(itemId, (item: PlanRunItem) => {
      item.submitting = true
      item.error = null
    })
    try {
      const response = await plansApi.approvePlanRun(runId)
      upsertPlanRun(response.plan)
      updateItem(itemId, (item: PlanRunItem) => {
        item.submitting = false
      })
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.detail : String(cause)
      updateItem(itemId, (item: PlanRunItem) => {
        item.submitting = false
        item.error = message
      })
      throw cause
    }
  }

  // ----------------------------------------------------------------------
  // handleEvent —— WS event → ChatStreamItem 完整映射
  // ----------------------------------------------------------------------

  function handleEvent(rawEvent: WebEvent) {
    if (!rawEvent || typeof rawEvent.type !== "string") return

    // 1. 协议事件——hello / shutdown 是裸 dict（控制 frame，不走 envelope 路径）
    if (rawEvent.type === "hello") {
      wsConnected.value = true
      wsReconnecting.value = false
      // P1-B3-0c: hello 控制 frame 提供 first/last_available_sequence 用于建立 baseline。
      // **关键不变量**：hello 不消耗 next_event_sequence / 不进 buffer / 不进 seenEventIds；
      // 这里只更新 lastGlobalSequence baseline——避免首个真实事件 sequence=500
      // 被误判缺失 1-499。
      // 仅当本地尚无 cursor 时建立 baseline；reconnect 时保留旧 cursor 让 replay 走起。
      const helloLast = (rawEvent as any).last_available_sequence
      const wasReconnect = lastGlobalSequence.value > 0
      if (
        lastGlobalSequence.value === 0 &&
        typeof helloLast === "number" &&
        helloLast > 0
      ) {
        lastGlobalSequence.value = helloLast
      }
      // P1-B3-2: reconnect 时触发 replay——从 lastGlobalSequence 拉取缺失事件
      if (wasReconnect) {
        void replayFromCursor()
      }
      return
    }
    if (rawEvent.type === "shutdown") {
      wsConnected.value = false
      return
    }

    // P1-B2: envelope-aware 处理——提取 payload 作为下游 event；envelope 元数据用于
    // 去重 + session/request 隔离 + sequence gap 检测。hello / shutdown / legacy
    // 裸事件走 isWebEventEnvelope=false 分支，保留原行为。
    let event: any = rawEvent
    const envelope: WebEventEnvelope | null = isWebEventEnvelope(rawEvent)
      ? (rawEvent as WebEventEnvelope)
      : null
    if (envelope) {
      // 去重：event_id 已见过 → 跳过（B2 验收 #8）
      // P1-B3 hardening: rememberEventId 实现 FIFO 淘汰——只淘汰最旧 ID，
      // 不清空全部历史，避免边界后旧 event 被 replay 时重复处理。
      if (!rememberEventId(envelope.event_id)) return

      // sequence gap 检测——用全局 cursor（后端 sequence 是全局单调）。
      // per-session cursor 不能用于 gap 判断：跨 session 事件会让 per-session
      // 看起来"缺号"但实际没丢（B2.1 hardening）。
      if (lastGlobalSequence.value > 0 && envelope.sequence > lastGlobalSequence.value + 1) {
        gapDetected.value = true
      }
      lastGlobalSequence.value = Math.max(lastGlobalSequence.value, envelope.sequence)

      // per-session sequence cursor（统计 + B3 replay 用，不参与 gap 判断）
      if (envelope.session_id) {
        const last = lastSequenceBySession.value[envelope.session_id] ?? 0
        lastSequenceBySession.value = {
          ...lastSequenceBySession.value,
          [envelope.session_id]: Math.max(last, envelope.sequence),
        }
      }

      // session 隔离：不属于当前 active session 的事件不写入当前消息流（B2 验收 #10）。
      if (
        envelope.session_id &&
        activeSessionId.value &&
        envelope.session_id !== activeSessionId.value
      ) {
        return
      }

      if (
        recoveringActiveRequestId !== null &&
        envelope.request_id === recoveringActiveRequestId
      ) {
        liveEventsDuringActiveRecovery.push(envelope)
        return
      }

      // P1-B3-2: replay 期间——live envelope 暂存（已通过 event_id 去重 + advance cursor）
      // flush 时与 replay events 合并 → sort by sequence → applyEventToStreamItems
      if (replaying.value) {
        liveEventsDuringReplay.push(envelope)
        return
      }

      // P1-B3-1: request 隔离 / pending buffer
      const requestId = envelope.request_id
      const isTurnControl =
        requestId !== null && TURN_CONTROL_TYPES.has(envelope.type)

      if (isTurnControl) {
        // 是 turn-control 事件——必须属于 currentRequestId
        if (pendingRequest.value && currentRequestId.value === null && requestId !== null) {
          // 等 202 期间——缓冲；202 来了 flush
          bufferPendingEvent(requestId, envelope)
          return
        }
        if (requestId !== currentRequestId.value) {
          // 旧 request 或未知 request——不污染当前 turn
          // 已通过 event_id 去重 + 已推进 cursor，但不调下游 mapper
          return
        }
        // requestId === currentRequestId——继续下游
      }
      // 非 turn-control（无 request_id 或管理 event）——继续下游

      // 合并 envelope.type + envelope.payload 作为下游 event
      event = { ...envelope.payload, type: envelope.type }
    }

    applyEventToStreamItems(event)
  }

  /**
   * 下游 event → ChatStreamItem 映射器——handleEvent / sendPrompt pending flush 共用。
   *
   * **不变量**：调用此函数前 event 已通过：
   * - event_id 去重（envelope 路径）
   * - session 隔离
   * - request 隔离（turn-control 事件属于 currentRequestId）
   *
   * hello / shutdown 不应进入此函数（handleEvent 入口提前 return）。
   */
  function applyEventToStreamItems(event: any) {
    currentTurnEvents.value.push(event)

    const t = event.type

    if (typeof t === "string" && t.startsWith("plan_") && event.plan) {
      upsertPlanRun(event.plan as PlanRun)
      return
    }

    if (t === "context_budget_updated") {
      useContextBudgetStore().applyEvent(activeSessionId.value, event)
      return
    }

    // 2. error 事件
    if (t === "error") {
      appendError((event as any).message || "stream error", event)
      finalizeTurnInfo("error")
      return
    }
    if ((event as any).last_error) {
      appendError(String((event as any).last_error), event)
      return
    }

    // 3. request lifecycle
    if (t === "request_queued" || t === "request_start") {
      ensureTurnInfo(t === "request_queued" ? "queued" : "running")
      return
    }
    if (t === "request_end") {
      const status = (event as any).status
      // P1-B3-3: terminal event——触发 status poll（等 SQLite 持久化完成）
      terminalEventSeen.value = true
      if (status === "aborted" || status === "error") {
        finalizeTurnInfo("error")
        if (status === "aborted") {
          appendError("Request aborted", event)
        }
      } else {
        finalizeTurnInfo("done")
      }
      // P1-B3-3: poll 直到 terminal 后 reconcile messages
      // （request_end 可能早于 SQLite 持久化，立即 loadMessages 会缺最终消息）
      const reqId = currentRequestId.value
      if (reqId) {
        void pollRequestUntilTerminal(reqId)
      } else {
        // 无 currentRequestId（异常路径）——直接清 state
        sending.value = false
        streaming.value = false
        aborting.value = false
      }
      return
    }
    if (t === "agent_abort") {
      appendError(`Request aborted: ${(event as any).reason ?? "no reason"}`, event)
      finalizeTurnInfo("error")
      return
    }

    // 4. agent / turn lifecycle
    if (t === "agent_start") {
      // run 级开始——确保有 turn_info（如果 sendPrompt 已建过则不动）
      ensureTurnInfo("running")
      return
    }
    if (t === "agent_end") {
      // run 级结束——finalize，但不清空（保留 turn cards）
      finalizeTurnInfo("done")
      streaming.value = false
      currentAssistantItemId = null
      return
    }
    if (t === "turn_start") {
      ensureTurnInfo("running")
      return
    }
    if (t === "turn_end") {
      finalizeTurnInfo("done")
      // assistant 最终文本来自 turn_end.message
      const msg = (event as any).message
      if (msg && currentAssistantItemId !== null) {
        const finalText = assistantTextOf(msg)
        const finalThinking = assistantThinkingOf(msg)
        updateItem(currentAssistantItemId, (it: any) => {
          if (it.kind === "assistant_message") {
            if (finalText) it.content = finalText
            if (finalThinking.content) it.thinking = finalThinking.content
            it.thinkingRedacted = finalThinking.redacted || undefined
            it.thinkingStreaming = false
            it.streaming = false
            it.usage = msg.usage ?? undefined
            it.generationMetrics = msg.generation_metrics
          }
        })
      }
      currentAssistantItemId = null
      streaming.value = false
      return
    }

    // 5. message lifecycle
    if (t === "message_start") {
      const msg = (event as any).message
      if (msg?.role === "assistant") {
        const thinking = assistantThinkingOf(msg)
        // 创建 streaming draft
        if (currentAssistantItemId === null) {
          const id = genId("a")
          streamItems.value.push({
            kind: "assistant_message",
            id,
            content: assistantTextOf(msg),
            thinking: thinking.content || undefined,
            thinkingRedacted: thinking.redacted || undefined,
            streaming: true,
          })
          currentAssistantItemId = id
        } else {
          // 已有 draft——append
          updateItem(currentAssistantItemId, (it: any) => {
            if (it.kind === "assistant_message") {
              const seg = assistantTextOf(msg)
              if (seg && !it.content.endsWith(seg)) {
                it.content = it.content + seg
              }
              if (thinking.content) it.thinking = thinking.content
              it.thinkingRedacted = thinking.redacted || undefined
            }
          })
        }
      }
      // user / toolResult message_start 跳过——user 已乐观 push，
      // toolResult 走 tool_execution 路径
      return
    }
    if (t === "message_update") {
      const msg = (event as any).message
      const streamEvent = (event as any).assistant_message_event
      if (streamEvent?.type === "text_delta" && typeof streamEvent.delta === "string") {
        // 优先用 delta
        appendAssistantDelta(streamEvent.delta)
      } else if (typeof streamEvent?.type === "string" && streamEvent.type.startsWith("thinking_")) {
        updateAssistantThinking(msg, streamEvent)
      } else if (msg?.role === "assistant") {
        // 兜底：用整条 message.content diff
        const full = assistantTextOf(msg)
        appendAssistantFull(full)
      }
      return
    }
    if (t === "message_end") {
      const msg = (event as any).message
      if (msg?.role === "assistant") {
        const full = assistantTextOf(msg)
        const thinking = assistantThinkingOf(msg)
        if (currentAssistantItemId !== null) {
          updateItem(currentAssistantItemId, (it: any) => {
            if (it.kind === "assistant_message") {
              if (full && (!it.content || it.content.length < full.length)) {
                it.content = full
              }
              if (thinking.content) it.thinking = thinking.content
              it.thinkingRedacted = thinking.redacted || undefined
              it.thinkingStreaming = false
              it.streaming = false
              it.usage = msg.usage
              it.generationMetrics = msg.generation_metrics
            }
          })
        } else {
          // 没收到 message_start——补一个
          const id = genId("a")
          streamItems.value.push({
            kind: "assistant_message",
            id,
            content: full,
            thinking: thinking.content || undefined,
            thinkingRedacted: thinking.redacted || undefined,
            thinkingStreaming: false,
            usage: msg.usage ?? undefined,
            generationMetrics: msg.generation_metrics,
          })
          currentAssistantItemId = id
        }
      }
      return
    }

    if (t === "tool_approval_requested" || t === "tool_approval_resolved") {
      upsertToolApproval((event as any).approval)
      return
    }

    // 6. tool execution
    if (t === "tool_execution_start") {
      upsertToolCallStart(event)
      return
    }
    if (t === "tool_execution_update") {
      upsertToolCallUpdate(event)
      return
    }
    if (t === "tool_execution_end") {
      upsertToolCallEnd(event)
      return
    }

    // 7. 未知事件——不白屏；累积到 currentTurnEvents 即可
    // （已 push）——可选：更新 turn_info details
  }

  // ----------------------------------------------------------------------
  // upsert helpers
  // ----------------------------------------------------------------------

  function updateAssistantThinking(message: any, streamEvent: any) {
    const full = assistantThinkingOf(message)
    const delta =
      streamEvent?.type === "thinking_delta" && typeof streamEvent.delta === "string"
        ? streamEvent.delta
        : ""
    const isEnd = streamEvent?.type === "thinking_end"

    let targetId: string | null = currentAssistantItemId
    if (targetId === null) {
      for (let i = streamItems.value.length - 1; i >= 0; i--) {
        if (streamItems.value[i].kind === "assistant_message") {
          targetId = streamItems.value[i].id
          break
        }
      }
      if (targetId !== null) currentAssistantItemId = targetId
    }

    if (targetId === null) {
      const id = genId("a")
      streamItems.value.push({
        kind: "assistant_message",
        id,
        content: assistantTextOf(message),
        thinking: full.content || delta || undefined,
        thinkingStreaming: !isEnd,
        thinkingRedacted: full.redacted || streamEvent?.redacted === true || undefined,
        streaming: true,
      })
      currentAssistantItemId = id
      return
    }

    updateItem(targetId, (it: any) => {
      if (it.kind !== "assistant_message") return
      if (full.content) {
        it.thinking = full.content
      } else if (delta) {
        it.thinking = `${it.thinking || ""}${delta}`
      }
      it.thinkingStreaming = !isEnd
      it.thinkingRedacted =
        full.redacted || streamEvent?.redacted === true || it.thinkingRedacted || undefined
      it.streaming = true
    })
  }

  function ensureTurnInfo(status: "queued" | "running"): string {
    if (currentTurnInfoId) {
      updateItem(currentTurnInfoId, (it: any) => {
        if (it.kind === "turn_info") {
          it.status = status
        }
      })
      return currentTurnInfoId
    }
    const id = genId("t")
    streamItems.value.push({
      kind: "turn_info",
      id,
      title: "This turn",
      summary: status === "queued" ? "Queued…" : "Running…",
      status,
      muted: true,
    })
    currentTurnInfoId = id
    return id
  }

  function appendAssistantDelta(delta: string) {
    if (!delta) return

    // 关键修复：同 appendAssistantFull——不依赖 currentAssistantItemId 跨事件持久化
    let targetId: string | null = currentAssistantItemId
    if (targetId === null) {
      for (let i = streamItems.value.length - 1; i >= 0; i--) {
        if (streamItems.value[i].kind === "assistant_message") {
          targetId = streamItems.value[i].id
          break
        }
      }
      if (targetId !== null) currentAssistantItemId = targetId
    }

    if (targetId === null) {
      const id = genId("a")
      streamItems.value.push({
        kind: "assistant_message",
        id,
        content: delta,
        streaming: true,
      })
      currentAssistantItemId = id
      return
    }
    updateItem(targetId, (it: any) => {
      if (it.kind === "assistant_message") {
        // 防重复——如果 delta 已是 content 后缀则跳过
        if (!it.content.endsWith(delta)) {
          it.content = it.content + delta
        }
      }
    })
  }

  function appendAssistantFull(full: string) {
    if (!full) return

    // 关键修复：不依赖 currentAssistantItemId 跨事件持久化（实测会被某种方式
    // 重置成 null，疑似多 socket 实例 / Pinia 多订阅）。
    // 直接从 streamItems 反查最后一个 assistant_message——保证不会创建
    // 重复 draft。
    let targetId: string | null = currentAssistantItemId
    if (targetId === null) {
      // 反查 streamItems 最后一个 assistant_message
      for (let i = streamItems.value.length - 1; i >= 0; i--) {
        if (streamItems.value[i].kind === "assistant_message") {
          targetId = streamItems.value[i].id
          break
        }
      }
      // 同步把 currentAssistantItemId 修正回来，后续事件能复用
      if (targetId !== null) currentAssistantItemId = targetId
    }

    if (targetId === null) {
      // streamItems 里也没有 assistant_message——首次创建
      const id = genId("a")
      streamItems.value.push({
        kind: "assistant_message",
        id,
        content: full,
        streaming: true,
      })
      currentAssistantItemId = id
      return
    }

    updateItem(targetId, (it: any) => {
      if (it.kind === "assistant_message") {
        if (full.length > it.content.length) it.content = full
      }
    })
  }

  function upsertToolCallStart(event: any) {
    const tc = extractToolCall(event)
    const toolCallId = tc.id
    const toolName = tc.name || "unknown_tool"

    // 已存在（同 tool_call.id）—— 只更新 status
    if (toolCallId && toolItemIds[toolCallId]) {
      updateItem(toolItemIds[toolCallId], (it: any) => {
        if (it.status !== undefined) it.status = "running"
        it.startedAt = Date.now()
      })
      return
    }

    const id = genId("tc")
    if (toolCallId) toolItemIds[toolCallId] = id

    const argsPreview = previewOf(tc.args)

    if (isFileTool(toolName)) {
      // 从 args 中尝试拿 fileName / fileId / format
      const args: any =
        tc.args && typeof tc.args === "object" ? (tc.args as any) : {}
      const fileItem: FileReadItem = {
        kind: "file_read",
        id,
        toolName,
        toolCallId,
        fileName: args.file_id || args.filename || args.name || args.path,
        fileId: args.file_id,
        format: args.format,
        status: "running",
      }
      streamItems.value.push(fileItem)
    } else if (isMcpTool(toolName)) {
      const parsed = parseMcpName(toolName)
      const mcpItem: MCPToolCallItem = {
        kind: "mcp_tool_call",
        id,
        serverName: parsed.serverName,
        toolName: parsed.toolName,
        toolCallId,
        status: "running",
        argsPreview,
      }
      streamItems.value.push(mcpItem)
    } else {
      const item: ToolCallItem = {
        kind: "tool_call",
        id,
        toolCallId,
        toolName,
        status: "running",
        argsPreview,
        startedAt: Date.now(),
      }
      streamItems.value.push(item)
    }
  }

  function upsertToolCallEnd(event: any) {
    const tc = extractToolCall(event)
    const result: any = (event as any).result
    const toolCallId = tc.id
    const toolName = tc.name || "unknown_tool"
    const isError = !!result?.is_error

    const resultPreview = result
      ? previewOf(
          Array.isArray(result.content)
            ? result.content
                .map((c: any) => (c?.type === "text" ? c.text : ""))
                .filter(Boolean)
                .join("\n")
            : result,
        )
      : undefined

    const existingId = toolCallId ? toolItemIds[toolCallId] : undefined

    if (existingId) {
      // 更新已有 item
      updateItem(existingId, (it: any) => {
        it.status = isError ? "error" : "done"
        if (resultPreview) it.resultPreview = resultPreview
        if (it.startedAt) {
          it.durationMs = Date.now() - it.startedAt
        }
        it.details = result
      })
      return
    }

    // 没找到 start——补一个 done item
    const id = genId("tc")
    if (toolCallId) toolItemIds[toolCallId] = id

    if (isFileTool(toolName)) {
      const fileItem: FileReadItem = {
        kind: "file_read",
        id,
        toolName,
        toolCallId,
        status: isError ? "error" : "done",
        preview: resultPreview,
        details: result,
      }
      streamItems.value.push(fileItem)
    } else if (isMcpTool(toolName)) {
      const parsed = parseMcpName(toolName)
      const mcpItem: MCPToolCallItem = {
        kind: "mcp_tool_call",
        id,
        serverName: parsed.serverName,
        toolName: parsed.toolName,
        toolCallId,
        status: isError ? "error" : "done",
        resultPreview,
      }
      streamItems.value.push(mcpItem)
    } else {
      const item: ToolResultItem = {
        kind: "tool_result",
        id,
        toolName,
        toolCallId,
        status: isError ? "error" : "done",
        resultPreview,
        details: result,
      }
      streamItems.value.push(item)
    }
  }

  function upsertToolCallUpdate(event: any) {
    const tc = extractToolCall(event)
    const toolCallId = tc.id
    const partial: any = (event as any).partial_result
    const existingId = toolCallId ? toolItemIds[toolCallId] : undefined
    if (!existingId) {
      upsertToolCallStart(event)
    }
    const targetId = toolCallId ? toolItemIds[toolCallId] : undefined
    if (!targetId) return
    const preview = partial
      ? previewOf(
          Array.isArray(partial.content)
            ? partial.content
                .map((c: any) => (c?.type === "text" ? c.text : ""))
                .filter(Boolean)
                .join("\n")
            : partial,
        )
      : undefined
    updateItem(targetId, (it: any) => {
      it.status = "running"
      if (preview) {
        if (it.kind === "file_read") it.preview = preview
        else it.resultPreview = preview
      }
      it.details = partial
    })
  }

  function appendError(message: string, details?: unknown) {
    streamItems.value.push({
      kind: "error",
      id: genId("e"),
      message,
      details,
    })
  }

  // ----------------------------------------------------------------------
  // WS lifecycle
  // ----------------------------------------------------------------------

  function connectEvents() {
    if (socket !== null) return
    socket = createEventSocket({
      onEvent: handleEvent,
      onOpen: () => {
        wsConnected.value = true
        wsReconnecting.value = false
      },
      onClose: () => {
        wsConnected.value = false
      },
      onError: () => {
        // 自动重连会启动
      },
      onReconnecting: () => {
        wsReconnecting.value = true
      },
    })
  }

  function disconnectEvents() {
    if (socket !== null) {
      socket.close()
      socket = null
    }
    wsConnected.value = false
    wsReconnecting.value = false
  }

  /**
   * P1-B3-4: 仅 E2E 测试用——模拟"非主动网络断线"触发自动 reconnect + replay。
   * 与 disconnectEvents 区别：disconnectEvents 主动 close（不重连）；
   * 本函数走 closeForTest 让 onclose 自动 scheduleReconnect。
   */
  function closeEventSocketForTest() {
    if (socket !== null) {
      socket.closeForTest()
    }
  }

  /** 切换 active session 时调用——清空当前 turn + items。 */
  function resetForSession() {
    streamItems.value = []
    currentTurnEvents.value = []
    error.value = null
    sending.value = false
    streaming.value = false
    currentTurnInfoId = null
    currentAssistantItemId = null
    Object.keys(toolItemIds).forEach((k) => delete toolItemIds[k])
    Object.keys(approvalItemIds).forEach((k) => delete approvalItemIds[k])
    lastSkillSignature = null
    // P1-B2: 重置去重 state（session 切换时不清理 lastSequenceBySession——
    // 保留 per-session sequence 用于后续切回时 gap 检测；但 seenEventIds
    // 可累积——event_id 是全局唯一的）
    gapDetected.value = false
    currentRequestId.value = null
    // P1-B3-1: 清 async prompt state
    pendingRequest.value = false
    terminalEventSeen.value = false
    needsFinalResync.value = false
    aborting.value = false
    checkpointing.value = false
    checkpointNotice.value = null
    pendingEventsByRequest.clear()
    activeRecoveryToken += 1
    recoveringActiveRequestId = null
    liveEventsDuringActiveRecovery = []
  }

  function resetWorkspace() {
    disconnectEvents()
    resetForSession()
    activeSessionId.value = null
    lastGlobalSequence.value = 0
    lastSequenceBySession.value = {}
    seenEventIds.clear()
    seenEventQueue.splice(0)
    requestMetadataById.clear()
    terminalPolls.clear()
    replaying.value = false
    liveEventsDuringReplay = []
    regeneration.value = {
      regenerationId: null,
      requestId: null,
      targetMessageId: null,
      draftItemId: null,
      status: "idle",
      errorMessage: null,
    }
    _regenerateInFlight = false
  }

  /**
   * D2-7: 计算当前 stream 中**最新 persisted assistant** 的 messageId——
   * 用于 MessageBubble 判断是否显示 Regenerate 按钮。
   *
   * **审核 §5 关键**：从 streamItems 末尾向前扫，跳过：
   * - 非 assistant_message
   * - 未 persisted 的 streaming draft
   * - isRegenerationDraft
   * - 任何 ErrorCard / ToolCard / turn_info
   *
   * 返回 messageId 或 null。
   */
  const latestPersistedAssistantMessageId = computed(() => {
    for (let i = streamItems.value.length - 1; i >= 0; i--) {
      const it: any = streamItems.value[i]
      if (
        it.kind === "assistant_message" &&
        it.persisted === true &&
        !it.isRegenerationDraft &&
        typeof it.messageId === "string"
      ) {
        return it.messageId as string
      }
    }
    return null
  })

  /**
   * 同步当前 active session id——供 sessionStore 切换/新建/删除 session 时调用。
   * 必须在所有切换路径调用，否则 handleEvent 的 session 隔离会用过期的 session_id。
   *
   * P1-B2.1 hardening：之前只在 sendPrompt 内推断 activeSessionId，导致用户切换
   * session 但未发消息时，WS 事件仍按旧 session 过滤。
   *
   * 不调 resetForSession——由调用方决定是否清空消息流（首次加载 / 切换 / 删除等场景不同）。
   */
  function setActiveSession(sid: string | null) {
    activeSessionId.value = sid
  }

  /**
   * D2-7: 切换 session 时清理 regeneration UI state——但**不**清服务器 request。
   * 审核 §11：Session A 的 regeneration delta 不应出现在 Session B。
   *
   * 与 resetForSession 区别：resetForSession 清全部 turn state；本函数只清
   * regeneration draft + state。
   */
  function clearRegenerationForSessionSwitch() {
    // 删除 regeneration draft item（若有）
    if (regeneration.value.draftItemId) {
      streamItems.value = streamItems.value.filter(
        (it: any) => it.id !== regeneration.value.draftItemId,
      )
    }
    regeneration.value = {
      regenerationId: null,
      requestId: null,
      targetMessageId: null,
      draftItemId: null,
      status: "idle",
      errorMessage: null,
    }
  }

  return {
    streamItems,
    sending,
    streaming,
    wsConnected,
    wsReconnecting,
    error,
    checkpointing,
    checkpointNotice,
    itemCount,
    pendingApprovalCount,
    // P1-B2: envelope-aware state（暴露给调试 / 后续 UI）
    lastSequenceBySession,
    gapDetected,
    currentRequestId,
    activeSessionId,
    // P1-B3-1: async prompt 竞态 + request 隔离 state
    pendingRequest,
    terminalEventSeen,
    needsFinalResync,
    aborting,
    // P1-B3-2: replay state
    replaying,
    // P1-B3-4: 暴露给 E2E 测试重置——生产 UI 不读
    lastGlobalSequence,
    // D2-7: regeneration 单对象 state + computed
    regeneration,
    latestPersistedAssistantMessageId,
    loadMessages,
    sendPrompt,
    executeCheckpointer,
    // D2-7: regenerate action
    regenerateAssistantMessage,
    clearRegenerationForSessionSwitch,
    connectEvents,
    disconnectEvents,
    closeEventSocketForTest,
    handleEvent,
    resetForSession,
    setActiveSession,
    // P1-B3-3: 新 actions
    abortRun,
    reconcileMessagesFromServer,
    pollRequestUntilTerminal,
    findActiveRequest,
    resumeActiveRequest,
    recoverActiveRequestEvents,
    loadPendingApprovals,
    resolveToolApproval,
    approvePlan,
    resetWorkspace,
  }
})
