import { beforeEach, describe, expect, it, vi } from "vitest"

const { requestJsonMock } = vi.hoisted(() => ({
  requestJsonMock: vi.fn(),
}))

vi.mock("../../src/api/client", () => ({
  requestJson: requestJsonMock,
}))

import {
  branchSession,
  forkSession,
  getSessionTree,
  setActiveLane,
  setEntryLabel,
} from "../../src/api/sessions"

describe("Session tree API client", () => {
  beforeEach(() => requestJsonMock.mockReset())

  it("encodes tree lane and include_all query", async () => {
    requestJsonMock.mockResolvedValue({ entries: [] })
    await getSessionTree("session / one", { lane: "review / 1", includeAll: true })
    expect(requestJsonMock).toHaveBeenCalledWith(
      "/api/sessions/session%20%2F%20one/tree?lane=review+%2F+1&include_all=true",
    )
  })

  it("posts fork payload without inventing an at_entry_id", async () => {
    const payload = { name: "alternate", activate: true }
    await forkSession("s1", payload)
    expect(requestJsonMock).toHaveBeenCalledWith("/api/sessions/s1/fork", {
      method: "POST",
      body: payload,
    })
  })

  it("posts branch including explicit root null", async () => {
    await branchSession("s1", { entry_id: null, lane: "main" })
    expect(requestJsonMock).toHaveBeenCalledWith("/api/sessions/s1/branch", {
      method: "POST",
      body: { entry_id: null, lane: "main" },
    })
  })

  it("patches active lane", async () => {
    await setActiveLane("s1", "review")
    expect(requestJsonMock).toHaveBeenCalledWith("/api/sessions/s1/active-lane", {
      method: "PATCH",
      body: { lane: "review" },
    })
  })

  it("encodes entry id and preserves null label", async () => {
    await setEntryLabel("s1", "entry / 1", null)
    expect(requestJsonMock).toHaveBeenCalledWith(
      "/api/sessions/s1/entries/entry%20%2F%201/label",
      { method: "PUT", body: { label: null } },
    )
  })
})
