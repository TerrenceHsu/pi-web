import { expect, test } from "@playwright/test"

// The existing launcher creates an isolated temporary workspace and seeds Alice.
// Never reuse the suite admin token or connect this test to a live model service.
test.use({ storageState: { cookies: [], origins: [] } })

test("sidebar X permanently deletes a session and releases only its provider binding", async ({ page, baseURL }, testInfo) => {
  const origin = new URL(baseURL!)
  expect(["127.0.0.1", "localhost"]).toContain(origin.hostname)
  expect(["8000", "5173"]).not.toContain(origin.port)
  const headers = { "X-PI-Agent-UI": "1" }
  const login = await page.request.post("/api/auth/login", {
    headers, data: { name: "alice", password: "alice-pass" },
  })
  expect(login.ok(), await login.text()).toBe(true)

  const stamp = Date.now()
  const credentialResponse = await page.request.post("/api/credentials", {
    headers,
    data: {
      label: `Shared deletion fixture ${stamp}`, storage_mode: "session_only",
      secret_value: "pi-e2e-deletion-fixture-not-a-real-secret",
    },
  })
  expect(credentialResponse.status(), await credentialResponse.text()).toBe(201)
  const credentialId = (await credentialResponse.json()).credential.credential_id as string
  const fixtures: Array<{ sessionId: string; title: string; profileId: string }> = []
  for (const kind of ["victim", "survivor"]) {
    const title = `session-deletion-${kind}-${stamp}`
    const profileResponse = await page.request.post("/api/provider-profiles", {
      headers,
      data: {
        name: title, provider_id: "openai_compatible", api_style: "openai_compatible",
        base_url: "https://deletion-fixture.example.test/v1", credential_id: credentialId,
        default_model: "offline-fixture", enabled: true, is_default: false,
      },
    })
    expect(profileResponse.status(), await profileResponse.text()).toBe(201)
    const profileId = (await profileResponse.json()).profile.id as string
    const sessionResponse = await page.request.post("/api/sessions", {
      headers, data: { title },
    })
    expect(sessionResponse.ok(), await sessionResponse.text()).toBe(true)
    const sessionId = (await sessionResponse.json()).id as string
    const binding = await page.request.put(`/api/sessions/${sessionId}/model-binding`, {
      headers, data: { profile_id: profileId, model_id: "offline-fixture" },
    })
    expect(binding.status(), await binding.text()).toBe(200)
    fixtures.push({ sessionId, title, profileId })
  }
  const [victim, survivor] = fixtures
  const upload = await page.request.post(`/api/sessions/${victim.sessionId}/files`, {
    headers,
    multipart: { files: { name: "delete-fixture.txt", mimeType: "text/plain",
      buffer: Buffer.from("Session-owned offline deletion fixture.") } },
  })
  expect(upload.ok(), await upload.text()).toBe(true)

  const unexpectedRequests: string[] = []
  page.on("request", (request) => {
    const url = new URL(request.url())
    if (/^\/api\/prompt(?:\/|$)/.test(url.pathname) || url.hostname.endsWith(".example.test")) {
      unexpectedRequests.push(url.pathname)
    }
  })
  await page.route(/\/api\/prompt(?:\/|$)/, (route) => route.abort())
  await page.route("https://**.example.test/**", (route) => route.abort())
  await page.goto(`/chat/${victim.sessionId}`)
  const victimRow = page.getByTestId("session-item").filter({ hasText: victim.title })
  await expect(victimRow).toBeVisible()
  await expect(victimRow).toHaveClass(/active/)
  await expect(page.getByTestId("provider-profile-select")).toHaveValue(victim.profileId)

  const deletion = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/sessions/${victim.sessionId}` &&
    response.request().method() === "DELETE",
  )
  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm")
    await dialog.accept()
  })
  await victimRow.getByTestId("session-delete-btn").click()
  const deleted = await deletion
  expect(deleted.status(), await deleted.text()).toBe(200)
  expect((await deleted.json()).ok).toBe(true)
  await expect(victimRow).toHaveCount(0)
  await expect(page).not.toHaveURL(new RegExp(`/chat/${victim.sessionId}$`))
  await expect(page.getByTestId("session-error")).toHaveCount(0)

  await page.reload()
  await expect(page.getByTestId("new-chat-button")).toBeVisible()
  await expect(victimRow).toHaveCount(0)
  await expect(page).not.toHaveURL(new RegExp(`/chat/${victim.sessionId}$`))
  await expect(page.getByTestId("session-item").filter({ hasText: survivor.title })).toBeVisible()
  expect((await page.request.get(`/api/sessions/${victim.sessionId}`, { headers })).status()).toBe(404)
  expect((await page.request.get(`/api/sessions/${victim.sessionId}/files`, { headers })).status()).toBe(404)
  const sessionList = await page.request.get("/api/sessions", { headers })
  expect(sessionList.ok(), await sessionList.text()).toBe(true)
  expect((await sessionList.json()).sessions).not.toEqual(expect.arrayContaining([
    expect.objectContaining({ id: victim.sessionId }),
  ]))

  // An orphan binding used to block this DELETE even after the sidebar row vanished.
  const profileDeletion = await page.request.delete(`/api/provider-profiles/${victim.profileId}`, { headers })
  expect(profileDeletion.status(), await profileDeletion.text()).toBe(204)
  expect(await profileDeletion.body()).toHaveLength(0)
  const survivingBinding = await page.request.get(`/api/sessions/${survivor.sessionId}/model-binding`, { headers })
  expect(survivingBinding.ok(), await survivingBinding.text()).toBe(true)
  expect((await survivingBinding.json()).binding.profile_id).toBe(survivor.profileId)
  const profiles = await page.request.get("/api/provider-profiles", { headers })
  expect(profiles.ok(), await profiles.text()).toBe(true)
  const remainingProfiles = (await profiles.json()).profiles
  expect(remainingProfiles).not.toEqual(expect.arrayContaining([expect.objectContaining({ id: victim.profileId })]))
  expect(remainingProfiles).toEqual(expect.arrayContaining([
    expect.objectContaining({ id: survivor.profileId, credential_id: credentialId }),
  ]))
  const credentials = await page.request.get("/api/credentials", { headers })
  expect(credentials.ok(), await credentials.text()).toBe(true)
  expect((await credentials.json()).credentials).toEqual(expect.arrayContaining([
    expect.objectContaining({ credential_id: credentialId }),
  ]))
  expect(unexpectedRequests).toEqual([])
  await page.screenshot({ path: testInfo.outputPath("session-deletion-complete.png") })
})
