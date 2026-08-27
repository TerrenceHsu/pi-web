import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  createWikiEntryPageProposal,
  createWikiSourceChangeSet,
  createWikiSourceSummary,
  createWikiTopicPageProposals,
  createWikiSpace,
  listWikiArtifacts,
  searchWikiPages,
  uploadWikiSource,
} from "../../src/api/wiki"
import { UI_HEADER_NAME, UI_HEADER_VALUE } from "../../src/api/client"

const fetchMock = vi.fn()

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal("fetch", fetchMock)
})

afterEach(() => vi.unstubAllGlobals())

describe("Wiki API", () => {
  it("creates a Space through the protected /api/wiki contract", async () => {
    fetchMock.mockResolvedValue(response({ id: "space_1", name: "Docs" }, 201))

    await createWikiSpace("Docs", "Product documentation")

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("/api/wiki/spaces")
    expect(init.method).toBe("POST")
    expect(JSON.parse(init.body)).toEqual({
      name: "Docs",
      description: "Product documentation",
    })
    expect(new Headers(init.headers).get(UI_HEADER_NAME)).toBe(UI_HEADER_VALUE)
  })

  it("uploads the original file and parse mode as multipart fields", async () => {
    fetchMock.mockResolvedValue(response({ source: { id: "source_1" }, parse_queued: true }, 201))
    const file = new File(["<h1>Guide</h1>"], "guide.html", { type: "text/html" })

    await uploadWikiSource("space_1", file, "accurate")

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("/api/wiki/spaces/space_1/sources")
    expect(init.body).toBeInstanceOf(FormData)
    const form = init.body as FormData
    expect((form.get("file") as File).name).toBe("guide.html")
    expect(form.get("parse_mode")).toBe("accurate")
    expect(new Headers(init.headers).has("content-type")).toBe(false)
  })

  it("binds FTS and artifact revision filters as encoded query parameters", async () => {
    fetchMock.mockResolvedValueOnce(response([])).mockResolvedValueOnce(response([]))

    await searchWikiPages("space_1", "tables & formulas", 12)
    await listWikiArtifacts("source_1", "parse_revision_1")

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/wiki/spaces/space_1/search?q=tables+%26+formulas&limit=12",
    )
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/wiki/sources/source_1/artifacts?parse_revision_id=parse_revision_1",
    )
  })

  it("maps the Source-to-Change-Set workflow to four explicit write endpoints", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ id: "summary_1" }, 201))
      .mockResolvedValueOnce(response({ id: "proposal_entry" }, 201))
      .mockResolvedValueOnce(response([], 201))
      .mockResolvedValueOnce(response({ change_set: { id: "change_set_1" }, items: [] }, 201))

    await createWikiSourceSummary("source_1")
    await createWikiEntryPageProposal("summary_1")
    await createWikiTopicPageProposals("summary_1")
    await createWikiSourceChangeSet("summary_1")

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/wiki/sources/source_1/summaries",
      "/api/wiki/summaries/summary_1/entry-page-proposal",
      "/api/wiki/summaries/summary_1/topic-page-proposals",
      "/api/wiki/summaries/summary_1/change-set",
    ])
    expect(fetchMock.mock.calls.map(([, init]) => init.method)).toEqual([
      "POST",
      "POST",
      "POST",
      "POST",
    ])
  })
})
