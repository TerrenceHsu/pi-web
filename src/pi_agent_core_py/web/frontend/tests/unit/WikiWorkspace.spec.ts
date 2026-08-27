import { flushPromises, mount } from "@vue/test-utils"
import { createPinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

const wikiApi = vi.hoisted(() => ({
  listWikiSpaces: vi.fn(),
  createWikiSpace: vi.fn(),
  listWikiSources: vi.fn(),
  uploadWikiSource: vi.fn(),
  enqueueWikiParse: vi.fn(),
  listWikiArtifacts: vi.fn(),
  listWikiParseRevisions: vi.fn(),
  readWikiArtifact: vi.fn(),
  listWikiPages: vi.fn(),
  searchWikiPages: vi.fn(),
  listWikiPageRevisions: vi.fn(),
  getWikiGraph: vi.fn(),
  listWikiChangeSets: vi.fn(),
  listWikiChangeSetItems: vi.fn(),
  decideWikiChangeSet: vi.fn(),
  listWikiConversations: vi.fn(),
  createWikiConversation: vi.fn(),
  setWikiConversationStatus: vi.fn(),
}))

vi.mock("../../src/api/wiki", () => wikiApi)

import WikiWorkspace from "../../src/components/wiki/WikiWorkspace.vue"

const space = {
  id: "space_000000000000000000000001",
  name: "Product Wiki",
  description: "Approved product knowledge",
  status: "active" as const,
  graph_revision: 4,
  created_at_ms: 1,
  updated_at_ms: 2,
}

const source = {
  id: "source_000000000000000000000001",
  space_id: space.id,
  display_name: "guide.html",
  mime_type: "text/html" as const,
  size_bytes: 100,
  source_sha256: "a".repeat(64),
  source_relpath: "raw/source/source.html",
  selected_parse_revision_id: "parse_revision_000000000000000000000001",
  selection_version: 1,
  selected_at_ms: 2,
  status: "parsed" as const,
  safe_error_code: "",
  created_at_ms: 1,
  updated_at_ms: 2,
}

beforeEach(() => {
  vi.clearAllMocks()
  wikiApi.listWikiSpaces.mockResolvedValue([space])
  wikiApi.listWikiSources.mockResolvedValue([source])
  wikiApi.listWikiArtifacts.mockResolvedValue([])
  wikiApi.listWikiParseRevisions.mockResolvedValue([])
  wikiApi.listWikiPages.mockResolvedValue([])
})

describe("WikiWorkspace", () => {
  it("renders an independent Space/Sources workspace and returns to chat", async () => {
    const wrapper = mount(WikiWorkspace, {
      global: { plugins: [createPinia()] },
    })
    await flushPromises()

    expect(wrapper.get("[data-testid='wiki-workspace']").exists()).toBe(true)
    expect(wrapper.get("[data-testid='wiki-space-row']").text()).toContain("Product Wiki")
    expect(wrapper.get("[data-testid='wiki-source-row']").text()).toContain("guide.html")

    await wrapper.get("[data-testid='wiki-tab-pages']").trigger("click")
    await flushPromises()
    expect(wrapper.get("[data-testid='wiki-pages-view']").text()).toContain("No approved pages")

    await wrapper.get("[data-testid='wiki-back-to-chat']").trigger("click")
    expect(wrapper.emitted("close")).toHaveLength(1)
  })

  it("shows Raw Markdown strictly as text rather than executable HTML", async () => {
    wikiApi.listWikiArtifacts.mockResolvedValue([
      {
        id: "artifact_000000000000000000000001",
        source_id: source.id,
        parse_revision_id: source.selected_parse_revision_id,
        kind: "parsed_markdown",
        relpath: "parsed.md",
        mime_type: "text/markdown",
        size_bytes: 30,
        sha256: "b".repeat(64),
        width: null,
        height: null,
        source_locator_json: "{}",
        created_at_ms: 2,
      },
    ])
    wikiApi.listWikiParseRevisions.mockResolvedValue([])
    wikiApi.readWikiArtifact.mockResolvedValue({
      blob: { text: vi.fn().mockResolvedValue("# Raw\n<script>alert(1)</script>") },
      filename: "parsed.md",
    })
    const wrapper = mount(WikiWorkspace, {
      global: { plugins: [createPinia()] },
    })
    await flushPromises()

    await wrapper.get("[data-testid='wiki-source-row']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='wiki-artifact-row']").trigger("click")
    await flushPromises()

    const preview = wrapper.get("[data-testid='wiki-artifact-preview']")
    expect(preview.text()).toContain("<script>alert(1)</script>")
    expect(preview.find("script").exists()).toBe(false)
  })
})
