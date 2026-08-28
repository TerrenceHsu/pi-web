import { expect, test, type Page } from "@playwright/test"
import { readFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"

type Session = { id: string; title: string }

async function createSession(page: Page): Promise<Session> {
  const response = await page.request.post("/api/sessions", {
    data: { title: `workspace-results-${Date.now()}` },
  })
  expect(response.ok(), await response.text()).toBe(true)
  return response.json()
}

async function waitForApp(page: Page): Promise<void> {
  await expect(page.locator("[data-testid='new-chat-button']")).toBeVisible()
  await expect(page.locator("[data-testid='workspace-panel']")).toBeVisible()
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

test("Agent Python result automatically appears and survives reload", async ({ page }) => {
  await page.goto("/")
  await waitForApp(page)
  const session = await createSession(page)
  await page.goto(`/chat/${session.id}`)
  await waitForApp(page)

  const input = page.locator("[data-testid='chat-input-field']")
  await input.fill("WORKSPACE_RESULT_PY")
  await page.locator("[data-testid='send-button']").click()
  const approval = page.locator("[data-testid='tool-approval-card']")
  await expect(approval).toHaveAttribute("data-status", "pending")
  await page.locator("[data-testid='approval-approve']").click()
  await waitUntilIdle(page)

  const latest = page.locator("[data-testid='workspace-latest-artifact']")
  await expect(latest).toContainText("scripts/workspace-result-e2e.py")
  await expect(page.locator("[data-testid='workspace-file-preview']")).toContainText(
    "scripts/workspace-result-e2e.py",
  )
  await expect(page.locator("[aria-label='workspace-result-e2e.py code']")).toContainText(
    "workspace result ready",
  )

  await page.reload()
  await waitForApp(page)
  await expect(latest).toContainText("scripts/workspace-result-e2e.py")
  await expect(page.locator("[aria-label='workspace-result-e2e.py code']")).toContainText(
    "workspace result ready",
  )
})

test("Workspace panel creates Markdown and uploads code without attaching it to chat", async ({
  page,
}) => {
  await page.goto("/")
  await waitForApp(page)
  const session = await createSession(page)
  await page.goto(`/chat/${session.id}`)
  await waitForApp(page)

  await page.getByRole("button", { name: "New Markdown" }).click()
  await page.getByLabel("Markdown logical path").fill("notes/ui-result.md")
  await page.getByLabel("Initial Markdown content").fill("# UI result\n\nReady.")
  await page.locator("form.create-markdown").getByRole("button", { name: "Create" }).click()
  await expect(page.locator("[data-testid='workspace-file-preview']")).toContainText(
    "notes/ui-result.md",
  )
  await expect(page.locator(".markdown-preview")).toContainText("UI result")
  await page.getByRole("tab", { name: "Edit" }).click()
  await page.getByLabel("ui-result.md content").fill("# Edited UI result\n")
  await page.locator(".editor-actions").getByRole("button", { name: "Save" }).click()
  await expect(page.locator(".markdown-preview")).toContainText("Edited UI result")

  await page.locator("[data-testid='workspace-upload-input']").setInputFiles({
    name: "uploaded-result.py",
    mimeType: "text/x-python",
    buffer: Buffer.from("print('uploaded result')\n", "utf8"),
  })
  await expect(page.locator("[data-testid='workspace-file-preview']")).toContainText(
    "scripts/uploaded-result.py",
  )
  await expect(page.locator("[aria-label='uploaded-result.py code']")).toContainText(
    "uploaded result",
  )
  await expect(page.locator("[data-testid='file-chip']")).toHaveCount(0)
})

test("uploaded workbook is converted and shown read-only in the Workspace panel", async ({
  page,
}) => {
  await page.goto("/")
  await waitForApp(page)
  const session = await createSession(page)
  await page.goto(`/chat/${session.id}`)
  await waitForApp(page)

  const fixture = fileURLToPath(new URL("./fixtures/metrics.xlsx.b64", import.meta.url))
  const workbook = Buffer.from((await readFile(fixture, "utf8")).trim(), "base64")
  await page.locator("[data-testid='workspace-upload-input']").setInputFiles({
    name: "metrics.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: workbook,
  })

  const preview = page.locator("[data-testid='workspace-file-preview']")
  await expect(preview).toContainText("content.md")
  await expect(preview).toContainText("Generated document")
  await expect(preview).toContainText("Workbook summary")
  await expect(preview).toContainText("Metrics")
  await expect(preview).toContainText("Generated document output is read-only.")
  await expect(page.getByRole("tab", { name: "Edit" })).toHaveCount(0)
})

test("narrow layout exposes Workspace results as a drawer", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 760 })
  await page.goto("/")
  await waitForApp(page)

  const trigger = page.locator("[data-testid='workspace-drawer-trigger']")
  await expect(trigger).toBeVisible()
  await trigger.click()
  await expect(trigger).toHaveAttribute("aria-expanded", "true")
  await expect(page.locator("[data-testid='workspace-sidebar']")).toHaveClass(/open/)

  await page.keyboard.press("Escape")
  await expect(trigger).toHaveAttribute("aria-expanded", "false")
})
