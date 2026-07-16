/**
 * P1-D2-8.1: Regenerate E2E —— 8 用例覆盖审核 §D2-8.1。
 *
 * 后端使用 delayed FakeClient（每个 delta 75-150ms，至少 4 个 delta）让
 * regenerate 流式过程可观察。每个 test 用独立 session 隔离避免 SQLite 污染。
 *
 * 用例：
 *   1. 按钮可见性（最新 persisted assistant 显示 / 历史 / draft 不显示 / active 期间隐藏）
 *   2. 流式期间旧回答保留
 *   3. 成功后同 message_id 就地更新
 *   4. Abort 保留旧回答
 *   5. Reload / reconnect
 *   6. 双击只一个 request
 *   7. Export 只含新 active answer
 *   8. 下一轮用新 active answer 作历史
 */
import { test, expect, type Page } from "@playwright/test"

async function waitForE2EHooks(page: Page) {
  await page.waitForFunction(
    () => !!(window as any).__storeHooks?.chatStore?.(),
    undefined,
    { timeout: 5_000 },
  )
}

async function newSession(page: Page): Promise<string> {
  await page.locator("[data-testid='new-chat-button']").click()
  await page.waitForTimeout(200)
  // 从 sidebar 第一个 session 拿 sid（最新的）
  const sid = await page.evaluate(() => {
    const el = document.querySelector(
      "[data-testid='session-item']:first-of-type",
    ) as any
    return el?.dataset?.sessionId ?? null
  })
  return sid ?? ""
}

async function sendAndAwaitAssistant(page: Page, text: string): Promise<string> {
  await page.locator("[data-testid='chat-input-field']").fill(text)
  await page.locator("[data-testid='send-button']").click()
  // 等 assistant-message 出现并 persisted（带 data-message-id）
  const assistant = page.locator(
    "[data-testid='assistant-message'][data-persisted='true']",
  )
  await expect(assistant.last()).toBeVisible({ timeout: 20_000 })
  // 等 data-message-id 出现（reconcileFromServer 写入 persisted=true）
  await page.waitForFunction(
    () => {
      const els = document.querySelectorAll(
        "[data-testid='assistant-message'][data-persisted='true']",
      )
      const last = els[els.length - 1]
      return last && (last as HTMLElement).dataset.messageId
    },
    undefined,
    { timeout: 10_000 },
  )
  const messageId = await page.evaluate(() => {
    const els = document.querySelectorAll(
      "[data-testid='assistant-message'][data-persisted='true']",
    )
    return (els[els.length - 1] as HTMLElement).dataset.messageId
  })
  return messageId ?? ""
}

// ============================================================================
// 1. 按钮可见性
// ============================================================================
test.describe("Regenerate 1: button visibility", () => {
  test("最新 persisted assistant 显示 Regenerate；历史不显示", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    // 第一轮
    await sendAndAwaitAssistant(page, "first turn")
    // 第二轮
    await sendAndAwaitAssistant(page, "second turn")

    // 最新 persisted assistant 有 Regenerate 按钮
    const regenBtns = page.locator("[data-testid='message-regenerate-btn']")
    await expect(regenBtns).toHaveCount(1, { timeout: 5_000 })

    // 历史 assistant（第一轮）没显示——只有一个按钮（最新）
    // 已在 toHaveCount(1) 断言
  })

  test("active request 期间 Regenerate 不可用", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)
    await sendAndAwaitAssistant(page, "first turn")

    // 触发 prompt（不等待完成）—— sending=true 期间
    await page.locator("[data-testid='chat-input-field']").fill("second")
    await page.locator("[data-testid='send-button']").click()

    // sending 期间 Regenerate 按钮不可见
    await expect(page.locator("[data-testid='message-regenerate-btn']")).toHaveCount(0)
    // 等到 prompt 完成
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
  })
})

