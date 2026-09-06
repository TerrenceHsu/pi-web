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
  await expect(page.locator("[data-testid='send-button']")).toBeEnabled()
  const accepted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/prompt/async") &&
      response.request().method() === "POST",
  )
  await page.locator("[data-testid='send-button']").click()
  expect((await accepted).status()).toBe(202)
  await waitUntilIdle(page)
  await expect(
    page.locator("[data-testid='user-message']").filter({ hasText: text }),
  ).toBeVisible()
}

function blockedBudget(sessionId: string) {
  return {
    session_id: sessionId,
    provider_id: "e2e",
    model_id: "tiny-context",
    capability_source: "user",
    estimate: {
      system_prompt_tokens: 50,
      message_tokens: 930,
      tool_definition_tokens: 10,
      estimated_input_tokens: 990,
      reserved_output_tokens: 128,
      projected_tokens: 1118,
      context_window: 1000,
      input_ratio: 0.99,
      effective_input_budget: 850,
      effective_ratio: 1.165,
      projected_ratio: 1.118,
      level: "blocked",
      can_send: false,
      approximate: true,
      estimator_version: "mixed-char-v1",
    },
  }
}

test("blocked budget preserves the draft; compaction survives a full refresh", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000)
  const session = await createSession(page, `context-budget-${Date.now()}`)
  await page.goto(`/chat/${session.id}`)
  await waitForWorkspace(page)

  // More than the default four retained turns gives the real compaction API
  // a complete, oldest turn to summarize.
  for (let index = 1; index <= 5; index += 1) {
    await sendMessage(page, `context turn ${index}${index === 1 ? " context-evidence".repeat(600) : ""}`)
  }

  const original = await (await page.request.get(`/api/messages?session_id=${session.id}`)).json()

  const budget = blockedBudget(session.id)
  await page.route(`**/api/sessions/${session.id}/context-budget`, async (route) => {
    const actual = await (await route.fetch()).json()
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      ...actual,
      ...budget,
      compaction: { ...actual.compaction, can_auto_compact: false },
    }) })
  })
  await page.route(
    `**/api/sessions/${session.id}/context-budget/estimate`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(budget),
      })
    },
  )

  await page.reload()
  await waitForWorkspace(page)
  await expect(page.locator("[data-testid='context-budget-label']")).toHaveText(
    "Context ~117%",
  )
  await expect(page.locator("[data-testid='context-compact-button']")).toBeVisible()

  const draft = "keep this draft while blocked"
  await page.locator("[data-testid='chat-input-field']").fill(draft)
  await expect(page.locator("[data-testid='send-button']")).toBeDisabled()
  await expect(page.locator("[data-testid='chat-input-field']")).toHaveValue(draft)

  const compactResponse = page.waitForResponse(
    (response) =>
      response.url().includes(`/api/sessions/${session.id}/context/compact`) &&
      response.status() === 200,
  )
  await page.locator("[data-testid='context-compact-button']").click()
  await compactResponse
  await expect(page.locator("[data-testid='context-summary-card']")).toBeVisible()
  await expect(page.locator("[data-testid='context-summary-text']")).toContainText("context turn 1")
  await expect(page.locator("[data-testid='context-token-change']")).toContainText("Estimated input")
  await expect(page.locator("[data-testid='chat-input-field']")).toHaveValue(draft)
  const after = await (await page.request.get(`/api/messages?session_id=${session.id}`)).json()
  expect(after.messages).toEqual(original.messages)

  const autoToggle = page.locator("[data-testid='context-auto-compact-toggle']")
  await expect(autoToggle).toBeChecked()
  const settingSaved = page.waitForResponse((response) =>
    response.url().endsWith(`/api/sessions/${session.id}/context/compaction`)
    && response.request().method() === "PUT",
  )
  await autoToggle.uncheck()
  expect((await settingSaved).status()).toBe(200)
  await expect(autoToggle).not.toBeChecked()

  await page.reload()
  await waitForWorkspace(page)
  await expect(page).toHaveURL(new RegExp(`/chat/${session.id}$`))
  await expect(page.locator("[data-testid='context-summary-card']")).toBeVisible()
  await expect(autoToggle).not.toBeChecked()
  await expect(page.locator("[data-testid='user-message']").last()).toContainText(
    "context turn 5",
  )

  await page.locator("[data-testid='context-details-button']").click()
  await expect(page.locator("[data-testid='context-summary-text']")).toContainText("context turn 1")
  const sourceButton = page.locator("[data-testid='context-source-button']").first()
  const sourceId = (await sourceButton.textContent())!.trim()
  await sourceButton.click()
  const sourcePreview = page.locator("[data-testid='context-source-preview']")
  await expect(sourcePreview.locator("pre")).toContainText("context turn 1")
  const nextPage = page.waitForResponse((response) =>
    response.url().includes(`/context/source/${sourceId}?offset=6000`),
  )
  await sourcePreview.getByRole("button", { name: "Next page" }).click()
  const sourceResponse = await nextPage
  expect(sourceResponse.status()).toBe(200)
  expect((await sourceResponse.json()).offset).toBe(6000)
  await expect(sourcePreview.getByRole("button", { name: "Previous page" })).toBeVisible()
  await testInfo.attach("working-context-panel", {
    body: await page.screenshot({ path: testInfo.outputPath("working-context-panel.png") }),
    contentType: "image/png",
  })

  const another = await createSession(page, `context-isolation-${Date.now()}`)
  const denied = await page.request.get(`/api/sessions/${another.id}/context/source/${sourceId}`)
  expect(denied.status()).toBe(404)
  await page.goto(`/chat/${another.id}`)
  await waitForWorkspace(page)
  await expect(page.locator("[data-testid='context-summary-card']")).toHaveCount(0)
  await expect(page.locator("[data-testid='context-source-preview']")).toHaveCount(0)
  const anotherState = await (await page.request.get(`/api/sessions/${another.id}/context/compaction`)).json()
  expect(anotherState.auto_compact).toBe(true)
})
