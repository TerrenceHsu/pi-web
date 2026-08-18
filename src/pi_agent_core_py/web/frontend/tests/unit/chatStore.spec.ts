// chatStore 单元测试——WT-1 G2（turn_info 卡片清理）。
//
// 仅覆盖 sendPrompt 入口处对 streamItems 的 cleanup 不变量：
//   1. 上一轮 done 的 turn_info 在新一轮 sendPrompt 时清除
//   2. 上一轮 error 的 turn_info 在新一轮 sendPrompt 时清除
//   3. 当前正在 running 的 turn_info 不被错误清除（防御性）
//   4. assistant_message 不被清除
//   5. tool cards（tool_call / tool_result / file_read）不被清除
//
// 不覆盖 sendPrompt 后续 WS / API 错误路径——这些由其它 E2E / integration 覆盖。

import { describe, expect, it, vi, beforeEach } from "vitest"
import { createPinia, setActivePinia } from "pinia"

// ----- Mock api/messages -----
// vitest.config.ts 有 restoreMocks: true——vi.mock 内的 mockResolvedValue
// 在每个测试前会被清掉，所以这里只放裸 vi.fn()，beforeEach 再补实现。
vi.mock("../../src/api/messages", () => ({
  sendPromptAsync: vi.fn(),
  sendPrompt: vi.fn(),
  getRequestStatus: vi.fn(),
  abortRequest: vi.fn(),
  listActiveRequests: vi.fn(),
  getMessages: vi.fn(),
}))

vi.mock("../../src/api/client", () => ({
  ApiError: class ApiError extends Error {},
  requestJson: vi.fn(),
}))

vi.mock("../../src/api/slashCommands", () => ({
  executeSlashCommand: vi.fn(),
  listSlashCommands: vi.fn(),
}))

vi.mock("../../src/api/events", () => ({
  getEvents: vi.fn(),
}))

vi.mock("../../src/api/approvals", () => ({
  listRequestApprovals: vi.fn(),
  resolveToolApproval: vi.fn(),
}))

import * as approvalsApi from "../../src/api/approvals"
import * as eventsApi from "../../src/api/events"
import * as messagesApi from "../../src/api/messages"
import * as slashCommandsApi from "../../src/api/slashCommands"

import { useChatStore } from "../../src/stores/chatStore"
import type { ChatStreamItem } from "../../src/types"

function makeTurnInfo(id: string, status: "queued" | "running" | "done" | "error"): ChatStreamItem {
  return {
    kind: "turn_info",
    id,
    title: "This turn",
    summary: "...",
    status,
    muted: true,
  } as ChatStreamItem
}

function makeAssistant(id: string): ChatStreamItem {
  return {
    kind: "assistant_message",
    id,
    content: "hello",
  } as ChatStreamItem
}

function makeToolCall(id: string): ChatStreamItem {
  return {
    kind: "tool_call",
    id,
    toolName: "view_file",
    status: "done",
  } as ChatStreamItem
}

function makeFileRead(id: string): ChatStreamItem {
  return {
    kind: "file_read",
    id,
    toolName: "view_file",
    status: "done",
  } as ChatStreamItem
}

const FAKE_RESP = {
  ok: true,
  request_id: "req-fake",
  session_id: "sess-1",
  status: "queued" as const,
  events_url: "/api/events",
  request_url: "/api/requests/req-fake",
  abort_url: "/api/requests/req-fake/abort",
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.mocked(messagesApi.sendPromptAsync).mockResolvedValue(FAKE_RESP)
  vi.mocked(approvalsApi.listRequestApprovals).mockResolvedValue({
    request_id: "req-fake",
    session_id: "sess-1",
    count: 0,
    approvals: [],
  })
  vi.mocked(slashCommandsApi.executeSlashCommand).mockResolvedValue({
    ok: true,
    command: "/checkpointer",
    request_id: "req-checkpoint",
    session_id: "sess-1",
    status: "queued",
    request_url: "/api/requests/req-checkpoint",
    abort_url: "/api/requests/req-checkpoint/abort",
  })
  vi.mocked(eventsApi.getEvents).mockResolvedValue({
    count: 0,
    events: [],
    first_available_sequence: null,
    last_available_sequence: null,
    has_more: false,
    gap: false,
  })
})

