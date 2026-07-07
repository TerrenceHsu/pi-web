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
import { ApiError } from "../api/client"
import { createEventSocket, type EventSocket } from "../api/websocket"
import type {
  AgentMessage,
  ChatStreamItem,
  FileRef,
  MCPToolCallItem,
  ToolCallItem,
  ToolResultItem,
  FileReadItem,
  TurnInfoItem,
  WebEvent,
} from "../types"

// ============================================================================
// 工具函数
// ============================================================================

function genId(prefix: string): string {
  ChatStoreSeed.counter += 1
  return `${prefix}-${Date.now().toString(36)}-${ChatStoreSeed.counter}`
}

const ChatStoreSeed = { counter: 0 }

function textOf(msg: AgentMessage): string {
  if (!Array.isArray(msg?.content)) return ""
  return msg.content
    .filter((c: any) => c && c.type === "text" && typeof c.text === "string")
    .map((c: any) => c.text)
    .join("\n")
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

/** 判断 toolName 是否是文件工具。 */
function isFileTool(name: string | undefined): boolean {
  return name === "view_file" || name === "list_files"
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

  let socket: EventSocket | null = null

  /** 当前 turn 收到的 raw events——用于 details 展开。 */
  const currentTurnEvents = ref<WebEvent[]>([])

  /** 当前 turn 的 TurnInfoItem id——配对更新用。 */
  let currentTurnInfoId: string | null = null
  /** 当前 streaming assistant draft item id——避免重复创建。 */
  let currentAssistantItemId: string | null = null
  /**
   * 本轮 WS 是否已经收到并处理过 assistant 内容（message_start/update/end）。
   *
   * 用于 POST /api/prompt 兜底逻辑判断——避免在 WS 已经渲染完 assistant 后，
   * turn_end 清掉 currentAssistantItemId 导致 POST 返回时重复 push 一个
   * assistant_message（典型 WS + 同步 POST 重复 bug）。
   */
  let assistantSeenFromWs = false
  /** tool_call.id → streamItem.id 映射——用于 start/end 配对。 */
  const toolItemIds: Record<string, string> = {}
  /** 已展示过的 SkillUsedItem signature——避免重复。 */
  let lastSkillSignature: string | null = null

  const itemCount = computed(() => streamItems.value.length)

  // ----------------------------------------------------------------------
  // 历史消息加载
  // ----------------------------------------------------------------------

  function messageToItem(msg: AgentMessage, id: string): ChatStreamItem | null {
    if (msg.role === "user") {
      return {
        kind: "user_message",
        id,
        content: textOf(msg) || "(empty user message)",
      }
    }
    if (msg.role === "assistant") {
      return {
        kind: "assistant_message",
        id,
        content: textOf(msg),
      }
    }
    // toolResult / summary / 其它——历史消息中没有 tool_call 配对信息，
    // 简化为 turn_info（避免在历史中显示一堆孤立的 tool_result card）
    return {
      kind: "turn_info",
      id,
      title: msg.role || "info",
      summary: textOf(msg) || "(no text)",
      muted: true,
    }
  }

  async function loadMessages(sessionId?: string) {
    error.value = null
    try {
      const resp = await messagesApi.getMessages(sessionId)
      const items: ChatStreamItem[] = []
      resp.messages.forEach((m, i) => {
        const item = messageToItem(m, `hist-${i}`)
        if (item) items.push(item)
      })
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
  }) {
    if (sending.value) return
    if (!input.text.trim()) return

    sending.value = true
    streaming.value = true
    error.value = null
    currentTurnEvents.value = []

    // 1. 乐观 push user_message——含本轮附件 FileRef[]
    streamItems.value.push({
      kind: "user_message",
      id: genId("u"),
      content: input.text,
      files: input.files && input.files.length > 0 ? input.files.slice() : undefined,
    })

    // 2. 创建本轮 TurnInfoItem placeholder
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
      const resp = await messagesApi.sendPrompt({
        text: input.text,
        session_id: input.sessionId,
        file_ids: input.fileIds,
        skill_names: input.skillNames,
      })

      // ============================================================
      // 关键设计：POST /api/prompt 是同步阻塞，返回时所有 AgentEvent 已 emit
      // 到 WS。但浏览器 event loop 的 microtask（POST 响应 continuation）
      // 必然先于 macrotask（WS onmessage）执行——所以这里**绝不能 push
      // assistant_message 兜底**，否则会与稍后到达的 WS event 重复。
      //
      // WS 正常工作时实时渲染 streaming draft；WS 断连时用户能从
      // wsConnected=false / 状态条感知；不再做 POST 兜底。
      // ============================================================

      // finalize turn_info
      finalizeTurnInfo("done")

      // finalize turn_info
      finalizeTurnInfo("done")

      return resp
    } catch (e: any) {
      let msg: string
      if (e instanceof ApiError) {
        msg = e.status === 409 ? "Agent is already running" : e.detail
      } else {
        msg = String(e?.message ?? e)
      }
      error.value = msg
      streamItems.value.push({
        kind: "error",
        id: genId("e"),
        message: msg,
        details: e instanceof ApiError ? { status: e.status, payload: e.payload } : undefined,
      })
      finalizeTurnInfo("error")
      throw e
    } finally {
      // 关键：不清 currentAssistantItemId——POST 响应是 microtask，会先于
      // 剩余 WS macrotask 执行；若在这里清变量，后续 WS event 会看到 null。
      // WS 自己会在 turn_end / agent_end 时清，无需 finally 重复 cleanup。
      // （错误路径：catch 已 finalize turn_info；currentItemId 留给下一轮
      // message_start 自动判断 if-null-create / else-append 处理。）
      sending.value = false
      streaming.value = false
    }
  }

  /** 从 response.messages 取最后一条 assistant 文本——用于 WS 缺失时兜底。 */
  function pickFinalAssistantText(messages: any[] | undefined): string | null {
    if (!Array.isArray(messages) || messages.length === 0) return null
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m?.role === "assistant") {
        const t = assistantTextOf(m)
        return t || ""
      }
    }
    return null
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

  // ----------------------------------------------------------------------
  // handleEvent —— WS event → ChatStreamItem 完整映射
  // ----------------------------------------------------------------------

  function handleEvent(event: WebEvent) {
    if (!event || typeof event.type !== "string") return
    currentTurnEvents.value.push(event)

    const t = event.type

    // 1. 协议事件
    if (t === "hello") {
      wsConnected.value = true
      wsReconnecting.value = false
      return
    }
    if (t === "shutdown") {
      wsConnected.value = false
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
      if (status === "aborted" || status === "error") {
        finalizeTurnInfo("error")
        if (status === "aborted") {
          appendError("Request aborted", event)
        }
      } else {
        finalizeTurnInfo("done")
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
        if (finalText) {
          updateItem(currentAssistantItemId, (it: any) => {
            if (it.kind === "assistant_message") {
              it.content = finalText
              it.streaming = false
            }
          })
        }
      }
      currentAssistantItemId = null
      streaming.value = false
      return
    }

    // 5. message lifecycle
    if (t === "message_start") {
      const msg = (event as any).message
      if (msg?.role === "assistant") {
        // 标记本轮 WS 已收到 assistant 内容——避免 POST 兜底重复 push
        assistantSeenFromWs = true
        // 创建 streaming draft
        if (currentAssistantItemId === null) {
          const id = genId("a")
          streamItems.value.push({
            kind: "assistant_message",
            id,
            content: assistantTextOf(msg),
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
        // 标记本轮 WS 已收到 assistant 内容——避免 POST 兜底重复 push
        assistantSeenFromWs = true
        const full = assistantTextOf(msg)
        if (currentAssistantItemId !== null) {
          updateItem(currentAssistantItemId, (it: any) => {
            if (it.kind === "assistant_message") {
              if (full && (!it.content || it.content.length < full.length)) {
                it.content = full
              }
              it.streaming = false
            }
          })
        } else {
          // 没收到 message_start——补一个
          const id = genId("a")
          streamItems.value.push({
            kind: "assistant_message",
            id,
            content: full,
          })
          currentAssistantItemId = id
        }
      }
      return
    }

    // 6. tool execution
    if (t === "tool_execution_start") {
      upsertToolCallStart(event)
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
    assistantSeenFromWs = true

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
    assistantSeenFromWs = true

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
        fileName: args.file_id || args.name || args.path,
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

  /** 切换 active session 时调用——清空当前 turn + items。 */
  function resetForSession() {
    streamItems.value = []
    currentTurnEvents.value = []
    error.value = null
    sending.value = false
    streaming.value = false
    currentTurnInfoId = null
    currentAssistantItemId = null
    assistantSeenFromWs = false
    Object.keys(toolItemIds).forEach((k) => delete toolItemIds[k])
    lastSkillSignature = null
  }

  return {
    streamItems,
    sending,
    streaming,
    wsConnected,
    wsReconnecting,
    error,
    itemCount,
    loadMessages,
    sendPrompt,
    connectEvents,
    disconnectEvents,
    handleEvent,
    resetForSession,
  }
})
