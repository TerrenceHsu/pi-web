import { test, expect } from "@playwright/test"

/**
 * P1-B2.1 hardening — 前端 chatStore 去重 / session 隔离 / gap 检测直接测试。
 *
 * 通过 window.__storeHooks.chatStore() 注入 envelope，绕过真实 WS——
 * 因为 sync TestClient + WS 的混合测试难，且 FakeClient 不会自然产生重复事件 /
 * 跨 session 事件 / sequence gap。
 *
 * 依赖：main.ts expose `window.__storeHooks.chatStore`（生产 build 也保留，
 * 无 secret 泄露——store 不持密钥）。
 */

function envelope(opts: {
  eventId: string
  sequence: number
  type: string
  sessionId?: string | null
  requestId?: string | null
  payload?: Record<string, any>
}) {
  return {
    event_id: opts.eventId,
    request_id: opts.requestId ?? null,
    session_id: opts.sessionId ?? null,
    sequence: opts.sequence,
    type: opts.type,
    timestamp: new Date().toISOString(),
    payload: opts.payload ?? { type: opts.type },
  }
}

async function setupStore(
  page: import("@playwright/test").Page,
  sessionId: string | null,
) {
  await page.goto("/")
  await page.waitForFunction(() => !!(window as any).__storeHooks?.chatStore)
  await page.evaluate((sid) => {
    const s = (window as any).__storeHooks.chatStore()
    if (sid !== null) s.setActiveSession(sid)
    s.resetForSession()
    // P1-B3-4: 重置 lastGlobalSequence=0——避免被 hello baseline 推进影响 gap 测试
    // hello 在 connectEvents 后会 set baseline = hello.last_available_sequence
    // 如果 server-side buffer 已有事件（前序 test 触发的），baseline 会很高
    s.lastGlobalSequence = 0
  }, sessionId)
}

test.describe("chatStore envelope-aware behavior (P1-B2.1)", () => {
  test("重复 event_id 只处理一次（B2 验收 #8）", async ({ page }) => {
    await setupStore(page, null)

    const ev = envelope({
      eventId: "evt_dup_1",
      sequence: 100,
      type: "message_start",
      sessionId: null,
      payload: {
        type: "message_start",
        message: { role: "assistant", content: [] },
      },
    })

    // 在同一个浏览器任务内连续注入，避免真实 WS 后台事件夹在
    // 两次读数之间，把无关 stream item 误判为去重失败。
    const { before, count1, count2 } = await page.evaluate((e) => {
      const store = (window as any).__storeHooks.chatStore()
      const before = store.streamItems.length
      store.handleEvent(e)
      const count1 = store.streamItems.length
      store.handleEvent(e)
      const count2 = store.streamItems.length
      return { before, count1, count2 }
    }, ev)

    // 同 event_id 不重复处理——streamItems 数量不变
    expect(count1).toBeGreaterThan(before)
    expect(count2).toBe(count1)
  })

  test("其他 session 事件不污染当前消息流（B2 验收 #10 session 部分）", async ({
    page,
  }) => {
    await setupStore(page, "sess_A")

    const otherSessionEvent = envelope({
      eventId: "evt_other_session",
      sequence: 200,
      type: "message_start",
      sessionId: "sess_B",
      payload: {
        type: "message_start",
        message: { role: "assistant", content: [{ type: "text", text: "from B" }] },
      },
    })
    const activeSessionEvent = envelope({
      eventId: "evt_active_session",
      sequence: 201,
      type: "message_start",
      sessionId: "sess_A",
      payload: {
        type: "message_start",
        message: { role: "assistant", content: [] },
      },
    })
    const { items, itemsAfter } = await page.evaluate(([other, active]) => {
      const store = (window as any).__storeHooks.chatStore()
      store.streamItems = []
      store.handleEvent(other)
      const items = store.streamItems.length
      store.handleEvent(active)
      return { items, itemsAfter: store.streamItems.length }
    }, [otherSessionEvent, activeSessionEvent])

    expect(items).toBe(0)
    expect(itemsAfter).toBeGreaterThan(0)
  })

  test("sequence gap 触发 gapDetected（B2 验收 #7 前端部分）", async ({
    page,
  }) => {
    await setupStore(page, "sess_gap")

    const first = envelope({
      eventId: "evt_gap_1",
      sequence: 300,
      type: "message_start",
      sessionId: "sess_gap",
    })
    const second = envelope({
      eventId: "evt_gap_2",
      sequence: 305,
      type: "message_end",
      sessionId: "sess_gap",
    })
    const { gap1, gap2 } = await page.evaluate(([first, second]) => {
      const store = (window as any).__storeHooks.chatStore()
      store.gapDetected = false
      store.lastGlobalSequence = 0
      store.handleEvent(first)
      const gap1 = store.gapDetected
      store.handleEvent(second)
      return { gap1, gap2: store.gapDetected }
    }, [first, second])

    expect(gap1).toBe(false)
    expect(gap2).toBe(true)
  })

  test("跨 session 事件不触发 per-session gap 误报（B2.1 关键问题修复）", async ({
    page,
  }) => {
    await setupStore(page, "sess_A")

    const first = envelope({
      eventId: "evt_cross_1",
      sequence: 400,
      type: "message_start",
      sessionId: "sess_A",
    })
    const second = envelope({
      eventId: "evt_cross_2",
      sequence: 401,
      type: "message_start",
      sessionId: "sess_B",
    })
    const third = envelope({
      eventId: "evt_cross_3",
      sequence: 402,
      type: "message_end",
      sessionId: "sess_A",
    })

    // 同一浏览器任务内验证全局 cursor，避免真实 WS 事件插入
    // 三次人工注入之间，产生与本断言无关的 gap。
    const gap = await page.evaluate(([first, second, third]) => {
      const store = (window as any).__storeHooks.chatStore()
      store.gapDetected = false
      store.lastGlobalSequence = 0
      store.handleEvent(first)
      store.handleEvent(second)
      store.handleEvent(third)
      return store.gapDetected
    }, [first, second, third])

    expect(gap).toBe(false)
  })
})
