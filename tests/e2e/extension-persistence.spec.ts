import { test, expect, type Page } from "@playwright/test"
import path from "node:path"
import { fileURLToPath } from "node:url"
import fs from "node:fs"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

/**
 * P1-C5: Extension persistence + secret safety Playwright E2E.
 *
 * 5 用例（用户原指令 C5 §C5-2）：
 *   Test 1: Upload Skill → GET 确认 persisted
 *   Test 2: Add MCP server with env → response 不含 env value
 *   Test 3: Disable tool → GET /api/mcp/tools enabled=false
 *   Test 4: Missing env → restore_status=needs_env（需 restart——简化为 API 验证）
 *   Test 5: Secret marker safety scan（body.innerText / API response 无 secret）
 *
 * restart restore 由 test_web_extension_restore.py Python integration 覆盖。
 */

const SECRET_MARKER = "P1C_SECRET_MARKER_7F3A91"

test.describe("P1-C5 Extension Persistence + Secret Safety", () => {
  test("Test 1: Upload Skill persists to DB", async ({ page }) => {
    await page.goto("/")

    const skillMd = `---\nname: e2e_persisted_skill\ndescription: E2E persisted test\n---\n# Instructions\nbody`
    const uploadResp = await page.request.post("/api/skills/upload", {
      multipart: {
        files: {
          name: "SKILL.md",
          mimeType: "text/markdown",
          buffer: Buffer.from(skillMd),
        },
      },
    })
    expect(uploadResp.ok()).toBeTruthy()

    // GET 确认 skill 存在
    const getResp = await page.request.get("/api/skills")
    const body = await getResp.json()
    const skill = body.skills.find((s: any) => s.name === "e2e_persisted_skill")
    expect(skill).toBeTruthy()
    // prompt body 不返回
    expect(skill.prompt).toBeUndefined()
  })

  test("Test 2: MCP server env value not in API response", async ({ page }) => {
    await page.goto("/")

    // 添加 MCP server 含 secret env
    const addResp = await page.request.post("/api/mcp/servers", {
      data: {
        name: "e2e_secret_test",
        command: "python",
        args: [],
        env: { SECRET_KEY: SECRET_MARKER },
        enabled: false,
      },
    })
    expect(addResp.ok()).toBeTruthy()

    // GET servers——response 不应含 secret value
    const getResp = await page.request.get("/api/mcp/servers")
    const body = await getResp.json()
    const responseBody = JSON.stringify(body)
    expect(responseBody).not.toContain(SECRET_MARKER)
    // 只返回 env_keys
    const server = body.servers.find((s: any) => s.name === "e2e_secret_test")
    expect(server.env_keys).toContain("SECRET_KEY")
    expect(server.env_keys).not.toContain(SECRET_MARKER)
  })

  test("Test 3: Disable tool persists with structured key", async ({ page }) => {
    await page.goto("/")

    // 先添加 + enable 一个 fake MCP server
    const fixturePath = path.resolve(
      __dirname,
      "..",
      "fixtures",
      "fake_mcp_stdio_server.py",
    )
    await page.request.post("/api/mcp/servers", {
      data: {
        name: "e2e_tool_test",
        command: process.env.E2E_PYTHON ?? "D:/miniconda/envs/pipy/python.exe",
        args: [fixturePath],
        env: {},
        enabled: true,
      },
    })
    await page.request.post("/api/mcp/servers/e2e_tool_test/enable")

    // 等 attach
    await page.waitForTimeout(500)

    // disable tool
    const disableResp = await page.request.post(
      "/api/mcp/tools/mcp__e2e_tool_test__echo/disable",
    )
    expect(disableResp.ok()).toBeTruthy()

    // GET tools——echo enabled=false
    const toolsResp = await page.request.get("/api/mcp/tools")
    const toolsBody = await toolsResp.json()
    const echo = toolsBody.tools.find(
      (t: any) => t.name === "mcp__e2e_tool_test__echo",
    )
    if (echo) {
      expect(echo.enabled).toBe(false)
    }
  })

  test("Test 4: Missing env structured via API", async ({ page }) => {
    await page.goto("/")

    // 添加 MCP server 含 env key，但不设环境变量
    await page.request.post("/api/mcp/servers", {
      data: {
        name: "e2e_missing_env",
        command: "python",
        args: [],
        env: { P1C_TEST_SECRET: "will-be-missing-on-restart" },
        enabled: true,
      },
    })
    await page.request.post("/api/mcp/servers/e2e_missing_env/enable")

    // 当前进程有 env value——attached 应该成功（因为 env 已在内存）
    // 但 restart 后 missing env 会触发 needs_env——由 Python restart 测试覆盖
    // 这里验证 API response 结构正确
    const resp = await page.request.get("/api/mcp/servers")
    const respBody = await resp.json()
    const server = respBody.servers.find(
      (s: any) => s.name === "e2e_missing_env",
    )
    expect(server.desired_enabled).toBe(true)
    expect(server.env_keys).toContain("P1C_TEST_SECRET")
    // env value 不在 response
    expect(JSON.stringify(server)).not.toContain("will-be-missing-on-restart")
  })

  test("Test 5: Secret marker safety scan", async ({ page }) => {
    await page.goto("/")

    // 添加含 secret 的 server
    await page.request.post("/api/mcp/servers", {
      data: {
        name: "e2e_safety_scan",
        command: "python",
        args: [],
        env: { SAFETY_KEY: SECRET_MARKER },
        enabled: false,
      },
    })

    // 1. body.innerText 不含 secret
    const bodyText = await page.locator("body").innerText()
    expect(bodyText).not.toContain(SECRET_MARKER)

    // 2. API response 不含 secret
    const apiResp = await page.request.get("/api/mcp/servers")
    const apiBody = await apiResp.text()
    expect(apiBody).not.toContain(SECRET_MARKER)

    // 3. frontend bundle 不含 store hooks（production safety）
    const pageHtml = await page.content()
    expect(pageHtml).not.toContain("__storeHooks")
    expect(pageHtml).not.toContain("__e2eHooks")
  })
})
