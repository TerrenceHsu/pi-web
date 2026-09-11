import { expect, test } from "@playwright/test"

test.use({ channel: "chromium" })

test("browser pixels keep the same geometry across motion and HD frames", async ({ page }) => {
  test.skip(process.env.PI_E2E_BROWSER !== "1", "Explicit installed Chromium opt-in")
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 1600, height: 1000 })
  const created = await page.request.post("/api/sessions", { data: { title: "browser-geometry" } })
  const sid = (await created.json()).id as string
  try {
    await page.goto(`/chat/${sid}`)
    await page.getByTestId("desk-new-browser").click()
    await page.getByLabel("Browser rendering mode").selectOption("screenshots")
    await page.getByLabel("Browser address").fill("https://browser-fixture.example.test/geometry")
    await page.getByRole("button", { name: "Go", exact: true }).click()
    await expect(page.getByTestId("desk-tab-browser")).toContainText("Geometry fixture")
    const screen = page.getByAltText("Interactive browser page")
    await expect(screen).toBeVisible()
    await expect.poll(async () => screen.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
    for (const hd of [true, false, true]) {
      await page.getByLabel("HD browser rendering").setChecked(hd)
      await screen.dispatchEvent("wheel", { deltaY: 100 })
      const samples = await screen.evaluate(async (image: HTMLImageElement) => {
        const results = []
        for (let index = 0; index < 90; index++) {
          await new Promise(requestAnimationFrame)
          if (!image.complete || !image.naturalWidth) continue
          const canvas = document.createElement("canvas")
          canvas.width = image.naturalWidth
          canvas.height = image.naturalHeight
          const ctx = canvas.getContext("2d")!
          ctx.drawImage(image, 0, 0)
          const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data
          let left = canvas.width, top = canvas.height, right = 0, bottom = 0
          for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
            const offset = (y * canvas.width + x) * 4
            if (pixels[offset] - Math.max(pixels[offset + 1], pixels[offset + 2]) < 150) continue
            left = Math.min(left, x); right = Math.max(right, x + 1)
            top = Math.min(top, y); bottom = Math.max(bottom, y + 1)
          }
          if (!right) continue
          const rect = image.getBoundingClientRect()
          const parent = image.parentElement!.getBoundingClientRect()
          const scale = Math.min(1, parent.width / Number(image.dataset.width))
          results.push({
            intrinsic: [image.naturalWidth, image.naturalHeight],
            layout: [rect.width, rect.height],
            marker: [left * rect.width / canvas.width / scale, top * rect.height / canvas.height / scale,
              right * rect.width / canvas.width / scale, bottom * rect.height / canvas.height / scale],
          })
        }
        return results
      })
      expect(samples.length).toBeGreaterThan(10)
      const invalid = samples.filter(sample => sample.marker.some((value, i) => Math.abs(value - [100, 100, 180, 160][i]) > 2))
      expect(invalid).toEqual([])
    }
  } finally {
    await page.request.delete(`/api/sessions/${sid}`, { headers: { "X-PI-Agent-UI": "1" } })
  }
})

test("Markdown tabs retain drafts in split panes and closing a tab keeps its file", async ({ page }) => {
  const created = await page.request.post("/api/sessions", { data: { title: `desk-${Date.now()}` } })
  const sid = (await created.json()).id as string
  await page.goto(`/chat/${sid}`)
  await expect(page.getByRole("button", { name: "New Markdown", exact: true })).toHaveCount(1)
  await expect(page.locator(".workspace-toolbar").getByRole("button", { name: "New Markdown", exact: true })).toHaveCount(0)
  for (const name of ["one", "two"]) {
    await page.getByRole("button", { name: "New Markdown", exact: true }).click()
    await page.getByLabel("Markdown logical path").fill(`notes/${name}.md`)
    await page.getByLabel("Initial Markdown content").fill(`# ${name}\n\nOriginal`)
    await page.locator("form.create-markdown").getByRole("button", { name: "Create", exact: true }).click()
    await expect(page.locator("[data-testid='workspace-file-preview']:visible")).toContainText(`${name}.md`)
  }
  await page.getByTestId("desk-tab-file").filter({ hasText: "one.md" }).click()
  await page.getByRole("tab", { name: "Edit", exact: true }).click()
  await page.getByLabel("one.md content").fill("# Unsaved notes\nDraft survives tab switches.")
  await page.getByTestId("desk-tab-file").filter({ hasText: "two.md" }).click()
  await page.getByLabel("Preview layout").selectOption("columns")
  await expect(page.locator("[data-testid='workspace-file-preview']:visible")).toHaveCount(2)
  await expect(page.getByLabel("one.md content")).toHaveValue(/Unsaved notes/)
  const one = page.locator("[data-testid='workspace-file-preview']").filter({ hasText: "notes/one.md" })
  await one.getByRole("button", { name: "Save", exact: true }).click()
  await expect(one).toContainText("Unsaved notes")
  await expect(one.locator(".markdown-preview")).toBeVisible()
  await page.getByRole("button", { name: "Close one.md", exact: true }).click()
  await expect(page.getByTestId("desk-tab-file").filter({ hasText: "one.md" })).toHaveCount(0)
  await page.getByRole("button", { name: "Open one.md", exact: true }).click()
  await expect(page.getByTestId("desk-tab-file").filter({ hasText: "one.md" })).toBeVisible()
  await page.reload()
  await page.getByRole("button", { name: "Open one.md", exact: true }).click()
  await expect(page.locator(".markdown-preview:visible")).toContainText("Draft survives tab switches.")
})

