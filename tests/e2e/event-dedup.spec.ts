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

    // 第一次注入
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, ev)
    const count1 = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().streamItems.length
    })

    // 再次注入完全相同的 envelope（同 event_id）
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, ev)
    const count2 = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().streamItems.length
    })

    // 同 event_id 不重复处理——streamItems 数量不变
    expect(count2).toBe(count1)
  })

  test("其他 session 事件不污染当前消息流（B2 验收 #10 session 部分）", async ({
    page,
  }) => {
    await setupStore(page, "sess_A")

    // 注入 session_B 的事件——应被过滤
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_other_session",
      sequence: 200,
      type: "message_start",
      sessionId: "sess_B",
      payload: {
        type: "message_start",
        message: { role: "assistant", content: [{ type: "text", text: "from B" }] },
      },
    }))
    const items = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().streamItems.length
    })
    expect(items).toBe(0)

    // 同样事件但 session_id=A 应该被处理
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_active_session",
      sequence: 201,
      type: "message_start",
      sessionId: "sess_A",
      payload: {
        type: "message_start",
        message: { role: "assistant", content: [] },
      },
    }))
    const itemsAfter = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().streamItems.length
    })
    expect(itemsAfter).toBeGreaterThan(0)
  })

  test("sequence gap 触发 gapDetected（B2 验收 #7 前端部分）", async ({
    page,
  }) => {
    await setupStore(page, "sess_gap")

    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_gap_1",
      sequence: 300,
      type: "message_start",
      sessionId: "sess_gap",
    }))
    const gap1 = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().gapDetected
    })
    expect(gap1).toBe(false)

    // 注入 sequence=305（gap=4）——应该触发 gapDetected
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_gap_2",
      sequence: 305,
      type: "message_end",
      sessionId: "sess_gap",
    }))
    const gap2 = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().gapDetected
    })
    expect(gap2).toBe(true)
  })

  test("跨 session 事件不触发 per-session gap 误报（B2.1 关键问题修复）", async ({
    page,
  }) => {
    await setupStore(page, "sess_A")

    // sess_A 收 sequence=400
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_cross_1",
      sequence: 400,
      type: "message_start",
      sessionId: "sess_A",
    }))

    // sess_B 收 sequence=401（前端整体 cursor 前进；但 sess_A 内部不应该报 gap）
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_cross_2",
      sequence: 401,
      type: "message_start",
      sessionId: "sess_B",
    }))

    // sess_A 再收 sequence=402——按 per-session gap 判断会误报"缺 401"
    // 但前端现在用全局 cursor，401 已经记录过，不应该 trigger gapDetected
    await page.evaluate((e) => {
      ;(window as any).__storeHooks.chatStore().handleEvent(e)
    }, envelope({
      eventId: "evt_cross_3",
      sequence: 402,
      type: "message_end",
      sessionId: "sess_A",
    }))

    const gap = await page.evaluate(() => {
      return (window as any).__storeHooks.chatStore().gapDetected
    })
    expect(gap).toBe(false)
  })
})

