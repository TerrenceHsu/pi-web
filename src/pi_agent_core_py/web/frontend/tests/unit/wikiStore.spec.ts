import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

const wikiApi = vi.hoisted(() => ({
  listWikiSpaces: vi.fn(),
  createWikiSpace: vi.fn(),
  listWikiSources: vi.fn(),
  uploadWikiSource: vi.fn(),
  enqueueWikiParse: vi.fn(),
  listWikiArtifacts: vi.fn(),
  listWikiParseRevisions: vi.fn(),
  listWikiSourceSummaries: vi.fn(),
  createWikiSourceSummary: vi.fn(),
  listWikiPageProposals: vi.fn(),
  createWikiEntryPageProposal: vi.fn(),
  createWikiTopicPageProposals: vi.fn(),
  createWikiSourceChangeSet: vi.fn(),
  listWikiChangeSets: vi.fn(),
  readWikiArtifact: vi.fn(),
}))

vi.mock("../../src/api/wiki", () => wikiApi)

import { useWikiStore } from "../../src/stores/wikiStore"
import type { WikiSource, WikiSpace } from "../../src/types/wiki"

const space = (id: string, name: string): WikiSpace => ({
  id,
  name,
  description: "",
  status: "active",
  graph_revision: 0,
  created_at_ms: 1,
  updated_at_ms: 1,
})

const source = (id: string, spaceId: string): WikiSource => ({
  id,
  space_id: spaceId,
  display_name: `${id}.html`,
  mime_type: "text/html",
  size_bytes: 20,
  source_sha256: "a".repeat(64),
  source_relpath: `raw/${id}/source.html`,
  selected_parse_revision_id: null,
  selection_version: 0,
  selected_at_ms: null,
  status: "uploaded",
  safe_error_code: "",
  created_at_ms: 1,
  updated_at_ms: 1,
})

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  wikiApi.listWikiArtifacts.mockResolvedValue([])
  wikiApi.listWikiParseRevisions.mockResolvedValue([])
})

