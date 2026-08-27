import { describe, expect, it, vi } from "vitest"

import { KNOWLEDGE_ROUTE, pushKnowledgeRoute, readAppView } from "../../src/utils/appRoute"

describe("application Knowledge route", () => {
  it("distinguishes independent Knowledge paths from chat paths", () => {
    expect(readAppView("/knowledge")).toBe("knowledge")
    expect(readAppView("/knowledge/spaces/space_1")).toBe("knowledge")
    expect(readAppView("/chat/session_1")).toBe("chat")
    expect(readAppView("/")).toBe("chat")
  })

  it("pushes the stable /knowledge history entry", () => {
    const pushState = vi.spyOn(window.history, "pushState")
    window.history.replaceState({}, "", "/chat/session_1")

    pushKnowledgeRoute()

    expect(pushState).toHaveBeenCalledWith({ view: "knowledge" }, "", KNOWLEDGE_ROUTE)
  })
})
