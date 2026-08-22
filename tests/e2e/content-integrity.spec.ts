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

test("persisted U+FFFD content is marked without mutating or exposing snippets", async ({
  page,
}) => {
  const response = await page.request.post("/api/sessions", {
    data: { title: `content-integrity-${Date.now()}` },
  })
  expect(response.ok(), await response.text()).toBe(true)
  const session = (await response.json()) as Session

  await page.goto(`/chat/${session.id}`)
  await waitForWorkspace(page)

  const input = page.locator("[data-testid='chat-input-field']")
  await expect(input).toBeEnabled()
  await input.fill("U_FFFD_HISTORY_TEST create a legacy record")
  await page.locator("[data-testid='send-button']").click()
  await waitUntilIdle(page)

  const warning = page.locator('[data-testid="content-integrity-warning"]')
  await expect(warning).toHaveCount(1)
  await expect(warning).toContainText("疑似编码损坏")
  await expect(warning).toContainText("U+FFFD")
  await expect(warning).toContainText("无法自动恢复")
  await expect(warning).toHaveAttribute("data-replacement-count", "1")
  await expect(warning).toContainText("/content/0/text")

  const messagesResponse = await page.request.get(
    `/api/messages?session_id=${encodeURIComponent(session.id)}`,
  )
  expect(messagesResponse.ok(), await messagesResponse.text()).toBe(true)
  const body = await messagesResponse.json()
  expect(body.content_integrity).toEqual({
    suspected_message_count: 1,
    replacement_character_count: 1,
  })

  const assistant = body.messages.find(
    (row: any) =>
      row.message?.role === "assistant" &&
      row.message.content?.some((block: any) => block.text?.includes("\ufffd")),
  )
  expect(assistant).toBeTruthy()
  expect(assistant.message.content[0].text).toBe("历史内容\ufffd疑似损坏")
  const apiWarning = assistant.message.content_warnings[0]
  expect(apiWarning).toMatchObject({
    code: "unicode_replacement_character",
    suspected: true,
    replacement_character_count: 1,
    affected_value_count: 1,
    affected_paths: ["/content/0/text"],
    paths_truncated: false,
    auto_repairable: false,
  })
  expect(JSON.stringify(apiWarning)).not.toContain("历史内容")

  await page.reload()
  await waitForWorkspace(page)
  await expect(warning).toHaveCount(1)
  await expect(warning).toContainText("/content/0/text")
  await expect(page.locator('[data-testid="assistant-message"]')).toContainText(
    "历史内容\ufffd疑似损坏",
  )
})