test("real Chromium coexists with Markdown and accepts user login and popup clicks", async ({ page, browser }, testInfo) => {
  test.skip(process.env.PI_E2E_BROWSER !== "1", "Explicit installed Chromium opt-in")
  test.setTimeout(60_000)
  await page.setViewportSize({ width: 1600, height: 1200 })
  const frameRequests: string[] = []
  page.on("request", (request) => { if (request.url().endsWith("/frame")) frameRequests.push(request.url()) })
  const headers = { "X-PI-Agent-UI": "1" }
  const created = await page.request.post("/api/sessions", { data: { title: `browser-${Date.now()}` } })
  const sid = (await created.json()).id as string
  await page.goto(`/chat/${sid}`)
  await page.getByRole("button", { name: "New Markdown", exact: true }).click()
  await page.getByLabel("Markdown logical path").fill("notes/browser.md")
  await page.getByLabel("Initial Markdown content").fill("# Browser notes\n\nMarkdown and browser coexist.")
  await page.locator("form.create-markdown").getByRole("button", { name: "Create", exact: true }).click()
  await expect(page.locator(".markdown-preview:visible")).toContainText("Browser notes")
  await page.getByTestId("desk-new-browser").click()
  await page.getByLabel("Browser rendering mode").selectOption("screenshots")
  await expect(page.getByTestId("workspace-browser")).toBeVisible()
  await page.getByLabel("Browser address").fill("https://browser-fixture.example.test/login")
  await page.getByRole("button", { name: "Go", exact: true }).click()
  await expect(page.getByTestId("desk-tab-browser")).toContainText("Local browser fixture", { timeout: 20_000 })
  const screen = page.getByAltText("Interactive browser page")
  await expect(screen).toBeVisible()
  const clickAt = async (x: number, y: number) => {
    const box = (await screen.boundingBox())!
    const size = await screen.evaluate((image: HTMLImageElement) => ({ width: Number(image.dataset.width), height: Number(image.dataset.height) }))
    await screen.click({ position: { x: x / size.width * box.width, y: y / size.height * box.height } })
  }
  await clickAt(100, 140)
  const keys = page.getByLabel("Browser keyboard input")
  await keys.pressSequentially("demo", { delay: 100 })
  await keys.press("Tab")
  await keys.pressSequentially("fixture", { delay: 100 })
  await clickAt(100, 260)
  await expect(page.getByTestId("desk-tab-browser")).toContainText("Signed in", { timeout: 15_000 })
  await expect(page.getByTestId("browser-performance")).toContainText("Live")
  await page.getByLabel("HD browser rendering").uncheck()
  await expect.poll(async () => (await (await page.request.get(`/api/sessions/${sid}/browser`, { headers })).json()).pages[0].dpr).toBe(1)
  await page.getByLabel("HD browser rendering").check()
  await page.getByLabel("Preview layout").selectOption("columns")
  await expect(page.locator(".markdown-preview:visible")).toContainText("Browser notes")
  await expect(screen).toBeVisible()
  await expect.poll(async () => {
    const current = (await (await page.request.get(`/api/sessions/${sid}/browser`, { headers })).json()).pages[0]
    const actual = await screen.evaluate((image: HTMLImageElement) => ({ width: image.naturalWidth, height: image.naturalHeight }))
    return { cssWidth: current.width, dpr: current.dpr, widthError: Math.abs(actual.width - current.width * current.dpr), heightError: Math.abs(actual.height - current.height * current.dpr) }
  }).toEqual({ cssWidth: 320, dpr: 2, widthError: 0, heightError: 0 })
  await page.screenshot({ path: testInfo.outputPath("markdown-browser-split.png") })
  await clickAt(100, 414)
  await expect(page.getByTestId("desk-tab-browser")).toHaveCount(2)
  await expect(page.locator(".desk-tabs [role='tab'][aria-selected='true']")).toHaveCount(1)
  const resize = page.getByTestId("workspace-resizer")
  for (let index = 0; index < 24; index++) await resize.press("ArrowRight")
  await expect(page.locator(".markdown-preview:visible")).toContainText("Browser notes")
  await expect(page.locator(".desk-tabs [role='tab'][aria-selected='true']")).toBeInViewport()
  await expect.poll(() => screen.evaluate((image: HTMLImageElement) => {
    const rect = image.parentElement!.getBoundingClientRect()
    const width = Math.min(1920, Math.max(320, Math.floor(rect.width)))
    const height = Math.min(1400, Math.max(200, Math.floor(rect.height)))
    return Number(image.dataset.width) === width && Number(image.dataset.height) === height
      && image.naturalWidth === width * 2 && image.naturalHeight === height * 2
  })).toBe(true)
  await page.screenshot({ path: testInfo.outputPath("browser-tabs-and-markdown.png") })
  const pages = (await (await page.request.get(`/api/sessions/${sid}/browser`, { headers })).json()).pages
  expect(pages).toHaveLength(2)
  expect(frameRequests).toHaveLength(0)
  const guest = await browser.newContext({ storageState: { cookies: [], origins: [] } })
  try {
    const guestPage = await guest.newPage()
    await guestPage.goto("/")
    const opened = await guestPage.evaluate(({ sid, pid }) => new Promise<boolean>((resolve) => {
      const url = new URL(`/ws/browser/${sid}/${pid}`, window.location.href)
      url.protocol = "ws:"
      const socket = new WebSocket(url, "pi-browser-v1")
      socket.onopen = () => { socket.close(); resolve(true) }
      socket.onerror = () => resolve(false)
    }), { sid, pid: pages[0].id })
    expect(opened).toBe(false)
  } finally { await guest.close() }
  // User-facing Session deletion must also reclaim all browser pages/contexts.
  expect((await page.request.delete(`/api/sessions/${sid}`, { headers })).ok()).toBe(true)
  expect((await page.request.get(`/api/sessions/${sid}/browser`, { headers })).status()).toBe(404)
})
