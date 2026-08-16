import { expect, test, type Page } from "@playwright/test"

type Session = { id: string; title: string }

async function createSession(page: Page, title: string): Promise<Session> {
  const response = await page.request.post("/api/sessions", { data: { title } })
  expect(response.ok(), await response.text()).toBe(true)
  return response.json()
}

async function waitForWorkspace(page: Page): Promise<void> {
  await expect(page.locator("[data-testid='new-chat-button']")).toBeVisible()
  await page.waitForFunction(() => !!(window as any).__storeHooks?.chatStore)
}

async function sendApprovalPrompt(page: Page, marker: string): Promise<string> {
  const input = page.locator("[data-testid='chat-input-field']")
  await expect(input).toBeEnabled()
  await input.fill(marker)
  await expect(page.locator("[data-testid='send-button']")).toBeEnabled()
  await page.locator("[data-testid='send-button']").click()
  return page
    .waitForFunction(
      () => (window as any).__storeHooks?.chatStore?.()?.currentRequestId,
    )
    .then((handle) => handle.jsonValue())
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

test.describe.serial("P2-B Human Approval UI", () => {
  test("pending approval survives reload and Approve once executes the ToolCall", async ({
    page,
  }) => {
    await page.goto("/")
    await waitForWorkspace(page)
    const session = await createSession(page, `approval-${Date.now()}`)
    await page.goto(`/chat/${session.id}`)
    await waitForWorkspace(page)

    const requestId = await sendApprovalPrompt(page, "P2B_APPROVAL_APPROVE")
    expect(requestId).toMatch(/^req_/)
    const card = page.locator("[data-testid='tool-approval-card']")
    await expect(card).toBeVisible()
    await expect(card).toHaveAttribute("data-status", "pending")
    await expect(page.locator("[data-testid='approval-tool-name']")).toContainText(
      "Write File",
    )
    await expect(page.locator("[data-testid='approval-arguments']")).toContainText(
      "approved-e2e.md",
    )

    await page.reload()
    await waitForWorkspace(page)
    await expect(card).toBeVisible()
    await expect(card).toHaveAttribute("data-status", "pending")
    await expect(page.locator("[data-testid='stop-button']")).toBeVisible()

    await page.locator("[data-testid='approval-approve']").click()
    await expect(card).toHaveAttribute("data-status", "approved")
    await waitUntilIdle(page)
    await expect(page.locator("[data-testid='assistant-message']").last()).toContainText(
      "Approval flow finished",
    )
    await expect(page.locator("[data-testid='session-folder']")).toContainText(
      "approved-e2e.md",
    )

    const approvals = await page.request.get(
      `/api/requests/${requestId}/approvals`,
    )
    expect(approvals.ok(), await approvals.text()).toBe(true)
    expect((await approvals.json()).approvals[0].status).toBe("approved")
  })

  test("Deny skips the ToolCall and leaves the Session folder unchanged", async ({ page }) => {
    const session = await createSession(page, `deny-${Date.now()}`)
    await page.goto(`/chat/${session.id}`)
    await waitForWorkspace(page)

    const requestId = await sendApprovalPrompt(page, "P2B_APPROVAL_DENY")
    const card = page.locator("[data-testid='tool-approval-card']")
    await expect(card).toHaveAttribute("data-status", "pending")
    await page.locator("[data-testid='approval-deny']").click()
    await expect(card).toHaveAttribute("data-status", "denied")
    await waitUntilIdle(page)

    await expect(page.locator("[data-testid='assistant-message']").last()).toContainText(
      "Approval flow finished",
    )
    await expect(page.locator("[data-testid='session-folder']")).not.toContainText(
      "denied-e2e.md",
    )
    const approvals = await page.request.get(
      `/api/requests/${requestId}/approvals`,
    )
    expect((await approvals.json()).approvals[0].status).toBe("denied")
  })
})
