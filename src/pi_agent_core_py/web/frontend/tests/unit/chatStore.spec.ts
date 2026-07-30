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

import * as messagesApi from "../../src/api/messages"

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
})

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
