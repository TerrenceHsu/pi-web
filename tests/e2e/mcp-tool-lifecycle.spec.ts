import { test, expect } from "@playwright/test"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

/**
 * P1-A2 — MCP tool enable/disable 真链路 E2E
 *
 * 链路：UI Add server（enabled=false）
 *   → Test connection（subprocess spawn + initialize + list_tools + close）
 *   → Enable server（subprocess 长连接 attach + tools 注册到 ToolRegistry）
 *   → GET /api/mcp/tools 验证 enabled=true
 *   → UI Disable echo tool（harness.agent.tools.unregister 真实生效）
 *   → GET /api/mcp/tools 验证 enabled=false
 *   → UI Enable echo tool（重新 register）
 *   → UI Disable server（detach，subprocess 关闭）
 *   → UI Delete server（配置删除，孤儿 disabled tool 配置清理）
 *
 * 同时覆盖 WEB_TESTING.md：
 *   #16 Add server → 表单字段清空
 *   #17 Test → "✓ last test: N tools detected"
 *   #18 Enable server → badge "attached · N tools"
 *   #19 Disable 单个 tool → badge disabled
 *
 * 不依赖真实 GLM（FakeClient）；不连接外部 MCP 服务（fake stdio subprocess）。
 *
 * webServer cwd 是 tests/e2e/，但 fake_mcp_stdio_server.py 路径用绝对路径最稳。
 */

const E2E_PYTHON = process.env.E2E_PYTHON ?? "D:/miniconda/envs/pipy/python.exe"
const FAKE_MCP_FIXTURE = path.resolve(
  __dirname,
  "..",
  "fixtures",
  "fake_mcp_stdio_server.py",
)

