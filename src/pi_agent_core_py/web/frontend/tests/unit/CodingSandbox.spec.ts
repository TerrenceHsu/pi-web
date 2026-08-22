import { createPinia, setActivePinia } from "pinia"
import { DOMWrapper, mount } from "@vue/test-utils"
import { nextTick } from "vue"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { ManagedSandboxEvent, ManagedSandboxOperation } from "../../src/types/codingSandbox"

const { api, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number
    readonly detail: string
    readonly payload = null

    constructor(status: number, detail: string) {
      super(detail)
      this.status = status
      this.detail = detail
    }
  }
  return {
    api: {
      startSandboxOperation: vi.fn(),
      getLatestSandboxOperation: vi.fn(),
      getSandboxOperation: vi.fn(),
      getSandboxEvents: vi.fn(),
      getSandboxDiff: vi.fn(),
      validateSandboxOperation: vi.fn(),
      prepareSandboxPublish: vi.fn(),
      publishSandboxOperation: vi.fn(),
      cancelSandboxOperation: vi.fn(),
      discardSandboxOperation: vi.fn(),
    },
    FakeApiError,
  }
})

vi.mock("../../src/api/codingSandbox", () => api)
vi.mock("../../src/api/client", () => ({ ApiError: FakeApiError }))
vi.mock("../../src/api/websocket", () => ({
  createEventSocket: vi.fn(() => ({
    close: vi.fn(),
    reconnect: vi.fn(),
    isOpen: vi.fn(() => true),
    closeForTest: vi.fn(),
  })),
}))

import CodingSandboxModal from "../../src/components/coding-sandbox/CodingSandboxModal.vue"
import { useCodingSandboxStore } from "../../src/stores/codingSandboxStore"
import { useSessionStore } from "../../src/stores/sessionStore"

function operation(overrides: Partial<ManagedSandboxOperation> = {}): ManagedSandboxOperation {
  return {
    schema_version: "pi-agent-managed-sandbox-operation/v1",
    operation_id: "sandbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    session_id: "session-one",
    status: "ready",
    config_revision: 1,
    created_at_ms: 100,
    updated_at_ms: 101,
    workspace_revision: 0,
    baseline_archive_sha256: "a".repeat(64),
    baseline_manifest_sha256: "b".repeat(64),
    validation: null,
    diff: null,
    artifact_id: null,
    artifact_sha256: null,
    publish_transaction_id: null,
    changed_paths: [],
    deleted_paths: [],
    error_code: null,
    terminal: false,
    cancellable: true,
    approval_required: false,
    ...overrides,
  }
}

const event: ManagedSandboxEvent = {
  schema_version: "pi-agent-managed-sandbox-event/v1",
  operation_id: "sandbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  session_id: "session-one",
  sequence: 1,
  event_type: "sandbox_operation_ready",
  recorded_at_ms: 101,
  payload: { status: "ready", workspace_revision: 0 },
}

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(api).forEach((mock) => mock.mockReset())
  api.getSandboxEvents.mockResolvedValue({
    events: [event],
    first_available_sequence: 1,
    last_available_sequence: 1,
    has_more: false,
    gap: false,
  })
  document.body.innerHTML = ""
})

describe("coding Sandbox state recovery", () => {
  it("restores persisted status and events without replaying work", async () => {
    api.getLatestSandboxOperation.mockResolvedValue({ operation: operation() })
    const store = useCodingSandboxStore()

    await store.restoreSession("session-one")

    expect(store.operation?.status).toBe("ready")
    expect(store.events).toEqual([event])
    expect(api.startSandboxOperation).not.toHaveBeenCalled()
    expect(api.validateSandboxOperation).not.toHaveBeenCalled()
    expect(api.publishSandboxOperation).not.toHaveBeenCalled()
  })

  it("reports an unavailable lifecycle without throwing", async () => {
    api.getLatestSandboxOperation.mockRejectedValue(new FakeApiError(503, "Sandbox unavailable"))
    const store = useCodingSandboxStore()

    await expect(store.restoreSession("session-one")).resolves.toBeUndefined()
    expect(store.available).toBe(false)
    expect(store.error).toBe("Sandbox unavailable")
  })
})

describe("coding Sandbox publish approval", () => {
  it("keeps publish disabled until the reviewed diff is explicitly confirmed", async () => {
    const awaiting = operation({
      status: "awaiting_approval",
      approval_required: true,
      artifact_id: "artifact-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      artifact_sha256: "c".repeat(64),
      diff: {
        entries: [
          {
            path: "src/app.py",
            status: "modified",
            before_sha256: "d".repeat(64),
            after_sha256: "e".repeat(64),
          },
        ],
        patch: "-old\n+new\n",
        patch_truncated: false,
      },
    })
    api.getLatestSandboxOperation.mockResolvedValue({ operation: awaiting })
    api.publishSandboxOperation.mockResolvedValue(
      operation({
        status: "published",
        terminal: true,
        cancellable: false,
        publish_transaction_id: "publish-cccccccccccccccccccccccccccccccc",
      }),
    )

    const sessions = useSessionStore()
    sessions.activeSessionId = "session-one"
    mount(CodingSandboxModal, { props: { open: true } })
    await nextTick()
    await new Promise((resolve) => setTimeout(resolve, 0))
    await nextTick()

    const publish = new DOMWrapper(document.body.querySelector('[data-testid="sandbox-publish"]')!)
    expect(publish.attributes("disabled")).toBeDefined()
    expect(document.body.textContent).toContain("src/app.py")

    const checkbox = new DOMWrapper(
      document.body.querySelector('.approval-panel input[type="checkbox"]')!,
    )
    await checkbox.setValue(true)
    expect(publish.attributes("disabled")).toBeUndefined()
    await publish.trigger("click")

    expect(api.publishSandboxOperation).toHaveBeenCalledWith(awaiting.operation_id)
  })
})
