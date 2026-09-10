import { expect, test } from "@playwright/test"

// The launcher seeds this non-admin account inside its temporary data root.
// Do not reuse the suite's admin cookie or contact a real model service.
test.use({ storageState: { cookies: [], origins: [] } })

test("unified provider configuration persists protocol and endpoint without replacing its key", async ({ page }, testInfo) => {
  const headers = { "X-PI-Agent-UI": "1" }
  const login = await page.request.post("/api/auth/login", {
    headers,
    data: { name: "alice", password: "alice-pass" },
  })
  expect(login.ok(), await login.text()).toBe(true)
  const createdSession = await page.request.post("/api/sessions", {
    data: { title: `provider-settings-${Date.now()}` },
  })
  expect(createdSession.ok(), await createdSession.text()).toBe(true)
  const sessionId = (await createdSession.json()).id as string

  const unexpectedCalls: string[] = []
  const credentialMutations: string[] = []
  page.on("request", (request) => {
    const url = new URL(request.url())
    if (
      /^\/api\/prompt(?:\/|$)/.test(url.pathname) ||
      /\/api\/provider-profiles\/[^/]+\/(?:models|model-capabilities)$/.test(url.pathname) ||
      /\/api\/credentials\/[^/]+\/validate$/.test(url.pathname) ||
      url.hostname.endsWith(".example.test")
    ) unexpectedCalls.push(url.pathname)
    if (url.pathname.startsWith("/api/credentials") && request.method() !== "GET") {
      credentialMutations.push(`${request.method()} ${url.pathname}`)
    }
  })
  await page.route(/\/api\/prompt(?:\/|$)/, (route) => route.abort())

  await page.goto(`/chat/${sessionId}`)
  await expect(page.getByTestId("provider-settings-button")).toBeEnabled()
  await page.getByTestId("provider-settings-button").click()
  const dialog = page.getByRole("dialog")
  await expect(dialog.locator(".provider-section")).toHaveCount(1)
  await expect(dialog.locator("[data-provider-id]")).toHaveCount(0)
  await dialog.getByTestId("add-profile-btn").click()
  const draft = dialog.locator('[data-profile-id="draft"]')
  await expect(draft.getByTestId("api-style-select")).toHaveValue("openai_compatible")
  await expect(draft.getByTestId("api-style-select").locator("option")).toHaveCount(2)
  await expect(draft.getByTestId("api-style-select").locator('option[value="anthropic_compatible"]'))
    .toHaveCount(1)
  await expect(dialog.getByTestId("context-window-input")).toHaveCount(0)
  await expect(dialog.getByTestId("max-output-tokens-input")).toHaveCount(0)

  const profileName = `Compatible gateway ${Date.now()}`
  await draft.getByTestId("profile-name-input").fill(profileName)
  await draft.getByTestId("base-url-input").fill("https://gateway.example.test/v1")
  await draft.getByTestId("model-input").fill("e2e-model")
  await draft.getByTestId("storage-mode-session-only").check()
  await draft.getByTestId("credential-label-input").fill("E2E synthetic provider key")
  await draft.getByTestId("api-key-input").fill("pi-e2e-provider-key-not-a-real-secret")
  await expect(draft.getByTestId("is-default-checkbox")).not.toBeChecked()
  const created = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/provider-profiles" &&
    response.request().method() === "POST",
  )
  await draft.getByTestId("save-configuration-btn").click()
  const creation = await created
  expect(creation.status(), await creation.text()).toBe(201)
  const { profile } = await creation.json()
  expect(profile).toMatchObject({
    name: profileName,
    provider_id: "openai_compatible",
    api_style: "openai_compatible",
    base_url: "https://gateway.example.test/v1",
    default_model: "e2e-model",
    is_default: false,
    status: "ready",
  })
  expect(profile.id).toBeTruthy()
  expect(profile.credential_id).toBeTruthy()

  const form = dialog.locator(`[data-profile-id="${profile.id}"]`)
  await expect(form).toBeVisible()
  await expect(form.getByTestId("api-key-input")).toHaveValue("")
  await form.getByTestId("api-style-select").selectOption("anthropic_compatible")
  await form.getByTestId("base-url-input").fill("https://gateway.example.test/anthropic")
  const updated = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/provider-profiles/${profile.id}` &&
    response.request().method() === "PATCH",
  )
  await form.getByTestId("save-configuration-btn").click()
  const update = await updated
  expect(update.status(), await update.text()).toBe(200)
  expect((await update.json()).profile).toMatchObject({
    id: profile.id,
    credential_id: profile.credential_id,
    provider_id: "openai_compatible",
    api_style: "anthropic_compatible",
    base_url: "https://gateway.example.test/anthropic",
  })
  expect(credentialMutations).toEqual(["POST /api/credentials"])
  await page.getByTestId("modal-close").click()

  const selector = page.getByTestId("provider-profile-select")
  await expect(selector).toBeEnabled()
  await expect(selector.locator("optgroup")).toHaveCount(0)
  await expect(selector.locator(`option[value="${profile.id}"]`)).toHaveCount(1)
  const bound = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/sessions/${sessionId}/model-binding` &&
    response.request().method() === "PUT",
  )
  await selector.selectOption(profile.id)
  expect((await bound).status()).toBe(200)
  await expect(selector).toHaveValue(profile.id)

  await page.reload()
  await expect(selector).toHaveValue(profile.id)
  const persisted = await page.request.get("/api/provider-profiles", { headers })
  expect(persisted.ok(), await persisted.text()).toBe(true)
  expect((await persisted.json()).profiles).toEqual(expect.arrayContaining([
    expect.objectContaining({
      id: profile.id,
      credential_id: profile.credential_id,
      api_style: "anthropic_compatible",
      base_url: "https://gateway.example.test/anthropic",
    }),
  ]))
  await page.getByTestId("provider-settings-button").click()
  await expect(form.getByTestId("api-style-select")).toHaveValue("anthropic_compatible")
  await expect(form.getByTestId("base-url-input")).toHaveValue("https://gateway.example.test/anthropic")
  await expect(form.getByTestId("storage-mode-session-only")).toBeChecked()
  await expect(form.getByTestId("api-key-input")).toHaveValue("")
  await page.screenshot({ path: testInfo.outputPath("providers-unified.png") })
  expect(unexpectedCalls).toEqual([])
})
