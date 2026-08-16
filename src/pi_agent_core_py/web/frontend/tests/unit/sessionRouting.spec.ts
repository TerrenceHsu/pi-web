import { beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"

const { sessionsApi, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    detail: string
    constructor(detail: string) {
      super(detail)
      this.detail = detail
    }
  }
  return {
    sessionsApi: {
      listSessions: vi.fn(),
      createSession: vi.fn(),
      renameSession: vi.fn(),
      deleteSession: vi.fn(),
    },
    FakeApiError,
  }
})

vi.mock("../../src/api/sessions", () => sessionsApi)
vi.mock("../../src/api/client", () => ({ ApiError: FakeApiError }))

import { useSessionStore } from "../../src/stores/sessionStore"
import {
  clearSessionRoute,
  pushSessionRoute,
  readSessionRoute,
  replaceSessionRoute,
  sessionRoutePath,
} from "../../src/utils/sessionRoute"

const sessions = [
  {
    id: "sess-A",
    title: "A",
    created_at: 1,
    updated_at: 1,
    is_current: true,
  },
  {
    id: "sess-B",
    title: "B",
    created_at: 2,
    updated_at: 2,
    is_current: false,
  },
]

describe("session URL routing", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    window.history.replaceState({}, "", "/")
    sessionsApi.listSessions.mockResolvedValue({ sessions })
  })

  it("round-trips a valid /chat/{session_id} route", () => {
    expect(sessionRoutePath("sess-A")).toBe("/chat/sess-A")
    replaceSessionRoute("sess-A")
    expect(window.location.pathname).toBe("/chat/sess-A")
    expect(readSessionRoute()).toEqual({ requested: true, sessionId: "sess-A" })
    pushSessionRoute("sess-B")
    expect(window.location.pathname).toBe("/chat/sess-B")
    clearSessionRoute()
    expect(window.location.pathname).toBe("/")
  })

  it("rejects malformed route segments without decoding them into an API id", () => {
    for (const path of ["/chat/", "/chat/a/b", "/chat/%2Fetc", "/other/sess-A"]) {
      window.history.replaceState({}, "", path)
      expect(readSessionRoute()).toEqual({ requested: true, sessionId: null })
    }
  })

  it("prefers an owned URL session over the backend current session", async () => {
    const store = useSessionStore()
    await store.loadSessions("sess-B")
    expect(store.activeSessionId).toBe("sess-B")
  })

  it("falls back safely when the URL session is deleted or unowned", async () => {
    const store = useSessionStore()
    await store.loadSessions("sess-foreign")
    expect(store.activeSessionId).toBe("sess-A")
    expect(store.setActiveSession("sess-foreign")).toBe(false)
    expect(store.activeSessionId).toBe("sess-A")
  })

  it("clears account-scoped session state on logout", async () => {
    const store = useSessionStore()
    await store.loadSessions("sess-B")
    store.resetWorkspace()
    expect(store.sessions).toEqual([])
    expect(store.activeSessionId).toBeNull()
    expect(store.error).toBeNull()
  })
})
