import { beforeEach, describe, expect, it, vi } from "vitest"

const requestJson = vi.hoisted(() => vi.fn())
vi.mock("../../src/api/client", () => ({ requestJson }))

import {
  getContextCompaction,
  getContextSource,
  setContextAutoCompaction,
} from "../../src/api/contextBudget"

beforeEach(() => requestJson.mockReset())

describe("context management API", () => {
  it("uses the same session-scoped endpoint for status and persistent settings", async () => {
    await getContextCompaction("session/one")
    expect(requestJson).toHaveBeenLastCalledWith("/api/sessions/session%2Fone/context/compaction")
    await setContextAutoCompaction("session/one", false)
    expect(requestJson).toHaveBeenLastCalledWith(
      "/api/sessions/session%2Fone/context/compaction",
      { method: "PUT", body: { auto_compact: false } },
    )
  })

  it("requests a bounded source page with encoded session and entry IDs", async () => {
    await getContextSource("session/one", "entry?#1", 6000)
    expect(requestJson).toHaveBeenCalledWith(
      "/api/sessions/session%2Fone/context/source/entry%3F%231",
      { query: { offset: 6000, max_chars: 6000 } },
    )
  })
})
