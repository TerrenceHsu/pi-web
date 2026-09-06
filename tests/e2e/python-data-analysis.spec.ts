import { expect, test } from "@playwright/test"

for (const decision of ["approve", "deny"] as const) {
  test(`Python analysis waits for ${decision}, survives refresh and runs only once`, async ({ page }) => {
    test.setTimeout(90_000)
    await page.goto("/")
    const session = await (await page.request.post("/api/sessions", {
      data: { title: `Python ${decision}` },
    })).json()
    const { id } = session
    const source = await (await page.request.post(`/api/sessions/${id}/files`, {
      multipart: { files: { name: "sales.csv", mimeType: "text/csv",
        buffer: Buffer.from("region,sales\nEast,10\nWest,20\nEast,30\n") } },
    })).json()
    await page.goto(`/chat/${id}`)
    const workspace = page.getByTestId("workspace-panel")
    await workspace.getByRole("button", { name: "extensions", exact: true }).click()
    const toggle = page.getByTestId("workspace-tool-run_python_analysis")
    await expect(toggle).toBeEnabled({ timeout: 20_000 })
    await expect(toggle).not.toBeChecked()
    await toggle.check()
    await expect(toggle).toBeChecked()
    await page.getByTestId("chat-input-field").fill(`分析数据 PYTHON_ANALYSIS_E2E:${source.files[0].id}`)
    await page.getByTestId("send-button").click()
    const card = page.getByTestId("tool-approval-card")
    await expect(card).toHaveAttribute("data-status", "pending")
    await expect(page.getByTestId("approval-python-code")).toContainText("plt.bar")
    const runsBefore = await (await page.request.get(`/api/workspaces/${id}/analysis`)).json()
    expect(runsBefore.runs).toHaveLength(1)
    expect(runsBefore.runs[0].status).toBe("awaiting_approval")
    await page.reload()
    await expect(card).toHaveAttribute("data-status", "pending")
    await expect(page.getByTestId("approval-python-code")).toContainText("plt.bar")
    await page.getByTestId(`approval-${decision}`).click()
    await expect(card).toHaveAttribute("data-status", decision === "approve" ? "approved" : "denied")
    const runUrl = `/api/workspaces/${id}/analysis/${runsBefore.runs[0].id}`
    await expect.poll(async () => (await (await page.request.get(runUrl)).json()).status,
      { timeout: 40_000 }).toBe(decision === "approve" ? "succeeded" : "failed")
    const finished = await (await page.request.get(runUrl)).json()
    if (decision === "approve") {
      expect(finished.result.rows).toEqual([["East", 40], ["West", 20]])
      const result = page.getByTestId("analysis-result").first()
      await expect(result).toContainText("计算完成")
      await expect(result.locator("img")).toBeVisible()
      await result.getByTestId("save-analysis").click()
      await expect(result).toContainText("Saved to Workspace")
    } else {
      expect(finished.error_code).toBe("python_execution_denied")
      expect(finished.result).toBeNull()
      const files = await (await page.request.get(`/api/sessions/${id}/files`)).json()
      expect(files.files.some((file: any) => file.logical_path.startsWith("artifacts/analysis/"))).toBe(false)
    }
  })
}