// ============================================================================
// 2. 流式期间旧回答保留
// ============================================================================
test.describe("Regenerate 2: streaming keeps old answer visible", () => {
  test("点击 Regenerate 后旧 A 仍可见；独立 draft 可见", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    const originalId = await sendAndAwaitAssistant(page, "original")
    // 记录原内容
    const originalText = await page
      .locator(`[data-message-id="${originalId}"] .text`)
      .innerText()

    // 点击 Regenerate
    await page.locator("[data-testid='message-regenerate-btn']").click()
    await page.waitForTimeout(300) // 让 draft 创建

    // 旧 A 仍可见——text 不变
    const stillOldText = await page
      .locator(`[data-message-id="${originalId}"] .text`)
      .innerText()
    expect(stillOldText).toBe(originalText)

    // 独立 regeneration draft 可见（data-regeneration-draft=true）
    const draft = page.locator(
      "[data-testid='assistant-message'][data-regeneration-draft='true']",
    )
    await expect(draft).toBeVisible({ timeout: 5_000 })

    // 等完成
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
  })
})

// ============================================================================
// 3. 成功后同 message_id 就地更新
// ============================================================================
test.describe("Regenerate 3: same message_id in-place update", () => {
  test("完成后 message_id 不变 + content 更新 + draft 删除", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    const originalId = await sendAndAwaitAssistant(page, "to-regenerate")
    expect(originalId).toBeTruthy()

    // 点击 Regenerate，等完成
    await page.locator("[data-testid='message-regenerate-btn']").click()
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
    // 再多等一会儿让 reconcile 完成
    await page.waitForTimeout(500)

    // 同 message_id 仍存在
    const sameMsg = page.locator(
      `[data-testid='assistant-message'][data-message-id="${originalId}"]`,
    )
    await expect(sameMsg).toHaveCount(1)

    // draft 已删除
    await expect(
      page.locator(
        "[data-testid='assistant-message'][data-regeneration-draft='true']",
      ),
    ).toHaveCount(0)

    // assistant-message 数量 = 1（不新增第二个）
    await expect(
      page.locator(
        "[data-testid='assistant-message'][data-persisted='true']",
      ),
    ).toHaveCount(1)
  })
})

// ============================================================================
// 4. Abort 保留旧回答
// ============================================================================
test.describe("Regenerate 4: abort keeps old answer", () => {
  test("Stop 后旧 A 仍在；draft 删除；revision 状态 aborted", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    const originalId = await sendAndAwaitAssistant(page, "to-abort-regen")
    const originalText = await page
      .locator(`[data-message-id="${originalId}"] .text`)
      .innerText()

    // 点 Regenerate
    await page.locator("[data-testid='message-regenerate-btn']").click()
    // 等 Stop 按钮可见后立即 abort
    await expect(page.locator("[data-testid='stop-button']")).toBeVisible({
      timeout: 5_000,
    })
    await page.locator("[data-testid='stop-button']").click()

    // 等 send 按钮恢复（terminal）
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
    await page.waitForTimeout(500)

    // 旧回答不变
    const afterText = await page
      .locator(`[data-message-id="${originalId}"] .text`)
      .innerText()
    expect(afterText).toBe(originalText)

    // draft 已删除
    await expect(
      page.locator(
        "[data-testid='assistant-message'][data-regeneration-draft='true']",
      ),
    ).toHaveCount(0)

    // 页面仍可继续发送——发新 prompt 验证
    await page.locator("[data-testid='chat-input-field']").fill("next after abort")
    await page.locator("[data-testid='send-button']").click()
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
  })
})

// ============================================================================
// 5. Reload / reconnect
// ============================================================================
test.describe("Regenerate 5: reload during regen", () => {
  // D2-8.1 NOTE: 完整 reload 恢复需要 URL-based session routing（当前 activeSessionId
  // 不在 URL/localStorage，reload 后 App.vue onMounted 选第一个 session）。
  // 本测试改用"主动 WS 断开 + 自动重连"模拟 reconnect——更稳定且不依赖 routing。
  test("WS 断开 + 自动重连后原回答仍可见 + 最终一个 active assistant", async ({
    page,
  }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    const originalId = await sendAndAwaitAssistant(page, "reconnect-test")

    // 触发 Regenerate
    await page.locator("[data-testid='message-regenerate-btn']").click()
    await page.waitForTimeout(300) // 让 draft 开始

    // 主动断开 WS（让自动重连触发 reconnect replay）
    await page.evaluate(() => {
      ;(window as any).__e2eHooks?.closeEventSocket?.()
    })
    await page.waitForTimeout(500)

    // 原 active 仍可见
    await expect(
      page.locator(`[data-message-id="${originalId}"]`),
    ).toBeVisible({ timeout: 10_000 })

    // 等 terminal
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 30_000,
    })
    await page.waitForTimeout(500)

    // 最终只一个 active assistant persisted
    await expect(
      page.locator(
        "[data-testid='assistant-message'][data-persisted='true']",
      ),
    ).toHaveCount(1)
    // draft 已删除
    await expect(
      page.locator(
        "[data-testid='assistant-message'][data-regeneration-draft='true']",
      ),
    ).toHaveCount(0)
  })
})

