import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"

const { authApi } = vi.hoisted(() => ({
  authApi: { getAuthSession: vi.fn(), login: vi.fn(), logout: vi.fn() },
}))

vi.mock("../../src/api/auth", () => authApi)

import LoginPage from "../../src/components/auth/LoginPage.vue"

describe("LoginPage", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it("renders username and current-password inputs", () => {
    const wrapper = mount(LoginPage)
    const username = wrapper.get('[data-testid="login-name"]').element as HTMLInputElement
    const password = wrapper.get('[data-testid="login-password"]').element as HTMLInputElement
    expect(username.autocomplete).toBe("username")
    expect(password.type).toBe("password")
    expect(password.autocomplete).toBe("current-password")
  })

  it("submits credentials and immediately clears the password field", async () => {
    authApi.login.mockResolvedValue({
      authenticated: true,
      user: { id: "usr_1", name: "admin" },
    })
    const wrapper = mount(LoginPage)
    await wrapper.get('[data-testid="login-name"]').setValue("admin")
    await wrapper.get('[data-testid="login-password"]').setValue("123456")
    await wrapper.get('[data-testid="login-form"]').trigger("submit")
    await flushPromises()
    expect(authApi.login).toHaveBeenCalledWith("admin", "123456")
    const password = wrapper.get('[data-testid="login-password"]').element as HTMLInputElement
    expect(password.value).toBe("")
  })

  it("disables submit until both fields are present", async () => {
    const wrapper = mount(LoginPage)
    const submit = wrapper.get('[data-testid="login-submit"]').element as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    await wrapper.get('[data-testid="login-name"]').setValue("admin")
    expect(submit.disabled).toBe(true)
    await wrapper.get('[data-testid="login-password"]').setValue("123456")
    expect(submit.disabled).toBe(false)
  })
})
