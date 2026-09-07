import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { api, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    status: number
    detail: string
    payload: unknown = null
    constructor(status: number, detail: string) {
      super(detail)
      this.status = status
      this.detail = detail
    }
  }
  return {
    api: { getAuthSession: vi.fn(), login: vi.fn(), logout: vi.fn(), changePassword: vi.fn() },
    FakeApiError,
  }
})

vi.mock("../../src/api/auth", () => api)
vi.mock("../../src/api/client", () => ({ ApiError: FakeApiError }))

import { useAuthStore } from "../../src/stores/authStore"

describe("auth store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it("restores an authenticated session", async () => {
    api.getAuthSession.mockResolvedValue({
      authenticated: true,
      user: { id: "usr_1", name: "admin" },
    })
    const store = useAuthStore()
    await store.restoreSession()
    expect(store.authenticated).toBe(true)
    expect(store.user?.name).toBe("admin")
  })

  it("treats a 401 restore as anonymous without an error banner", async () => {
    api.getAuthSession.mockRejectedValue(new FakeApiError(401, "Login required."))
    const store = useAuthStore()
    await store.restoreSession()
    expect(store.status).toBe("anonymous")
    expect(store.user).toBeNull()
    expect(store.error).toBeNull()
  })

  it("keeps password material out of Pinia state", async () => {
    const marker = "PASSWORD_MARKER"
    api.login.mockResolvedValue({
      authenticated: true,
      user: { id: "usr_1", name: "admin" },
    })
    const store = useAuthStore()
    expect(await store.login("admin", marker)).toBe(true)
    expect(api.login).toHaveBeenCalledWith("admin", marker)
    expect(JSON.stringify(store.$state)).not.toContain(marker)
  })

  it("shows a safe login failure and stays anonymous", async () => {
    api.login.mockRejectedValue(new FakeApiError(401, "Invalid username or password."))
    const store = useAuthStore()
    expect(await store.login("admin", "bad-password")).toBe(false)
    expect(store.authenticated).toBe(false)
    expect(store.error).toBe("Invalid username or password.")
  })

  it("clears the local identity after logout", async () => {
    api.login.mockResolvedValue({
      authenticated: true,
      user: { id: "usr_1", name: "admin" },
    })
    api.logout.mockResolvedValue({ authenticated: false })
    const store = useAuthStore()
    await store.login("admin", "123456")
    await store.logout()
    expect(store.status).toBe("anonymous")
    expect(store.user).toBeNull()
  })

  it("revokes local identity after password change without storing either secret", async () => {
    const store = useAuthStore()
    store.user = { id: "usr_1", name: "admin", is_admin: true }
    store.status = "authenticated"
    api.changePassword.mockResolvedValue({ authenticated: false })
    expect(await store.changePassword("OLD_SECRET", "NEW_SECRET_123")).toBe(true)
    expect(store.user).toBeNull()
    expect(store.authenticated).toBe(false)
    expect(JSON.stringify(store.$state)).not.toContain("SECRET")
  })

  it("keeps a valid login on rejected password change but clears expired login", async () => {
    const store = useAuthStore()
    store.user = { id: "usr_1", name: "admin", is_admin: true }
    store.status = "authenticated"
    api.changePassword.mockRejectedValue(new FakeApiError(400, "Change rejected."))
    expect(await store.changePassword("wrong", "NEW_SECRET_123")).toBe(false)
    expect(store.authenticated).toBe(true)
    api.changePassword.mockRejectedValue(new FakeApiError(401, "Login required."))
    expect(await store.changePassword("old", "NEW_SECRET_123")).toBe(false)
    expect(store.authenticated).toBe(false)
    expect(store.submitting).toBe(false)
  })
})