test.describe("MCP tool lifecycle (Smoke 10)", () => {
  test("add → test → enable → disable tool → enable tool → disable server → delete", async ({
    page,
  }) => {
    await page.goto("/")

    // 打开 MCP Modal（Modal 根元素 testid 是 "modal"，title 用于区分）
    await page.locator('[data-testid="mcp-button"]').click()
    const modal = page.locator('[data-testid="modal"]')
    await expect(modal).toContainText("MCP Servers")
    await expect(page.locator('[data-testid="mcp-server-form"]')).toBeVisible()

    // 填表单（enabled checkbox 默认未勾选）
    const serverName = `fake-e2e-${Date.now()}`
    await page.locator('[data-testid="mcp-server-name-input"]').fill(serverName)
    await page
      .locator('[data-testid="mcp-server-command-input"]')
      .fill(E2E_PYTHON)
    const argsJson = JSON.stringify([FAKE_MCP_FIXTURE])
    await page.locator('[data-testid="mcp-server-args-input"]').fill(argsJson)

    // 提交前断言 env value 字段是 type=password（不强求有值）
    const envValueInput = page.locator(
      '[data-testid="mcp-env-value-input"]',
    ).first()
    await expect(envValueInput).toHaveAttribute("type", "password")
    const secretCanary = "MCP_PRIVATE_VALUE_CANARY_20260907"
    await page.getByTestId("mcp-env-key-input").first().fill("PI_E2E_CANARY")
    await envValueInput.fill(secretCanary)

    // Add server
    await page
      .locator('[data-testid="mcp-server-form"] button[type="submit"]')
      .click()

    // #16 — 表单字段清空
    await expect(page.locator('[data-testid="mcp-server-name-input"]')).toHaveValue("")
    await expect(
      page.locator('[data-testid="mcp-server-command-input"]'),
    ).toHaveValue("")
    await expect(
      page.locator('[data-testid="mcp-server-args-input"]'),
    ).toHaveValue("[]")

    // server-card 出现，状态 disabled
    const card = page.locator(
      `[data-testid="mcp-server-card"][data-server-name="${serverName}"]`,
    )
    await expect(card).toBeVisible()
    await expect(
      card.locator('[data-testid="mcp-server-status-badge"]'),
    ).toHaveText("disabled")

    // #17 — Test connection
    await card.locator('[data-testid="mcp-server-test-btn"]').click()
    await expect(
      card.locator('[data-testid="mcp-server-test-result"]'),
    ).toContainText(/last test:\s*1 tool/i, { timeout: 15_000 })

    // env value 不出现在 body innerText（#20 复检）
    const bodyText = await page.locator("body").innerText()
    // Check the actual submitted value, not ordinary UI labels such as Password.
    expect(bodyText).not.toContain(secretCanary)

    // #18 — Enable server
    await card.locator('[data-testid="mcp-server-enable-btn"]').click()
    await expect(
      card.locator('[data-testid="mcp-server-status-badge"]'),
    ).toHaveText("enabled", { timeout: 10_000 })
    await expect(
      card.locator('[data-testid="mcp-server-attached-badge"]'),
    ).toContainText(/attached\s*·\s*1 tool/i, { timeout: 10_000 })

    // tools 出现
    const toolRow = page.locator(
      `[data-testid="mcp-tool-row"][data-tool-name="mcp__${serverName}__echo"]`,
    )
    await expect(toolRow).toBeVisible()
    await expect(
      toolRow.locator('[data-testid="mcp-tool-status-badge"]'),
    ).toHaveText("enabled")

    // GET /api/mcp/tools 验证 enabled=true（API 是真值）
    const resp1 = await page.request.get("/api/mcp/tools")
    expect(resp1.ok()).toBeTruthy()
    const data1 = await resp1.json()
    const echo1 = (data1.tools as any[]).find(
      (t) => t.name === `mcp__${serverName}__echo`,
    )
    expect(echo1).toBeDefined()
    expect(echo1.enabled).toBe(true)

    // #19 — Disable echo tool
    await toolRow.locator('[data-testid="mcp-tool-disable-btn"]').click()
    await expect(
      toolRow.locator('[data-testid="mcp-tool-status-badge"]'),
    ).toHaveText("disabled")

    // API 验证 enabled=false
    const resp2 = await page.request.get("/api/mcp/tools")
    const data2 = await resp2.json()
    const echo2 = (data2.tools as any[]).find(
      (t) => t.name === `mcp__${serverName}__echo`,
    )
    expect(echo2).toBeDefined()
    expect(echo2.enabled).toBe(false)

    // 再 Enable tool
    await toolRow.locator('[data-testid="mcp-tool-enable-btn"]').click()
    await expect(
      toolRow.locator('[data-testid="mcp-tool-status-badge"]'),
    ).toHaveText("enabled")

    const resp3 = await page.request.get("/api/mcp/tools")
    const data3 = await resp3.json()
    const echo3 = (data3.tools as any[]).find(
      (t) => t.name === `mcp__${serverName}__echo`,
    )
    expect(echo3.enabled).toBe(true)

    // Disable server → tools 不可用
    await card.locator('[data-testid="mcp-server-disable-btn"]').click()
    await expect(
      card.locator('[data-testid="mcp-server-status-badge"]'),
    ).toHaveText("disabled", { timeout: 10_000 })
    await expect(page.locator('[data-testid="mcp-tool-row"]')).toHaveCount(0, {
      timeout: 10_000,
    })

    // Delete server（confirm dialog）
    page.once("dialog", (d) => d.accept())
    await card.locator('[data-testid="mcp-server-delete-btn"]').click()
    await expect(card).toHaveCount(0)
  })
})

test.describe("Shift+Enter newline (Smoke 11)", () => {
  test("#4 Shift+Enter inserts newline, Enter sends", async ({ page }) => {
    await page.goto("/")

    // P1-B3-4: 新建独立 session——避免前 test 在 default session 留的 user_message 干扰
    await page.locator('[data-testid="new-chat-button"]').click()
    await page.waitForTimeout(150)

    const input = page.locator('[data-testid="chat-input-field"]')
    await expect(input).toBeVisible()
    await input.focus()

    await input.fill("line1")
    // 显式 down/up 模拟 Shift modifier——比 press("Shift+Enter") 更稳定
    await page.keyboard.down("Shift")
    await page.keyboard.press("Enter")
    await page.keyboard.up("Shift")
    await input.type("line2")

    const value = await input.inputValue()
    expect(value).toContain("\n")
    expect(value).toContain("line1")
    expect(value).toContain("line2")

    // 消息未发送——新 session 应该没有任何 user-message
    await expect(page.locator('[data-testid="user-message"]')).toHaveCount(0)
  })
})
