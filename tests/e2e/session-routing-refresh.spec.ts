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

async function isolateAdminLogin(page: Page): Promise<void> {
  // The suite-wide storageState token must remain valid for specs that run
  // after this logout test. Replace this context's cookie with a fresh admin
  // session so Sign out revokes only the test-local token.
  const response = await page.request.post("/api/auth/login", {
    headers: { "X-PI-Agent-UI": "1" },
    data: { name: "admin", password: "123456" },
  })
  expect(response.ok(), await response.text()).toBe(true)
}

test.describe.serial("P2-A Session route and full refresh recovery", () => {
  test("Session switch synchronizes URL and browser history", async ({ page }) => {
    await page.goto("/")
    await waitForWorkspace(page)
    const suffix = Date.now()
    const first = await createSession(page, `route-first-${suffix}`)
    const second = await createSession(page, `route-second-${suffix}`)

    await page.goto(`/chat/${first.id}`)
    await waitForWorkspace(page)
    await expect(page).toHaveURL(new RegExp(`/chat/${first.id}$`))

    await page
      .locator("[data-testid='session-item']")
      .filter({ hasText: second.title })
      .click()
    await expect(page).toHaveURL(new RegExp(`/chat/${second.id}$`))

    await page.goBack()
    await expect(page).toHaveURL(new RegExp(`/chat/${first.id}$`))
    await expect(
      page.locator("[data-testid='session-item'].active"),
    ).toContainText(first.title)
  })

  test("reload restores exact history, AGENT.md, Memory.md and file tree", async ({ page }) => {
    const suffix = Date.now()
    const session = await createSession(page, `restore-${suffix}`)
    await page.goto(`/chat/${session.id}`)
    await waitForWorkspace(page)

    await sendMessage(page, `before-checkpoint-${suffix}`)
    await page.locator("[data-testid='chat-input-field']").fill("/checkpointer")
    await page.locator("[data-testid='send-button']").click()
    await expect(page.locator("[data-testid='checkpoint-notice']")).toBeVisible({
      timeout: 20_000,
    })
    await waitUntilIdle(page)

    const persistedPrompt = `after-checkpoint-${suffix}`
    await sendMessage(page, persistedPrompt)
    await page.reload()
    await waitForWorkspace(page)

    await expect(page).toHaveURL(new RegExp(`/chat/${session.id}$`))
    await expect(page.locator("[data-testid='user-message']").last()).toContainText(
      persistedPrompt,
    )
    await expect(page.locator("[data-testid='assistant-message']").last()).toContainText(
      "fake backend",
    )
    const tree = page.locator("[data-testid='session-folder']")
    await expect(tree.getByText("AGENT.md", { exact: true })).toBeVisible()
    await expect(tree.getByText("Memory.md", { exact: true })).toBeVisible()
  })

  test("reload recovers running Prompt and Regenerate with live events", async ({ page }) => {
    const session = await createSession(page, `active-refresh-${Date.now()}`)
    const activeQueries: string[] = []
    const recoveryQueries: string[] = []
    page.on("request", (request) => {
      if (request.url().includes("/api/requests?")) activeQueries.push(request.url())
      if (request.url().includes("/api/events?")) recoveryQueries.push(request.url())
    })

    await page.goto(`/chat/${session.id}`)
    await waitForWorkspace(page)
    await page.locator("[data-testid='chat-input-field']").fill("P2A_SLOW_REFRESH prompt")
    await page.locator("[data-testid='send-button']").click()
    const promptRequestId = await page.waitForFunction(
      () => (window as any).__storeHooks?.chatStore?.()?.currentRequestId,
    ).then((handle) => handle.jsonValue())
    expect(promptRequestId).toMatch(/^req_/)

    await page.reload()
    await waitForWorkspace(page)
    await expect(page.locator("[data-testid='stop-button']")).toBeVisible()
    await waitUntilIdle(page)
    await expect(page.locator("[data-testid='assistant-message']").last()).toContainText(
      "Hello from delayed fake backend",
    )
    expect(
      activeQueries.some(
        (url) => url.includes(`session_id=${session.id}`) && url.includes("status=active"),
      ),
    ).toBe(true)
    expect(
      recoveryQueries.some(
        (url) =>
          url.includes(`session_id=${session.id}`) &&
          url.includes(`request_id=${promptRequestId}`),
      ),
    ).toBe(true)

    await page.locator("[data-testid='message-regenerate-btn']").last().click()
    const regenerateRequestId = await page.waitForFunction(
      (previous) => {
        const store = (window as any).__storeHooks?.chatStore?.()
        return store?.currentRequestId && store.currentRequestId !== previous
          ? store.currentRequestId
          : null
      },
      promptRequestId,
    ).then((handle) => handle.jsonValue())
    expect(regenerateRequestId).toMatch(/^req_/)

    await page.reload()
    await waitForWorkspace(page)
    await expect
      .poll(() =>
        page.evaluate(() => (window as any).__storeHooks.chatStore().regeneration.status),
      )
      .toBe("running")
    await expect(page.locator("[data-testid='stop-button']")).toBeVisible()
    await waitUntilIdle(page)
    await expect(page.locator("[data-testid='assistant-message']").last()).toContainText(
      "fake backend",
    )
  })

  test("invalid and deleted Session IDs safely fall back without probing them", async ({ page }) => {
    const fallback = await createSession(page, `fallback-${Date.now()}`)
    await page.goto(`/chat/${fallback.id}`)
    await waitForWorkspace(page)

    const forbiddenId = "sess-not-owned-by-admin"
    const requestedApiUrls: string[] = []
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/api/")) {
        requestedApiUrls.push(request.url())
      }
    })
    await page.goto(`/chat/${forbiddenId}`)
    await waitForWorkspace(page)
    await expect(page).not.toHaveURL(new RegExp(`/chat/${forbiddenId}$`))
    expect(requestedApiUrls.some((url) => url.includes(forbiddenId))).toBe(false)

    const deleted = await createSession(page, `deleted-${Date.now()}`)
    const deletedResponse = await page.request.delete(`/api/sessions/${deleted.id}`)
    expect(deletedResponse.ok()).toBe(true)
    requestedApiUrls.length = 0
    await page.goto(`/chat/${deleted.id}`)
    await waitForWorkspace(page)
    await expect(page).not.toHaveURL(new RegExp(`/chat/${deleted.id}$`))
    expect(requestedApiUrls.some((url) => url.includes(deleted.id))).toBe(false)
  })

  test("logout clears route and Alice cannot inherit the admin workspace", async ({ page }) => {
    await isolateAdminLogin(page)
    const marker = `admin-private-${Date.now()}`
    const adminSession = await createSession(page, `admin-private-${Date.now()}`)
    await page.goto(`/chat/${adminSession.id}`)
    await waitForWorkspace(page)
    await sendMessage(page, marker)

    const logoutReload = page.waitForNavigation({ waitUntil: "domcontentloaded" })
    await page.locator("[data-testid='sign-out-button']").click()
    await logoutReload
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator("[data-testid='login-form']")).toBeVisible()

    await page.locator("[data-testid='login-name']").fill("alice")
    await page.locator("[data-testid='login-password']").fill("alice-pass")
    await page.locator("[data-testid='login-submit']").click()
    await waitForWorkspace(page)

    await expect(page).toHaveURL(/\/chat\/[^/]+$/)
    expect(page.url()).not.toContain(adminSession.id)
    await expect(page.locator("[data-testid='message-list']")).not.toContainText(marker)
    const aliceSessionId = new URL(page.url()).pathname.split("/").at(-1)
    expect(aliceSessionId).toBeTruthy()
    expect(aliceSessionId).not.toBe(adminSession.id)
  })
})
