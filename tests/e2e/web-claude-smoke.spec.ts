import { expect, test } from "@playwright/test"

test("the authenticated product shell exposes the three primary work areas", async ({
  page,
}) => {
  await page.goto("/")

  await expect(page.locator('[data-testid="session-sidebar"]')).toBeVisible()
  await expect(page.locator('[data-testid="chat-panel"]')).toBeVisible()
  await expect(page.locator('[data-testid="workspace-panel"]')).toBeVisible()
  await expect(page.locator('[data-testid="new-chat-button"]')).toBeVisible()
  await expect(page.locator('[data-testid="chat-input"]')).toBeVisible()

  const bodyText = await page.locator("body").innerText()
  expect(bodyText).not.toContain("Trace Viewer")
  expect(bodyText).not.toContain("Raw JSON")
  expect(bodyText).not.toContain("Event Stream")
})

test("a new chat persists one user and one assistant message", async ({ page }) => {
  await page.goto("/")
  await expect(page).toHaveURL(/\/chat\/[^/]+$/)
  const created = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/sessions" &&
      response.request().method() === "POST",
  )
  await page.locator('[data-testid="new-chat-button"]').click()
  const response = await created
  expect(response.ok(), await response.text()).toBe(true)
  const sessionId = (await response.json()).id as string
  expect(sessionId).toBeTruthy()
  // The previous chat URL already matches /chat/:id; wait for this creation.
  await expect(page).toHaveURL(new RegExp(`/chat/${sessionId}$`))
  await page.waitForFunction(
    (expectedSessionId) =>
      (window as any).__storeHooks?.chatStore?.().activeSessionId === expectedSessionId,
    sessionId,
  )
  await expect(page.locator('[data-testid="provider-profile-select"]')).not.toHaveAttribute(
    "aria-label",
    "Loading model…",
  )
  await expect(page.locator('[data-testid="context-budget-label"]')).not.toHaveText("Context …")

  const prompt = "hello from playwright"
  await page.locator('[data-testid="chat-input-field"]').fill(prompt)
  await expect(page.locator('[data-testid="send-button"]')).toBeEnabled()
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/prompt/async") &&
      response.request().method() === "POST",
  )
  await page.locator('[data-testid="send-button"]').click()
  expect((await accepted).status()).toBe(202)

  await expect(
    page.locator('[data-testid="user-message"]').filter({ hasText: prompt }),
  ).toHaveCount(1)
  await expect(page.locator('[data-testid="assistant-message"]')).toHaveCount(1, {
    timeout: 15_000,
  })
})