const PENDING_APPROVAL = {
  approval_id: "approval-1",
  request_id: "req-approval",
  session_id: "sess-1",
  tool_call_id: "call-write",
  tool_name: "write_file",
  tool_label: "Write file",
  arguments: { filename: "report.md" },
  reason: "high-risk tool category 'write' not explicitly allowed",
  policy_name: "default",
  policy_metadata: { category: "write" },
  status: "pending" as const,
  created_at: "2026-08-16T00:00:00Z",
  resolved_at: null,
}

async function flushAll() {
  await new Promise((r) => setTimeout(r, 0))
}

describe("G2: sendPrompt clears stale turn_info cards", () => {
  it("1. removes previous-round done turn_info", async () => {
    const store = useChatStore()
    store.streamItems = [makeTurnInfo("t-old-done", "done"), makeAssistant("a-1")]
    await store.sendPrompt({ sessionId: "sess-1", text: "next" })
    await flushAll()
    expect(store.streamItems.some((it) => it.id === "t-old-done")).toBe(false)
  })

  it("2. removes previous-round error turn_info", async () => {
    const store = useChatStore()
    store.streamItems = [makeTurnInfo("t-old-err", "error"), makeAssistant("a-1")]
    await store.sendPrompt({ sessionId: "sess-1", text: "next" })
    await flushAll()
    expect(store.streamItems.some((it) => it.id === "t-old-err")).toBe(false)
  })

  it("3. keeps running turn_info (defensive)", async () => {
    const store = useChatStore()
    store.streamItems = [makeTurnInfo("t-running", "running")]
    await store.sendPrompt({ sessionId: "sess-1", text: "next" })
    await flushAll()
    // 旧 running 仍保留 + 新轮 turn_info 也加入
    expect(store.streamItems.some((it) => it.id === "t-running")).toBe(true)
  })

  it("4. keeps assistant_message", async () => {
    const store = useChatStore()
    store.streamItems = [
      makeAssistant("a-keep"),
      makeTurnInfo("t-old-done", "done"),
    ]
    await store.sendPrompt({ sessionId: "sess-1", text: "next" })
    await flushAll()
    expect(store.streamItems.some((it) => it.id === "a-keep")).toBe(true)
  })

  it("5. keeps tool_call / file_read cards", async () => {
    const store = useChatStore()
    store.streamItems = [
      makeToolCall("tc-keep"),
      makeFileRead("fr-keep"),
      makeTurnInfo("t-old-err", "error"),
    ]
    await store.sendPrompt({ sessionId: "sess-1", text: "next" })
    await flushAll()
    expect(store.streamItems.some((it) => it.id === "tc-keep")).toBe(true)
    expect(store.streamItems.some((it) => it.id === "fr-keep")).toBe(true)
  })

  it("always creates a new running turn_info for this round", async () => {
    const store = useChatStore()
    store.streamItems = [makeTurnInfo("t-old-done", "done")]
    await store.sendPrompt({ sessionId: "sess-1", text: "next" })
    await flushAll()
    const turnCards = store.streamItems.filter((it) => it.kind === "turn_info")
    expect(turnCards.length).toBe(1)
    expect((turnCards[0] as any).status).toBe("running")
  })
})

describe("terminal message reconciliation", () => {
  it("keeps a previous tool result anchored inside its original turn", async () => {
    const persisted = (
      messageId: string,
      idx: number,
      role: "user" | "assistant",
      text: string,
    ) => ({
      message_id: messageId,
      session_id: "sess-1",
      idx,
      role,
      content: [{ type: "text", text }],
      created_at: idx,
      message: { role, content: [{ type: "text", text }] },
    })
    vi.mocked(messagesApi.getMessages).mockResolvedValue({
      count: 4,
      session_id: "sess-1",
      messages: [
        persisted("u-old", 0, "user", "first question"),
        persisted("a-old", 2, "assistant", "first answer"),
        persisted("u-new", 3, "user", "second question"),
        persisted("a-new", 4, "assistant", "second answer"),
      ],
    })

    const store = useChatStore()
    store.setActiveSession("sess-1")
    store.streamItems = [
      {
        kind: "user_message",
        id: "u-old",
        messageId: "u-old",
        persisted: true,
        content: "first question",
      } as any,
      {
        kind: "turn_info",
        id: "tr-old",
        title: "toolResult",
        summary: "search result",
        muted: true,
      } as any,
      {
        kind: "assistant_message",
        id: "a-old",
        messageId: "a-old",
        persisted: true,
        content: "first answer",
      } as any,
      {
        kind: "user_message",
        id: "optimistic-user",
        content: "second question",
      } as any,
      {
        kind: "turn_info",
        id: "turn-new",
        title: "This turn",
        summary: "Done",
        status: "done",
        muted: true,
      } as any,
      {
        kind: "assistant_message",
        id: "draft-new",
        content: "second answer",
        streaming: false,
      } as any,
    ]

    await store.reconcileMessagesFromServer("sess-1")

    expect(store.streamItems.map((item) => item.id)).toEqual([
      "u-old",
      "tr-old",
      "a-old",
      "u-new",
      "turn-new",
      "a-new",
    ])
    expect(store.streamItems.findIndex((item) => item.id === "tr-old")).toBeLessThan(
      store.streamItems.findIndex((item) => item.id === "a-old"),
    )
  })
})

