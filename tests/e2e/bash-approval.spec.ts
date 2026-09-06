import { expect, test } from "@playwright/test"

test("Bash card renders a complete script and only submits the explicit decision", async ({ page }) => {
  await page.goto("/")
  const session = await (await page.request.post("/api/sessions", {
    data: { title: `Bash card ${Date.now()}` },
  })).json()
  await page.goto(`/chat/${session.id}`)
  await page.waitForFunction(() => !!(window as any).__storeHooks?.chatStore)
  await expect(page.getByTestId("chat-input-field")).toBeEnabled()
  await page.getByTestId("chat-input-field").fill("Bash UI fixture setup")
  await page.getByTestId("send-button").click()
  await expect(page.getByTestId("assistant-message").last()).toBeVisible()
  await page.waitForFunction(id => {
    const store = (window as any).__storeHooks.chatStore()
    return store.activeSessionId === id && !store.currentRequestId && !store.sending
  }, session.id)
  const script = "# <script>window.bashInjected = true</script>\n" + "# review every line\n".repeat(600) + "printf end"
  const approval = {
    approval_id: "bash-browser", request_id: "bash-request", session_id: session.id,
    tool_call_id: "task-browser", tool_name: "execution_task", tool_label: "Bash execution",
    arguments: { kind: "bash", script, cwd: "upload", timeout_seconds: 60,
      script_sha256: "a".repeat(64), backend: "local_docker", data_location: "local Docker",
      workspace_revision: 3 },
    policy_name: "execution_task", policy_metadata: {}, status: "pending",
    reason: "Review the complete script once", created_at: new Date().toISOString(), resolved_at: null,
  }
  let decisions = 0
  await page.route("**/api/requests/bash-request/approvals/bash-browser", async route => {
    expect(route.request().postDataJSON()).toEqual({ decision: "deny" })
    decisions++
    await route.fulfill({ json: { ok: true, idempotent: false,
      approval: { ...approval, status: "denied", resolved_at: new Date().toISOString() } } })
  })
  await page.evaluate(record => {
    const store = (window as any).__storeHooks.chatStore()
    store.currentRequestId = record.request_id
    store.handleEvent({ type: "tool_approval_requested", approval: record })
  }, approval)
  await expect(page.getByTestId("approval-bash-script")).toHaveText(script)
  await expect(page.getByTestId("approval-approve")).toHaveText("Approve this exact Bash script once")
  await expect(page.getByTestId("execution-scope-warning")).toContainText("discarded")
  expect(await page.evaluate(() => (window as any).bashInjected)).toBeUndefined()
  expect(decisions).toBe(0)
  await page.getByTestId("approval-deny").click()
  await expect(page.getByTestId("tool-approval-card")).toHaveAttribute("data-status", "denied")
  expect(decisions).toBe(1)
})

test("Bash history refresh reads the retained record without replaying commands", async ({ page }) => {
  await page.goto("/")
  const session = await (await page.request.post("/api/sessions", {
    data: { title: `Bash history ${Date.now()}` },
  })).json()
  const summary = { run_id: "bash-run-browser", status: "succeeded", cwd: ".", created_at_ms: 1 }
  const requests: string[] = []
  await page.route(`**/api/workspaces/${session.id}/bash-runs**`, async route => {
    requests.push(route.request().method())
    await route.fulfill({ json: route.request().url().endsWith(summary.run_id)
      ? { ...summary, script: "printf 'safe'", result: { stdout: "safe", file_changes_saved: false } }
      : { runs: [summary] } })
  })
  for (let view = 0; view < 2; view++) {
    await page.goto(`/chat/${session.id}`)
    await page.getByTestId("workspace-panel").getByRole("button", { name: "extensions", exact: true }).click()
    const history = page.getByTestId("bash-history")
    await expect(history).toContainText("succeeded")
    await history.locator("li button").click()
    await expect(page.getByTestId("bash-run-detail")).toContainText("file_changes_saved")
  }
  expect(requests.length).toBeGreaterThanOrEqual(4)
  expect(requests.every(method => method === "GET")).toBe(true)
})
