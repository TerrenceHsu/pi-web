import { createPinia, setActivePinia } from "pinia"
import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, expect, it, vi } from "vitest"
import ChangePasswordModal from "../../src/components/auth/ChangePasswordModal.vue"
import { useAuthStore } from "../../src/stores/authStore"

beforeEach(() => setActivePinia(createPinia()))

it("rejects mismatched confirmation, submits matched secrets, and clears on close", async () => {
  const store = useAuthStore()
  const change = vi.spyOn(store, "changePassword").mockResolvedValue(false)
  const wrapper = mount(ChangePasswordModal, {
    props: { open: true }, global: { stubs: { teleport: true } },
  })
  const fields = wrapper.findAll("input")
  await fields[0]!.setValue("old-password")
  await fields[1]!.setValue("new-password-123")
  await fields[2]!.setValue("wrong-confirmation")
  await wrapper.get("form").trigger("submit")
  expect(change).not.toHaveBeenCalled()
  expect(wrapper.get('[role="alert"]').text()).toContain("do not match")
  await fields[2]!.setValue("new-password-123")
  await wrapper.get("form").trigger("submit")
  await flushPromises()
  expect(change).toHaveBeenCalledWith("old-password", "new-password-123")
  expect(wrapper.findAll("input").map(field => (field.element as HTMLInputElement).value))
    .toEqual(["", "", ""])
  await wrapper.get("input").setValue("must-not-survive-close")
  await wrapper.setProps({ open: false })
  await wrapper.setProps({ open: true })
  expect((wrapper.get("input").element as HTMLInputElement).value).toBe("")
  wrapper.unmount()
})
