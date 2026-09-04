import { createPinia, setActivePinia } from "pinia"
import { DOMWrapper, mount, shallowMount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { nextTick } from "vue"

const { aboutApi } = vi.hoisted(() => ({
  aboutApi: {
    getAboutLicenses: vi.fn(),
    downloadWorkerSource: vi.fn(),
  },
}))

vi.mock("../../src/api/about", () => aboutApi)

import AboutModal from "../../src/components/about/AboutModal.vue"
import SessionSidebar from "../../src/components/layout/SessionSidebar.vue"

const response = {
  application: {
    name: "pi-agent-core-py",
    version: "0.0.29",
    license_expression: "MIT",
  },
  components: [
    {
      component_id: "wiki-parser-worker",
      name: "pi Wiki Parser Worker",
      version: "0.0.29",
      license_expression: "MIT",
      runtime_ready: false,
      source_offer_available: true,
      source_offer_url: "/api/about/wiki-parser-worker/source-offer",
      source_archive_url: "/api/about/wiki-parser-worker/source",
      license_url: "/api/about/wiki-parser-worker/license",
      notices_url: "/api/about/wiki-parser-worker/notices",
      sbom_url: "/api/about/wiki-parser-worker/sbom",
      source_tree_sha256: "a".repeat(64),
    },
  ],
  legal_notice: "Separate licenses; no warranty.",
}

function bodyGet(selector: string): DOMWrapper<Element> {
  const element = document.body.querySelector(selector)
  if (!element) throw new Error(`not found: ${selector}`)
  return new DOMWrapper(element)
}

async function flushAll(): Promise<void> {
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ""
  aboutApi.getAboutLicenses.mockReset().mockResolvedValue(response)
  aboutApi.downloadWorkerSource.mockReset().mockResolvedValue(undefined)
})

describe("AboutModal", () => {
  it("shows the Worker and MinerU notice with a prominent source offer", async () => {
    mount(AboutModal, { props: { open: true } })
    await flushAll()

    expect(aboutApi.getAboutLicenses).toHaveBeenCalledTimes(1)
    expect(bodyGet('[data-testid="main-app-license"]').text()).toContain("MIT")
    const worker = bodyGet('[data-testid="worker-license"]')
    expect(worker.text()).toContain("MIT")
    expect(worker.text()).toContain("MinerU")
    expect(worker.text()).toContain("absolutely no warranty")
    expect(worker.text()).toContain("runtime not ready")
    expect(worker.text()).toContain("a".repeat(64))
  })

  it("downloads the exact Corresponding Source endpoint", async () => {
    mount(AboutModal, { props: { open: true } })
    await flushAll()
    await bodyGet('[data-testid="download-worker-source"]').trigger("click")
    await flushAll()

    expect(aboutApi.downloadWorkerSource).toHaveBeenCalledWith(
      "/api/about/wiki-parser-worker/source",
      "wiki-parser-worker-0.0.29-source.tar.gz",
    )
  })

  it("fails closed when this deployment has no source archive", async () => {
    aboutApi.getAboutLicenses.mockResolvedValue({
      ...response,
      components: [{ ...response.components[0], source_offer_available: false }],
    })
    mount(AboutModal, { props: { open: true } })
    await flushAll()

    const button = bodyGet('[data-testid="download-worker-source"]').element as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(bodyGet('[role="status"]').text()).toContain("must remain unavailable")
  })

  it("surfaces a bounded load failure", async () => {
    aboutApi.getAboutLicenses.mockRejectedValue(new Error("safe load failure"))
    mount(AboutModal, { props: { open: true } })
    await flushAll()

    expect(bodyGet('[data-testid="about-error"]').text()).toContain("safe load failure")
    expect(document.body.textContent).not.toContain("MinerU runtime")
  })
})

describe("SessionSidebar About entry", () => {
  it("is always visible and opens the legal/source surface", async () => {
    const wrapper = shallowMount(SessionSidebar, {
      global: {
        stubs: {
          AboutModal: {
            props: ["open"],
            template: '<div v-if="open" data-testid="about-modal-stub"></div>',
          },
        },
      },
    })
    const entry = wrapper.get('[data-testid="about-source-button"]')
    expect((entry.element as HTMLButtonElement).disabled).toBe(false)
    await entry.trigger("click")
    await nextTick()
    expect(wrapper.find('[data-testid="about-modal-stub"]').exists()).toBe(true)
  })
})
