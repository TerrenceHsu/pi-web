import { request, type FullConfig } from "@playwright/test"
import { mkdir } from "node:fs/promises"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const currentDir = dirname(fileURLToPath(import.meta.url))
export const ADMIN_AUTH_STATE = resolve(currentDir, ".auth/admin.json")

export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0]?.use?.baseURL ?? "http://127.0.0.1:8000"
  await mkdir(dirname(ADMIN_AUTH_STATE), { recursive: true })
  const context = await request.newContext({ baseURL })
  try {
    const response = await context.post("/api/auth/login", {
      headers: { "X-PI-Agent-UI": "1" },
      data: { name: "admin", password: "123456" },
    })
    if (!response.ok()) {
      throw new Error(`E2E login failed: ${response.status()} ${await response.text()}`)
    }
    await context.storageState({ path: ADMIN_AUTH_STATE })
  } finally {
    await context.dispose()
  }
}
