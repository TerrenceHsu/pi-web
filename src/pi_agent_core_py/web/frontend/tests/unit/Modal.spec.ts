import { afterEach, describe, expect, it, vi } from "vitest"
import { mount } from "@vue/test-utils"

import Modal from "../../src/components/common/Modal.vue"

describe("Modal", () => {
  afterEach(() => {
    document.body.innerHTML = ""
    vi.restoreAllMocks()
  })

  it("forwards fallthrough attributes to the teleported dialog", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined)
    const wrapper = mount(Modal, {
      props: { open: true, title: "Forwarded attributes" },
      attrs: {
        "aria-label": "Forwarded modal",
        "data-testid": "forwarded-modal",
      },
    })

    const overlay = document.body.querySelector('[data-testid="forwarded-modal"]')
    const dialog = document.body.querySelector('[data-testid="modal"]')
    expect(overlay).not.toBeNull()
    expect(dialog).not.toBeNull()
    expect(dialog?.getAttribute("aria-label")).toBe("Forwarded modal")
    expect(warn.mock.calls.flat().join(" ")).not.toContain(
      "Extraneous non-props attributes",
    )

    wrapper.unmount()
  })
})