describe("checkpointer state", () => {
  it("clears every stream item only after the server reports completion", async () => {
    vi.useFakeTimers()
    try {
      vi.mocked(messagesApi.getRequestStatus).mockResolvedValue({
        request_id: "req-checkpoint",
        session_id: "sess-1",
        status: "completed",
        created_at: null,
        started_at: null,
        ended_at: null,
        error: null,
        error_type: null,
        abort_reason: null,
        result_summary: {
          command: "/checkpointer",
          memory_file_id: "file-memory",
        },
        event_start_sequence: null,
        event_end_sequence: null,
        operation: "checkpointer",
      })
      const store = useChatStore()
      store.streamItems = [makeAssistant("a-before"), makeTurnInfo("t-before", "done")]
      const pending = store.executeCheckpointer("sess-1")
      await vi.advanceTimersByTimeAsync(300)
      await pending
      expect(store.streamItems).toEqual([])
      expect(store.checkpointNotice).toBe("Checkpoint saved to Memory.md")
      expect(store.checkpointing).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it("keeps current items when command startup fails", async () => {
    vi.mocked(slashCommandsApi.executeSlashCommand).mockRejectedValue(
      new Error("provider unavailable"),
    )
    const store = useChatStore()
    store.streamItems = [makeAssistant("a-keep")]
    await expect(store.executeCheckpointer("sess-1")).rejects.toThrow(
      "provider unavailable",
    )
    expect(store.streamItems.map((item) => item.id)).toEqual(["a-keep"])
    expect(store.error).toBe("provider unavailable")
    expect(store.checkpointing).toBe(false)
  })
})

describe("full reload active request recovery", () => {
  it("replays only the owned session/request event stream", async () => {
    vi.mocked(messagesApi.listActiveRequests).mockResolvedValue({
      count: 1,
      requests: [{
        request_id: "req-running",
        session_id: "sess-1",
        status: "running",
        operation: "prompt",
      }],
    } as any)
    vi.mocked(eventsApi.getEvents).mockResolvedValue({
      count: 2,
      events: [
        {
          event_id: "evt-owned",
          request_id: "req-running",
          session_id: "sess-1",
          sequence: 10,
          type: "message_update",
          timestamp: "2026-08-16T00:00:00Z",
          payload: {
            assistant_message_event: {
              type: "text_delta",
              delta: "recovered text",
            },
          },
        },
        {
          event_id: "evt-foreign",
          request_id: "req-running",
          session_id: "sess-foreign",
          sequence: 11,
          type: "message_update",
          timestamp: "2026-08-16T00:00:01Z",
          payload: {
            assistant_message_event: {
              type: "text_delta",
              delta: "must not render",
            },
          },
        },
      ],
      first_available_sequence: 10,
      last_available_sequence: 11,
      has_more: false,
      gap: false,
    } as any)

    const store = useChatStore()
    store.setActiveSession("sess-1")
    const requestId = await store.findActiveRequest("sess-1")
    expect(requestId).toBe("req-running")
    store.resumeActiveRequest(requestId!)
    await store.recoverActiveRequestEvents("sess-1", requestId!)

    expect(eventsApi.getEvents).toHaveBeenCalledWith({
      afterSequence: 0,
      limit: 200,
      sessionId: "sess-1",
      requestId: "req-running",
    })
    const rendered = JSON.stringify(store.streamItems)
    expect(rendered).toContain("recovered text")
    expect(rendered).not.toContain("must not render")
  })

  it("does not let a stale Session recovery steal the current live buffer", async () => {
    let resolveFirst!: (value: any) => void
    let resolveSecond!: (value: any) => void
    vi.mocked(eventsApi.getEvents).mockImplementation(({ sessionId }) => (
      new Promise((resolve) => {
        if (sessionId === "sess-1") resolveFirst = resolve
        else resolveSecond = resolve
      })
    ))

    const store = useChatStore()
    store.setActiveSession("sess-1")
    store.resumeActiveRequest("req-1")
    const firstRecovery = store.recoverActiveRequestEvents("sess-1", "req-1")

    store.resetForSession()
    store.setActiveSession("sess-2")
    store.resumeActiveRequest("req-2")
    const secondRecovery = store.recoverActiveRequestEvents("sess-2", "req-2")

    resolveFirst({
      count: 0,
      events: [],
      first_available_sequence: null,
      last_available_sequence: null,
      has_more: false,
      gap: false,
    })
    await firstRecovery

    store.handleEvent({
      event_id: "evt-current-live",
      request_id: "req-2",
      session_id: "sess-2",
      sequence: 20,
      type: "message_update",
      timestamp: "2026-08-16T00:00:02Z",
      payload: {
        assistant_message_event: {
          type: "text_delta",
          delta: "current live text",
        },
      },
    } as any)
    expect(JSON.stringify(store.streamItems)).not.toContain("current live text")

    resolveSecond({
      count: 0,
      events: [],
      first_available_sequence: null,
      last_available_sequence: null,
      has_more: false,
      gap: false,
    })
    await secondRecovery
    expect(JSON.stringify(store.streamItems)).toContain("current live text")
  })
})

describe("P2-B tool approvals", () => {
  it("renders a request-scoped approval event and resolves it once", async () => {
    vi.mocked(approvalsApi.resolveToolApproval).mockResolvedValue({
      ok: true,
      request_id: "req-approval",
      session_id: "sess-1",
      idempotent: false,
      approval: {
        ...PENDING_APPROVAL,
        status: "approved",
        resolved_at: "2026-08-16T00:00:01Z",
      },
    })
    const store = useChatStore()
    store.setActiveSession("sess-1")
    store.resumeActiveRequest("req-approval")
    store.handleEvent({
      event_id: "evt-approval",
      request_id: "req-approval",
      session_id: "sess-1",
      sequence: 30,
      type: "tool_approval_requested",
      timestamp: "2026-08-16T00:00:00Z",
      payload: { type: "tool_approval_requested", approval: PENDING_APPROVAL },
    } as any)

    expect(store.pendingApprovalCount).toBe(1)
    expect(store.streamItems).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: "tool_approval",
          approvalId: "approval-1",
          status: "pending",
        }),
      ]),
    )

    await store.resolveToolApproval("approval-1", "approve")
    expect(approvalsApi.resolveToolApproval).toHaveBeenCalledWith(
      "req-approval",
      "approval-1",
      "approve",
    )
    expect(store.pendingApprovalCount).toBe(0)
    expect(store.streamItems).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ approvalId: "approval-1", status: "approved" }),
      ]),
    )
  })

  it("restores a pending approval through the request API after refresh", async () => {
    vi.mocked(approvalsApi.listRequestApprovals).mockResolvedValue({
      request_id: "req-approval",
      session_id: "sess-1",
      count: 1,
      approvals: [PENDING_APPROVAL],
    })
    const store = useChatStore()
    store.setActiveSession("sess-1")
    store.resumeActiveRequest("req-approval")

    await store.loadPendingApprovals("sess-1", "req-approval")

    expect(approvalsApi.listRequestApprovals).toHaveBeenCalledWith(
      "req-approval",
      "pending",
    )
    expect(store.pendingApprovalCount).toBe(1)
  })

  it("ignores an approval from another Session", () => {
    const store = useChatStore()
    store.setActiveSession("sess-1")
    store.resumeActiveRequest("req-approval")
    store.handleEvent({
      event_id: "evt-foreign-approval",
      request_id: "req-approval",
      session_id: "sess-foreign",
      sequence: 31,
      type: "tool_approval_requested",
      timestamp: "2026-08-16T00:00:00Z",
      payload: {
        type: "tool_approval_requested",
        approval: { ...PENDING_APPROVAL, session_id: "sess-foreign" },
      },
    } as any)

    expect(store.pendingApprovalCount).toBe(0)
  })
})
