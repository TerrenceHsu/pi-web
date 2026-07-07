import { test, expect, type Page } from "@playwright/test"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

/**
 * Web Claude P0 MVP —— 真实浏览器 smoke。
 *
 * 覆盖关键用户路径，**不依赖**真实 GLM / 真实 MCP server：
 *   - Test 1：两栏 UI 没退回调试器形态
 *   - Test 2：新建 session + 发普通消息
 *   - Test 3：上传 md 显示 FileChip，发送后仍展示
 *   - Test 4：图片显示 unsupported
 *   - Test 5：Skills/MCP Modal 能打开 + MCP env value 不回显到页面文本
 *   - Test 6（可选）：上传 SKILL.md + Use this turn → SkillUsedCard
 *
 * 后端由 webServer.command 启动的 start_test_web_app.py 提供，使用 FakeClient。
 */
const FIXTURES = path.resolve(__dirname, "fixtures")

async function dismissModalIfPresent(page: Page) {
  // ESC 关闭——Modal.vue 监听 keydown ESC，比点 close 按钮更稳
  await page.keyboard.press("Escape")
  await expect(page.locator('[data-testid="modal"]')).toHaveCount(0, { timeout: 3_000 })
}

// ============================================================================
// Test 1：页面打开 + 两栏 UI 没退回调试器
// ============================================================================
test.describe("Smoke 1: layout", () => {
  test("两栏 UI（sidebar + chat），无右栏 / Drawer / Trace Viewer 文案", async ({
    page,
  }) => {
    await page.goto("/")

    // 主体结构
    await expect(page.locator('[data-testid="session-sidebar"]')).toBeVisible()
    await expect(page.locator('[data-testid="chat-panel"]')).toBeVisible()
    await expect(page.locator('[data-testid="new-chat-button"]')).toBeVisible()
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible()

    // Skills / MCP footer 按钮存在
    await expect(page.locator('[data-testid="skills-button"]')).toBeVisible()
    await expect(page.locator('[data-testid="mcp-button"]')).toBeVisible()

    // 没有退回调试器形态——这些旧组件文本不应出现
    const bodyText = await page.locator("body").innerText()
    expect(bodyText).not.toContain("Trace Viewer")
    expect(bodyText).not.toContain("DeveloperDrawer")
    expect(bodyText).not.toContain("Raw JSON")
    expect(bodyText).not.toContain("Event Stream")
    expect(bodyText).not.toContain("Policy Audit")
  })
})

// ============================================================================
// Test 2：新建 session + 发送普通消息
// ============================================================================
test.describe("Smoke 2: chat", () => {
  test("New chat → 输入 → Send → 看到 user + assistant message", async ({
    page,
  }) => {
    await page.goto("/")

    // 等聊天 UI 就绪
    await expect(page.locator('[data-testid="chat-input-field"]')).toBeVisible()

    // 输入 + 发送
    await page.locator('[data-testid="chat-input-field"]').fill("hello from playwright")
    await page.locator('[data-testid="send-button"]').click()

    // user message 立刻可见（乐观 push）
    await expect(
      page.locator('[data-testid="user-message"]').filter({
        hasText: "hello from playwright",
      }),
    ).toBeVisible()

    // assistant message（FakeClient deterministic 文本）应在合理时间内出现
    // 同步阻塞 POST——等待 backend 返回
    await expect(
      page.locator('[data-testid="assistant-message"]').filter({
        hasText: "hello from fake backend",
      }),
    ).toBeVisible({ timeout: 20_000 })

    // 没有全屏错误
    await expect(page.locator('[data-testid="error-card"]')).toHaveCount(0)
  })
})

