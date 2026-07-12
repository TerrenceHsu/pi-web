import { defineConfig, devices } from "@playwright/test"
import { fileURLToPath } from "node:url"
import path from "node:path"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

/**
 * Playwright config —— Web Claude P0 MVP E2E smoke。
 *
 * - 仅 Chromium
 * - webServer 启动 Python 后端（FakeClient，无真实 GLM / MCP）
 * - 默认 baseURL http://127.0.0.1:8000
 * - trace/screenshot/video retain-on-failure
 *
 * 首次运行前需要安装浏览器：
 *   cd tests/e2e && npm install && npx playwright install chromium
 *
 * 前端必须先 build（webServer 启动的 Python 后端托管 ../static/）：
 *   cd src/pi_agent_core_py/web/frontend && npm install && npm run build
 */
export default defineConfig({
  testDir: ".",
  testMatch: ["*.spec.ts"],
  fullyParallel: false, // 共享一个 webServer 进程 + 共享 in-memory skill registry
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 1,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  timeout: 30_000,
  expect: {
    timeout: 8_000,
  },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    // 默认 Windows 下用 conda pipy 环境的 python（CLAUDE.md 注明）；
    // 用户可通过 E2E_PYTHON 环境变量覆盖（如 Linux / macOS）。
    // P1-B3-4: start_test_web_app.py 默认启用 delayed FakeClient——让 async prompt
    // 测试能观察 draft 增长；旧 e2e 也跑 delayed（每 prompt ~400-750ms）。
    // 如需 fast FakeClient，设 PI_E2E_FAST=1。
    command: `${process.env.E2E_PYTHON ?? "D:/miniconda/envs/pipy/python.exe"} start_test_web_app.py`,
    cwd: __dirname,
    url: "http://127.0.0.1:8000/api/state",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
})
