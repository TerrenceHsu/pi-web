import { beforeEach, describe, expect, it, vi } from "vitest"

const { requestJsonMock } = vi.hoisted(() => ({ requestJsonMock: vi.fn() }))

vi.mock("../../src/api/client", () => ({ requestJson: requestJsonMock }))

import { changePassword, getAuthSession, login, logout } from "../../src/api/auth"

describe("auth API", () => {
  beforeEach(() => requestJsonMock.mockReset())

  it("loads the current HttpOnly-cookie session", async () => {
    requestJsonMock.mockResolvedValue({ authenticated: true, user: { id: "u1", name: "admin" } })
    await getAuthSession()
    expect(requestJsonMock).toHaveBeenCalledWith("/api/auth/session")
  })

  it("posts credentials only to the login endpoint", async () => {
    requestJsonMock.mockResolvedValue({ authenticated: true, user: { id: "u1", name: "admin" } })
    await login("admin", "123456")
    expect(requestJsonMock).toHaveBeenCalledWith("/api/auth/login", {
      method: "POST",
      body: { name: "admin", password: "123456" },
    })
  })

  it("posts logout without exposing a token", async () => {
    requestJsonMock.mockResolvedValue({ authenticated: false })
    await logout()
    expect(requestJsonMock).toHaveBeenCalledWith("/api/auth/logout", { method: "POST" })
  })

  it("posts both passwords only in the password endpoint body", async () => {
    await changePassword("old-secret", "new-secret-123")
    expect(requestJsonMock).toHaveBeenCalledWith("/api/auth/password", {
      method: "POST", body: { current_password: "old-secret", new_password: "new-secret-123" },
    })
  })
})