// ============================================================================
// Test 3：上传 md → FileChip → Send 后保留
// ============================================================================
test.describe("Smoke 3: file upload", () => {
  test("上传 md 显示 supported FileChip，发送后 user 消息仍带 chip", async ({
    page,
  }) => {
    await page.goto("/")
    await expect(page.locator('[data-testid="chat-input-field"]')).toBeVisible()

    // 用 file-input 直接 setInputFiles（绕过 popup）
    const fileInput = page.locator('[data-testid="file-input"]')
    await fileInput.setInputFiles(path.join(FIXTURES, "sample.md"))

    // pending AttachmentBar 出现 chip
    await expect(page.locator('[data-testid="attachment-bar"] [data-testid="file-chip"]')).toHaveCount(1)
    await expect(page.locator('[data-testid="attachment-bar"]')).toContainText(/sample\.md/)

    // 发送——后端会保存附件 + 注入 FileBlock
    await page.locator('[data-testid="chat-input-field"]').fill("read this file")
    await page.locator('[data-testid="send-button"]').click()

    // user message 出现且包含 chip（user 消息内 FileChip 不可删除但可见）
    await expect(
      page.locator('[data-testid="user-message"]').filter({ hasText: "read this file" }),
    ).toBeVisible()
    await expect(
      page.locator('[data-testid="user-message"] [data-testid="file-chip"]'),
    ).toHaveCount(1)

    // pending AttachmentBar 应清空
    await expect(page.locator('[data-testid="attachment-bar"]')).toHaveCount(0)
  })
})

// ============================================================================
// Test 4：图片 → unsupported
// ============================================================================
test.describe("Smoke 4: image unsupported", () => {
  test("上传 png 显示 unsupported FileChip", async ({ page }) => {
    await page.goto("/")
    await expect(page.locator('[data-testid="chat-input-field"]')).toBeVisible()

    await page
      .locator('[data-testid="file-input"]')
      .setInputFiles(path.join(FIXTURES, "sample.png"))

    await expect(page.locator('[data-testid="attachment-bar"] [data-testid="file-chip"]')).toHaveCount(1)
    // label 出现 unsupported 文案（FileChip 渲染 fileSupportLabel('image_unsupported') → "unsupported"）
    await expect(page.locator('[data-testid="attachment-bar"]')).toContainText(/unsupported/i)
  })
})

// ============================================================================
// Test 5：Skills / MCP Modal + env value 不回显
// ============================================================================
test.describe("Smoke 5: modals + env value non-echo", () => {
  test("Skills modal 打开 + 关闭；MCP modal 打开 + 填表 + env value 不出现在 body 文本", async ({
    page,
  }) => {
    await page.goto("/")

    // --- Skills modal ---
    await page.locator('[data-testid="skills-button"]').click()
    await expect(page.locator('[data-testid="modal"]')).toBeVisible()
    // 标题 Skills 可见
    await expect(page.locator('[data-testid="modal"]')).toContainText("Skills")
    // upload input 存在
    await expect(page.locator('[data-testid="skill-upload-input"]')).toBeVisible()
    await dismissModalIfPresent(page)
    await expect(page.locator('[data-testid="modal"]')).toHaveCount(0)

    // --- MCP modal ---
    await page.locator('[data-testid="mcp-button"]').click()
    await expect(page.locator('[data-testid="modal"]')).toBeVisible()
    await expect(page.locator('[data-testid="modal"]')).toContainText("MCP Servers")
    await expect(page.locator('[data-testid="mcp-server-form"]')).toBeVisible()

    const SECRET_VALUE = "super-secret-value-zzz-12345"

    // 填 name + command（必填）
    await page.locator('[data-testid="mcp-server-name-input"]').fill("e2e-fake")
    await page.locator('[data-testid="mcp-server-command-input"]').fill("python")

    // 填 env key + secret value（type=password）
    await page.locator('[data-testid="mcp-env-key-input"]').first().fill("SECRET_KEY")
    await page.locator('[data-testid="mcp-env-value-input"]').first().fill(SECRET_VALUE)

    // 关键断言 1：在提交前，secret value 不应作为页面可见文本出现
    // （password input 的 value 在 DOM 状态里但不应渲染到 innerText）
    const bodyTextBefore = await page.locator("body").innerText()
    expect(bodyTextBefore).not.toContain(SECRET_VALUE)

    // 提交——后端会 200（不 enabled）或返回 cfg，但响应类型不含 env value
    await page
      .locator('[data-testid="mcp-server-form"] button[type="submit"]')
      .click()

    // 关键断言 2：提交后，表单应清空（resetForm），且 secret value 仍不出现在 body 文本
    await expect(page.locator('[data-testid="mcp-server-name-input"]')).toHaveValue("")
    await expect(page.locator('[data-testid="mcp-env-key-input"]').first()).toHaveValue("")
    const bodyTextAfter = await page.locator("body").innerText()
    expect(bodyTextAfter).not.toContain(SECRET_VALUE)

    // 关键断言 3：server list 出现 e2e-fake，env_keys 显示 KEY 名（"SECRET_KEY"）
    // 但 value 永远不出现在页面文本里
    await expect(page.locator('[data-testid="modal"]')).toContainText("e2e-fake")
    await expect(page.locator('[data-testid="modal"]')).toContainText("SECRET_KEY")
    const finalBodyText = await page.locator("body").innerText()
    expect(finalBodyText).not.toContain(SECRET_VALUE)

    await dismissModalIfPresent(page)
  })
})

