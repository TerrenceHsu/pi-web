import { expect, test } from "@playwright/test"
import { PPO_MATH } from "../../src/pi_agent_core_py/web/frontend/tests/fixtures/math"

test("PPO formulas render locally in chat and Workspace without overflowing narrow panes", async ({ page }, testInfo) => {
  const created = await page.request.post("/api/sessions", { data: { title: "math-rendering" } })
  const sid = (await created.json()).id as string
  const failedAssets: string[] = []
  page.on("response", response => {
    if (/KaTeX.*\.(woff2?|ttf)/.test(response.url()) && !response.ok()) failedAssets.push(response.url())
  })
  try {
    // Deterministic message-history fixture; avoids racing session hydration with
    // a direct store write and never invokes a real model or writes business data.
    await page.route("**/api/messages?**", async route => {
      if (new URL(route.request().url()).searchParams.get("session_id") !== sid) return route.continue()
      await route.fulfill({ json: { messages: [{ role: "assistant", content: [{ type: "text", text: PPO_MATH }] }] } })
    })
    await page.setViewportSize({ width: 1500, height: 1050 })
    await page.goto(`/chat/${sid}`)
    const chat = page.getByTestId("assistant-markdown")
    await expect(chat.locator(".katex-display")).toHaveCount(2)
    await expect(chat.locator(".math-source")).toHaveCount(0)
    await expect(chat.locator("strong .katex")).toBeVisible()

    await page.getByRole("button", { name: "New Markdown", exact: true }).click()
    await page.getByLabel("Markdown logical path").fill("notes/ppo.md")
    await page.getByLabel("Initial Markdown content").fill(PPO_MATH)
    await page.locator("form.create-markdown").getByRole("button", { name: "Create", exact: true }).click()
    const preview = page.locator(".markdown-preview:visible")
    await expect(preview.locator(".katex-display")).toHaveCount(2)
    await page.evaluate(() => document.fonts.ready)
    expect(failedAssets).toEqual([])
    expect(await page.evaluate(() => [...document.fonts].some(font => font.family.includes("KaTeX") && font.status === "loaded"))).toBe(true)
    for (const body of [chat, preview]) {
      const formulas = body.locator(".math-display")
      for (let index = 0; index < 2; index++) {
        const measurements = await formulas.nth(index).evaluate((formula: HTMLElement) => ({
          width: formula.getBoundingClientRect().width,
          parent: formula.parentElement!.getBoundingClientRect().width,
          overflow: getComputedStyle(formula).overflowX,
        }))
        expect(measurements.width).toBeLessThanOrEqual(measurements.parent + 1)
        expect(measurements.overflow).toBe("auto")
      }
    }
    await page.screenshot({ path: testInfo.outputPath("ppo-chat-workspace.png"), animations: "disabled" })
    await page.setViewportSize({ width: 400, height: 950 })
    await expect(chat).toBeVisible()
    const longFormula = chat.locator(".math-display").last()
    expect(await chat.evaluate(element => getComputedStyle(element.closest(".body")!).flexShrink)).toBe("1")
    const scrollable = await longFormula.evaluate((formula: HTMLElement) => {
      formula.scrollLeft = formula.scrollWidth
      return { scroll: formula.scrollLeft, width: formula.clientWidth, total: formula.scrollWidth }
    })
    expect(scrollable.total).toBeGreaterThan(scrollable.width)
    expect(scrollable.scroll).toBeGreaterThan(0)
    expect(await longFormula.evaluate((formula: HTMLElement) => formula.getBoundingClientRect().right <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath("ppo-narrow.png"), animations: "disabled" })
    await page.setViewportSize({ width: 1500, height: 1050 })
    await page.reload()
    await page.getByRole("button", { name: "Open ppo.md", exact: true }).click()
    await expect(page.locator(".markdown-preview:visible .katex-display")).toHaveCount(2)
  } finally {
    await page.request.delete(`/api/sessions/${sid}`, { headers: { "X-PI-Agent-UI": "1" } })
  }
})
