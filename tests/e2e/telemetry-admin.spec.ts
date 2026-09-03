import { expect, test, type Page } from "@playwright/test"

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

test("admin can inspect a content-free Agent request trace", async ({ page }) => {
  await page.goto("/")
  await waitForWorkspace(page)

  const marker = `TELEMETRY_PRIVATE_${Date.now()}`
  const input = page.locator("[data-testid='chat-input-field']")
  await input.fill(marker)
  await page.locator("[data-testid='send-button']").click()
  await waitUntilIdle(page)

  await page.locator("[data-testid='telemetry-button']").click()
  await expect(page).toHaveURL(/\/telemetry$/)
  await expect(page.locator("[data-testid='telemetry-dashboard']")).toBeVisible()
  await expect(page.getByText("Content and credentials are not collected")).toBeVisible()

  const row = page.locator("[data-testid='telemetry-span-row']").first()
  await expect(row).toBeVisible()
  await expect(page.locator("[data-testid='telemetry-dashboard']")).not.toContainText(marker)

  await row.click()
  await expect(page.locator("[data-testid='telemetry-span-detail']")).toBeVisible()
  await expect(page.locator("[data-testid='telemetry-span-detail']")).not.toContainText(marker)
})
