import { expect, test, type Locator, type Page } from "@playwright/test"

const WORKSPACE_KEY = "pi-agent-layout-workspace-width"

async function width(locator: Locator): Promise<number> {
  return (await locator.boundingBox())!.width
}

async function openLayout(page: Page): Promise<string> {
  const created = await page.request.post("/api/sessions", {
    data: { title: `panel-resize-${Date.now()}` },
  })
  expect(created.ok()).toBe(true)
  const sid = (await created.json()).id as string
  await page.goto(`/chat/${sid}`)
  await expect(page.getByTestId("workspace-sidebar")).toBeVisible()
  return sid
}

async function removeSession(page: Page, sid: string): Promise<void> {
  const removed = await page.request.delete(`/api/sessions/${sid}`, {
    headers: { "X-PI-Agent-UI": "1" },
  })
  expect(removed.ok()).toBe(true)
}

async function expectUsableNarrowChat(page: Page): Promise<void> {
  const chat = page.getByTestId("chat-panel")
  await expect.poll(() => width(page.getByTestId("chat-input-field"))).toBeGreaterThanOrEqual(230)
  await expect
    .poll(async () =>
      chat.evaluate((panel) => {
        const panelBox = panel.getBoundingClientRect()
        const selectors = [
          '[data-testid="chat-input-field"]',
          '[data-testid="send-button"]',
          '[data-testid="coding-mode-toggle"]',
          '[data-testid="plan-mode-toggle"]',
          '[data-testid="attach-button"]',
          '[data-testid="provider-selector"]',
          ".context-budget",
        ]
        const boxes = selectors.map((selector) => ({
          selector,
          box: panel.querySelector(selector)?.getBoundingClientRect(),
        }))
        const clipped = boxes
          .filter(
            ({ box }) =>
              !box ||
              !box.width ||
              !box.height ||
              box.left < panelBox.left - 1 ||
              box.right > panelBox.right + 1 ||
              box.top < panelBox.top - 1 ||
              box.bottom > panelBox.bottom + 1,
          )
          .map(({ selector }) => selector)
        const provider = panel
          .querySelector('[data-testid="provider-selector"]')!
          .getBoundingClientRect()
        const context = panel.querySelector(".context-budget")!.getBoundingClientRect()
        const headersOverlap =
          Math.min(provider.right, context.right) - Math.max(provider.left, context.left) > 1 &&
          Math.min(provider.bottom, context.bottom) - Math.max(provider.top, context.top) > 1
        return { clipped, headersOverlap }
      }),
    )
    .toEqual({ clipped: [], headersOverlap: false })
}

test("middle and chat widths move together with pointer, keyboard, limits and reset", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 })
  const sid = await openLayout(page)
  try {
    const middle = page.getByTestId("workspace-sidebar")
    const chat = page.getByTestId("chat-panel")
    const separator = page.getByTestId("workspace-resizer")
    const originalMiddle = await width(middle)
    const originalChat = await width(chat)
    expect(await width(separator)).toBe(8)
    const handle = (await separator.boundingBox())!
    await page.mouse.move(handle.x + handle.width / 2, handle.y + 180)
    await page.mouse.down()
    await page.mouse.move(handle.x + handle.width / 2 + 500, handle.y + 180, {
      steps: 12,
    })
    await page.mouse.up()
    await expect.poll(() => width(middle)).toBeCloseTo(originalMiddle + 500, 0)
    expect(await width(chat)).toBeCloseTo(originalChat - 500, 0)
    expect(await width(middle)).toBeGreaterThan(760)
    await page.screenshot({
      path: testInfo.outputPath("workspace-880.png"),
      animations: "disabled",
    })
    await separator.focus()
    await separator.press("End")
    await expect.poll(() => width(chat)).toBeCloseTo(320, 0)
    await expectUsableNarrowChat(page)
    await page.screenshot({
      path: testInfo.outputPath("chat-minimum-320.png"),
      animations: "disabled",
    })
    for (let step = 0; step < 5; step++) await separator.press("Shift+ArrowLeft")
    await expect.poll(() => width(chat)).toBeCloseTo(640, 0)
    await expectUsableNarrowChat(page)
    await separator.press("Home")
    await expect.poll(() => width(middle)).toBeCloseTo(280, 0)
    await separator.press("ArrowLeft")
    expect(await width(middle)).toBeCloseTo(280, 0)
    await separator.press("Shift+ArrowRight")
    await expect.poll(() => width(middle)).toBeCloseTo(344, 0)
    await separator.dblclick()
    await expect.poll(() => width(middle)).toBeCloseTo(380, 0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(1920)
    const sidebarSeparator = page.getByTestId("sidebar-resizer")
    const sidebar = page.getByTestId("session-sidebar")
    await sidebarSeparator.focus()
    await sidebarSeparator.press("Home")
    await expect.poll(() => width(sidebar)).toBeCloseTo(180, 0)
    await sidebarSeparator.press("End")
    await expect.poll(() => width(sidebar)).toBeCloseTo(440, 0)
    await sidebarSeparator.dblclick()
    await expect.poll(() => width(sidebar)).toBeCloseTo(260, 0)

    // Just above the drawer breakpoint, both separators must still leave usable panels.
    await page.setViewportSize({ width: 1051, height: 1000 })
    await expect(sidebarSeparator).toBeVisible()
    await expect(separator).toBeVisible()
    await separator.press("Home")
    await sidebarSeparator.press("End")
    await separator.press("End")
    await expect.poll(() => width(sidebar)).toBeCloseTo(435, 0)
    await expect.poll(() => width(middle)).toBeCloseTo(280, 0)
    await expect.poll(() => width(chat)).toBeCloseTo(320, 0)
    await expectUsableNarrowChat(page)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(1051)
  } finally {
    await removeSession(page, sid)
  }
})

test("column preferences survive reload, temporary smaller windows and the Results drawer", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1920, height: 1000 })
  const sid = await openLayout(page)
  try {
    const separator = page.getByTestId("workspace-resizer")
    const middle = page.getByTestId("workspace-sidebar")
    await separator.focus()
    await separator.press("End")
    const preferred = await width(middle)
    const stored = await page.evaluate((key) => localStorage.getItem(key), WORKSPACE_KEY)
    expect(preferred).toBeGreaterThan(760)
    await page.reload()
    await expect.poll(() => width(middle)).toBeCloseTo(preferred, 0)
    await page.setViewportSize({ width: 1200, height: 900 })
    await expect.poll(() => width(middle)).toBeLessThan(preferred)
    expect(await width(page.getByTestId("chat-panel"))).toBeGreaterThanOrEqual(319)
    expect(await page.evaluate((key) => localStorage.getItem(key), WORKSPACE_KEY)).toBe(stored)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(1200)
    await page.setViewportSize({ width: 900, height: 900 })
    await expect(separator).toBeHidden()
    const results = page.getByTestId("workspace-drawer-trigger")
    await expect(results).toBeVisible()
    await results.click()
    await expect(results).toHaveAttribute("aria-expanded", "true")
    await page.getByRole("button", { name: "Close Workspace results", exact: true }).click()
    await expect(results).toHaveAttribute("aria-expanded", "false")
    await page.setViewportSize({ width: 1920, height: 1000 })
    await expect(separator).toBeVisible()
    await expect.poll(() => width(middle)).toBeCloseTo(preferred, 0)
    expect(await page.evaluate((key) => localStorage.getItem(key), WORKSPACE_KEY)).toBe(stored)
  } finally {
    await removeSession(page, sid)
  }
})