describe("Wiki store", () => {
  it("loads Spaces and selects only the chosen Space's Sources", async () => {
    wikiApi.listWikiSpaces.mockResolvedValue([space("space_a", "A"), space("space_b", "B")])
    wikiApi.listWikiSources
      .mockResolvedValueOnce([source("source_a", "space_a")])
      .mockResolvedValueOnce([source("source_b", "space_b")])
    const store = useWikiStore()

    await store.loadSpaces()
    expect(store.selectedSpaceId).toBe("space_a")
    expect(store.sources.map((item) => item.id)).toEqual(["source_a"])

    await store.selectSpace("space_b")
    expect(store.selectedSpaceId).toBe("space_b")
    expect(store.sources.map((item) => item.id)).toEqual(["source_b"])
    expect(wikiApi.listWikiSources).toHaveBeenLastCalledWith("space_b")
  })

  it("keeps an uploaded Source scoped to the active Space and loads its Raw bundle", async () => {
    const createdSpace = space("space_a", "A")
    const uploaded = source("source_new", createdSpace.id)
    wikiApi.listWikiSpaces.mockResolvedValue([createdSpace])
    wikiApi.listWikiSources.mockResolvedValue([])
    wikiApi.uploadWikiSource.mockResolvedValue({ source: uploaded, parse_queued: true })
    const store = useWikiStore()
    await store.loadSpaces()

    const result = await store.uploadSource(
      new File(["<h1>A</h1>"], "a.html", { type: "text/html" }),
      "pipeline",
    )

    expect(result).toEqual(uploaded)
    expect(wikiApi.uploadWikiSource).toHaveBeenCalledWith(
      "space_a",
      expect.any(File),
      "pipeline",
    )
    expect(store.selectedSourceId).toBe(uploaded.id)
    expect(wikiApi.listWikiArtifacts).toHaveBeenCalledWith(uploaded.id)
    expect(wikiApi.listWikiParseRevisions).toHaveBeenCalledWith(uploaded.id)
  })

  it("renders Raw Markdown as text and revokes image object URLs on reset", async () => {
    const createObjectURL = vi.fn(() => "blob:wiki-image")
    const revokeObjectURL = vi.fn()
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL })
    wikiApi.readWikiArtifact
      .mockResolvedValueOnce({
        blob: { text: vi.fn().mockResolvedValue("# Parsed") },
        filename: "parsed.md",
      })
      .mockResolvedValueOnce({ blob: new Blob(["png"]), filename: "image.png" })
    const store = useWikiStore()

    await store.previewArtifact({
      id: "artifact_md",
      source_id: "source_a",
      parse_revision_id: "parse_revision_a",
      kind: "parsed_markdown",
      relpath: "parsed.md",
      mime_type: "text/markdown",
      size_bytes: 8,
      sha256: "a".repeat(64),
      width: null,
      height: null,
      source_locator_json: "{}",
      created_at_ms: 1,
    })
    expect(store.artifactPreview?.text).toBe("# Parsed")

    await store.previewArtifact({
      id: "artifact_image",
      source_id: "source_a",
      parse_revision_id: "parse_revision_a",
      kind: "embedded_image",
      relpath: "images/image.png",
      mime_type: "image/png",
      size_bytes: 3,
      sha256: "b".repeat(64),
      width: 10,
      height: 10,
      source_locator_json: "{}",
      created_at_ms: 1,
    })
    expect(store.artifactPreview?.objectUrl).toBe("blob:wiki-image")

    store.reset()
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:wiki-image")
    vi.unstubAllGlobals()
  })

  it("resumes and completes the ordered Source-to-Change-Set workflow", async () => {
    const parsedSource: WikiSource = {
      ...source("source_a", "space_a"),
      status: "parsed",
      selected_parse_revision_id: "parse_revision_a",
      selection_version: 2,
      selected_at_ms: 2,
    }
    const summary = {
      id: "summary_a",
      space_id: "space_a",
      source_id: parsedSource.id,
      parse_revision_id: "parse_revision_a",
      job_id: "job_summary_a",
      selection_version: 2,
      source_sha256: "a".repeat(64),
      parsed_markdown_sha256: "b".repeat(64),
      manifest_sha256: "c".repeat(64),
      page_count: 2,
      prompt_revision: "wiki-summary-v1",
      provider: "fake",
      model: "fake-model",
      content: {
        suggested_title: "Guide",
        overview: "Overview",
        key_points: [{ text: "Point", page_numbers: [1] }],
        topics: [],
        caveats: [],
      },
      content_sha256: "d".repeat(64),
      created_at_ms: 3,
    }
    const entry = {
      id: "proposal_entry_a",
      space_id: "space_a",
      source_id: parsedSource.id,
      summary_id: summary.id,
      job_id: "job_entry_a",
      kind: "entry" as const,
      topic_ordinal: null,
      parent_proposal_id: null,
      title: "Guide",
      slug: "guide",
      aliases: [],
      markdown: "# Guide",
      content_sha256: "e".repeat(64),
      source_locator_json: "{}",
      created_at_ms: 4,
    }
    const changeSet = {
      id: "change_set_a",
      space_id: "space_a",
      conversation_id: null,
      source_summary_id: summary.id,
      status: "awaiting_approval" as const,
      base_graph_revision: 0,
      summary: "Create Guide",
      safe_error_code: "",
      created_at_ms: 5,
      decided_at_ms: null,
      published_at_ms: null,
    }
    wikiApi.listWikiSpaces.mockResolvedValue([space("space_a", "A")])
    wikiApi.listWikiSources.mockResolvedValue([parsedSource])
    wikiApi.listWikiSourceSummaries.mockResolvedValue([])
    wikiApi.createWikiSourceSummary.mockResolvedValue(summary)
    wikiApi.listWikiPageProposals.mockResolvedValue([])
    wikiApi.createWikiEntryPageProposal.mockResolvedValue(entry)
    wikiApi.createWikiTopicPageProposals.mockResolvedValue([])
    wikiApi.createWikiSourceChangeSet.mockResolvedValue({ change_set: changeSet, items: [] })
    wikiApi.listWikiChangeSets.mockResolvedValue([changeSet])
    const store = useWikiStore()
    await store.loadSpaces()

    expect(await store.prepareSourceChangeSet(parsedSource.id)).toBe(true)

    const calls = [
      wikiApi.createWikiSourceSummary,
      wikiApi.createWikiEntryPageProposal,
      wikiApi.createWikiTopicPageProposals,
      wikiApi.createWikiSourceChangeSet,
    ].map((mock) => mock.mock.invocationCallOrder[0])
    expect(calls).toEqual([...calls].sort((left, right) => left - right))
    expect(store.activeTab).toBe("changes")
    expect(store.selectedChangeSetId).toBe(changeSet.id)
    expect(store.sourceWorkflowStage).toBeNull()
  })
})