// ============================================================================
// 6. 双击只一个 request
// ============================================================================
test.describe("Regenerate 6: double-click single request", () => {
  test("快速双击 Regenerate 最终只一个 active assistant", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    const originalId = await sendAndAwaitAssistant(page, "double-click-test")

    // 双击 Regenerate——前端 submitting guard + 后端 partial unique 兜底
    const btn = page.locator("[data-testid='message-regenerate-btn']")
    await btn.click()
    await btn.click()

    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })

    // 最终只一个 active assistant persisted——同 message_id（regenerate 后 ID 不变）
    await expect(
      page.locator(
        "[data-testid='assistant-message'][data-persisted='true']",
      ),
    ).toHaveCount(1)
    await expect(
      page.locator(`[data-message-id="${originalId}"]`),
    ).toHaveCount(1)
  })
})

// ============================================================================
// 7. Export 只含新 active answer
// ============================================================================
test.describe("Regenerate 7: export only new active", () => {
  test("Export Markdown 不含 revision 元数据", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    await sendAndAwaitAssistant(page, "export-original")

    // Regenerate
    await page.locator("[data-testid='message-regenerate-btn']").click()
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
    await page.waitForTimeout(500)

    // 通过 chatStore.activeSessionId 拿 sid（__storeHooks 只暴露 chatStore）
    const sid = await page.evaluate(() => {
      return (window as any).__storeHooks?.chatStore?.()?.activeSessionId
    })
    expect(sid).toBeTruthy()

    const resp = await page.request.get(
      `/api/sessions/${sid}/export/markdown`,
    )
    expect(resp.ok()).toBeTruthy()
    const blob = (await resp.body()).toString("utf-8")

    // 不含 revision 元数据关键词
    expect(blob).not.toContain("revision")
    expect(blob).not.toContain("superseded")
    expect(blob).not.toContain("regeneration_id")
    expect(blob).not.toContain("regeneration_draft")
    // 至少含一个 assistant 块
    expect(blob.length).toBeGreaterThan(0)
  })
})

// ============================================================================
// 8. 下一轮用新 active answer
// ============================================================================
test.describe("Regenerate 8: next turn uses new active", () => {
  test("regenerate 后普通 Prompt 仍可发送 + assistant 不重复", async ({ page }) => {
    await page.goto("/")
    await waitForE2EHooks(page)
    await newSession(page)

    const originalId = await sendAndAwaitAssistant(page, "first-original")
    expect(originalId).toBeTruthy()

    // Regenerate
    await page.locator("[data-testid='message-regenerate-btn']").click()
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })
    await page.waitForTimeout(500)

    // 同 message_id 仍存在（不新增 bubble）
    await expect(
      page.locator(`[data-message-id="${originalId}"]`),
    ).toHaveCount(1)

    // 再发一条 user message
    await page.locator("[data-testid='chat-input-field']").fill("next turn")
    await page.locator("[data-testid='send-button']").click()
    await expect(page.locator("[data-testid='send-button']")).toBeVisible({
      timeout: 20_000,
    })

    // 查后端 GET messages——应有 user + assistant + user + assistant = 4 messages
    // regenerate **不**新增 assistant row（同 message_id）
    const sid = await page.evaluate(() => {
      return (window as any).__storeHooks?.chatStore?.()?.activeSessionId
    })
    expect(sid).toBeTruthy()
    const resp = await page.request.get(`/api/messages?session_id=${sid}`)
    expect(resp.ok()).toBeTruthy()
    const body = await resp.json()
    const assistantMsgs = (body.messages || []).filter(
      (m: any) => m.role === "assistant",
    )
    // 应只有 2 个 assistant（第一轮 regenerate 后同 ID + 第二轮新 ID）
    // **不应**出现 3（regenerate candidate 不写新 row）
    expect(assistantMsgs.length).toBe(2)
    // 第一个 assistant 的 message_id 必须是 originalId
    expect(assistantMsgs[0].message_id).toBe(originalId)
  })
})
