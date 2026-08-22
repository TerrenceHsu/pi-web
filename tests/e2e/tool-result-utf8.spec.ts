import { expect, test, type Page } from "@playwright/test"

type Session = { id: string }

async function waitForWorkspace(page: Page): Promise<void> {
  await expect(page.locator("[data-testid='new-chat-button']")).toBeVisible()
  await page.waitForFunction(() => !!(window as any).__storeHooks?.chatStore)
}

async function waitUntilIdle(page: Page): Promise<void> {
  await page.waitForFunction(
    () => {
      const store = (window as any).__storeHooks?.chatStore?.()
      return store && !store.sending && !store.streaming && !store.currentRequestId
    },
    undefined,
    { timeout: 20_000 },
  )
}

async function sendMessage(page: Page, text: string): Promise<void> {
  const input = page.locator("[data-testid='chat-input-field']")
  await expect(input).toBeEnabled()
  await input.fill(text)
  await page.locator("[data-testid='send-button']").click()
  await waitUntilIdle(page)
}

test("ToolResult cards keep cross-turn order and Chinese DDGS text after reload", async ({
  page,
}) => {
  const response = await page.request.post("/api/sessions", {
    data: { title: `tool-result-utf8-${Date.now()}` },
  })
  expect(response.ok(), await response.text()).toBe(true)
  const session = (await response.json()) as Session

  await page.goto(`/chat/${session.id}`)
  await waitForWorkspace(page)

  await sendMessage(page, "UTF8_DDGS_TURN_1 搜索第一轮")
  const firstCard = page.locator(
    '[data-testid="mcp-tool-card"][data-tool-call-id="call_utf8_ddgs_1"]',
  )
  await expect(firstCard).toContainText("中文搜索结果：第一轮北京天气晴朗，编码保持完整。")

  await sendMessage(page, "UTF8_DDGS_TURN_2 搜索第二轮")
  const secondCard = page.locator(
    '[data-testid="mcp-tool-card"][data-tool-call-id="call_utf8_ddgs_2"]',
  )
  await expect(secondCard).toContainText("中文搜索结果：第二轮北京天气晴朗，编码保持完整。")

  const timeline = page.locator(
    '[data-testid="user-message"], [data-testid="mcp-tool-card"], [data-testid="assistant-message"]',
  )
  const liveText = await timeline.allInnerTexts()
  const liveIndex = (text: string) => liveText.findIndex((entry) => entry.includes(text))
  expect(liveIndex("UTF8_DDGS_TURN_1")).toBeLessThan(liveIndex("第一轮北京天气晴朗"))
  expect(liveIndex("第一轮北京天气晴朗")).toBeLessThan(liveIndex("DDGS 第1轮处理完成"))
  expect(liveIndex("DDGS 第1轮处理完成")).toBeLessThan(liveIndex("UTF8_DDGS_TURN_2"))
  expect(liveIndex("UTF8_DDGS_TURN_2")).toBeLessThan(liveIndex("第二轮北京天气晴朗"))
  expect(liveIndex("第二轮北京天气晴朗")).toBeLessThan(liveIndex("DDGS 第2轮处理完成"))

  await page.reload()
  await waitForWorkspace(page)

  await expect(firstCard).toContainText("中文搜索结果：第一轮北京天气晴朗，编码保持完整。")
  await expect(secondCard).toContainText("中文搜索结果：第二轮北京天气晴朗，编码保持完整。")
  await expect(page.locator('[data-testid="mcp-tool-card"]')).toHaveCount(2)

  const restoredText = await timeline.allInnerTexts()
  const restoredIndex = (text: string) => restoredText.findIndex((entry) => entry.includes(text))
  expect(restoredIndex("UTF8_DDGS_TURN_1")).toBeLessThan(
    restoredIndex("第一轮北京天气晴朗"),
  )
  expect(restoredIndex("第一轮北京天气晴朗")).toBeLessThan(
    restoredIndex("DDGS 第1轮处理完成"),
  )
  expect(restoredIndex("DDGS 第1轮处理完成")).toBeLessThan(
    restoredIndex("UTF8_DDGS_TURN_2"),
  )
  expect(restoredIndex("UTF8_DDGS_TURN_2")).toBeLessThan(
    restoredIndex("第二轮北京天气晴朗"),
  )
  expect(restoredIndex("第二轮北京天气晴朗")).toBeLessThan(
    restoredIndex("DDGS 第2轮处理完成"),
  )
})