// ============================================================================
// Test 6（可选）：上传 SKILL.md + Use this turn
// ============================================================================
// 修复后稳定通过。关键：
//   - 用 page.check() 而不是 click()——专门为 checkbox 设计，正确触发 change 事件
//     （SkillList 的 @change.prevent 用了 preventDefault；click() 在某些情况下
//     不触发 change handler，check() 会）
//   - 用 waitForResponse 等 enable API 真的完成，再继续下一步
test.describe("Smoke 6 (optional): skill upload + use this turn", () => {
  // 注意：FakeClient 不会真去调用 skill prompt；但 skill 上传 + enable + selected
  // 的 UI 状态变化可以验证；SkillUsedCard 的预 push 由前端 sendPrompt 完成。
  test("上传 SKILL.md → enable → Use this turn → 发送 → SkillUsedCard 出现", async ({
    page,
  }) => {
    await page.goto("/")

    // 打开 Skills modal
    await page.locator('[data-testid="skills-button"]').click()
    await expect(page.locator('[data-testid="modal"]')).toBeVisible()

    // 上传 SKILL.md —— SkillUploadForm 选文件后需手动点 Upload SKILL.md 按钮
    await page
      .locator('[data-testid="skill-upload-input"]')
      .setInputFiles(path.join(FIXTURES, "SKILL.md"))

    // 等 upload 响应（确保 registry 真的注册了 skill）
    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes("/api/skills/upload") && r.request().method() === "POST",
        { timeout: 10_000 },
      ),
      page
        .locator('[data-testid="modal"] .skill-upload-form button.primary')
        .click(),
    ])

    // skill card 出现（至少 1 个）
    await expect(page.locator('[data-testid="skill-card"]')).toHaveCount(1, { timeout: 10_000 })

    // 上传后 skill 默认 status="enabled"（SkillRegistry.register 默认值）——
    // 所以 "Use this turn" checkbox 已经 enabled，不需要先点 Enabled toggle
    // 直接勾选 Use this turn（本地状态，无 API；点 label 触发 change）
    await page
      .locator('[data-testid="skill-card"] label.select-box')
      .first()
      .click()

    // 验证勾选成功——Use this turn checkbox checked
    await expect(
      page.locator('[data-testid="skill-card"] input[type="checkbox"]').nth(1),
    ).toBeChecked()

    await dismissModalIfPresent(page)

    await dismissModalIfPresent(page)

    // 发送 prompt
    await page.locator('[data-testid="chat-input-field"]').fill("use the skill")
    await page.locator('[data-testid="send-button"]').click()

    // SkillUsedCard 出现在中间消息流——chatStore.sendPrompt 预 push 一条
    // kind="skill_used" item，content 形如 "Skills: e2e_demo_skill"
    await expect(
      page.getByText("Skills: e2e_demo_skill"),
    ).toBeVisible({ timeout: 10_000 })
  })
})
