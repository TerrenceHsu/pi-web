import { createPinia, setActivePinia } from "pinia"
import { shallowMount } from "@vue/test-utils"
import { beforeEach, describe, expect, it } from "vitest"

import SessionSidebar from "../../src/components/layout/SessionSidebar.vue"
import { useAuthStore } from "../../src/stores/authStore"

beforeEach(() => {
  setActivePinia(createPinia())
})

describe("administrator Telemetry navigation", () => {
  it("shows the entry only for an administrator and emits navigation", async () => {
    const auth = useAuthStore()
    auth.status = "authenticated"
    auth.user = { id: "admin-1", name: "admin", is_admin: true }
    const admin = shallowMount(SessionSidebar)

    const button = admin.get('[data-testid="telemetry-button"]')
    expect(button.text()).toContain("Admin")
    await button.trigger("click")
    expect(admin.emitted("open-telemetry")).toEqual([[]])
    admin.unmount()

    auth.user = { id: "user-1", name: "alice", is_admin: false }
    const user = shallowMount(SessionSidebar)
    expect(user.find('[data-testid="telemetry-button"]').exists()).toBe(false)
    user.unmount()
  })
})
