import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as sandboxApi from "../api/codingSandbox"
import { ApiError } from "../api/client"
import { createEventSocket, type EventSocket } from "../api/websocket"
import type {
  ManagedSandboxEvent,
  ManagedSandboxOperation,
  SandboxDiff,
} from "../types/codingSandbox"
import { isWebEventEnvelope } from "../types/events"

const TRANSIENT_STATUSES = new Set([
  "creating",
  "validating",
  "freezing",
  "publishing",
  "cancelling",
  "discarding",
])

export const useCodingSandboxStore = defineStore("codingSandbox", () => {
  const activeSessionId = ref<string | null>(null)
  const operation = ref<ManagedSandboxOperation | null>(null)
  const events = ref<ManagedSandboxEvent[]>([])
  const loading = ref(false)
  const actionRunning = ref(false)
  const available = ref(true)
  const connected = ref(false)
  const error = ref<string | null>(null)

  const hasOperation = computed(() => operation.value !== null)
  const busy = computed(
    () =>
      actionRunning.value ||
      (operation.value !== null && TRANSIENT_STATUSES.has(operation.value.status)),
  )

  let activationVersion = 0
  let socket: EventSocket | null = null
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let refreshTimer: ReturnType<typeof setTimeout> | null = null

  function messageOf(cause: unknown): string {
    if (cause instanceof ApiError) return cause.detail
    return cause instanceof Error ? cause.message : "Sandbox request failed."
  }

  function stopPolling(): void {
    if (pollTimer !== null) clearTimeout(pollTimer)
    pollTimer = null
  }

  function schedulePolling(): void {
    stopPolling()
    if (!operation.value || !TRANSIENT_STATUSES.has(operation.value.status)) return
    const expectedOperationId = operation.value.operation_id
    pollTimer = setTimeout(() => {
      pollTimer = null
      void refreshOperation(expectedOperationId)
    }, 900)
  }

  function applyOperation(next: ManagedSandboxOperation): void {
    if (next.session_id !== activeSessionId.value) return
    operation.value = next
    schedulePolling()
  }

  async function loadEvents(operationId: string, reset = false): Promise<void> {
    let cursor = reset ? 0 : (events.value.at(-1)?.sequence ?? 0)
    const collected: ManagedSandboxEvent[] = reset ? [] : [...events.value]
    for (let pageCount = 0; pageCount < 10; pageCount += 1) {
      const page = await sandboxApi.getSandboxEvents(operationId, cursor)
      if (page.gap) collected.length = 0
      for (const event of page.events) {
        if (!collected.some((existing) => existing.sequence === event.sequence)) {
          collected.push(event)
        }
      }
      const last = page.events.at(-1)
      if (last) cursor = last.sequence
      if (!page.has_more || !last) break
    }
    if (operation.value?.operation_id === operationId) {
      events.value = collected.sort((left, right) => left.sequence - right.sequence)
    }
  }

  async function refreshOperation(operationId?: string): Promise<void> {
    const target = operationId ?? operation.value?.operation_id
    if (!target) return
    try {
      const next = await sandboxApi.getSandboxOperation(target)
      if (operation.value?.operation_id !== target || next.session_id !== activeSessionId.value) {
        return
      }
      applyOperation(next)
      await loadEvents(target)
    } catch (cause) {
      error.value = messageOf(cause)
      stopPolling()
    }
  }

  async function restoreSession(sessionId: string | null): Promise<void> {
    const version = ++activationVersion
    stopPolling()
    activeSessionId.value = sessionId
    operation.value = null
    events.value = []
    error.value = null
    available.value = true
    if (!sessionId) return
    loading.value = true
    try {
      const response = await sandboxApi.getLatestSandboxOperation(sessionId)
      if (version !== activationVersion || activeSessionId.value !== sessionId) return
      operation.value = response.operation
      if (response.operation) {
        await loadEvents(response.operation.operation_id, true)
        schedulePolling()
      }
    } catch (cause) {
      if (version !== activationVersion) return
      if (cause instanceof ApiError && [404, 503].includes(cause.status)) {
        available.value = false
      }
      error.value = messageOf(cause)
    } finally {
      if (version === activationVersion) loading.value = false
    }
  }

  async function start(): Promise<void> {
    const sessionId = activeSessionId.value
    if (!sessionId) return
    actionRunning.value = true
    error.value = null
    try {
      const next = await sandboxApi.startSandboxOperation(sessionId)
      events.value = []
      applyOperation(next)
      await loadEvents(next.operation_id, true)
    } catch (cause) {
      error.value = messageOf(cause)
    } finally {
      actionRunning.value = false
    }
  }

  async function runAction(
    action: (operationId: string) => Promise<ManagedSandboxOperation>,
  ): Promise<void> {
    const operationId = operation.value?.operation_id
    if (!operationId) return
    actionRunning.value = true
    error.value = null
    try {
      applyOperation(await action(operationId))
      await loadEvents(operationId)
    } catch (cause) {
      error.value = messageOf(cause)
      await refreshOperation(operationId)
    } finally {
      actionRunning.value = false
    }
  }

  const validate = () => runAction(sandboxApi.validateSandboxOperation)
  const preparePublish = () => runAction(sandboxApi.prepareSandboxPublish)
  const publish = () => runAction(sandboxApi.publishSandboxOperation)
  const cancel = () => runAction(sandboxApi.cancelSandboxOperation)
  const discard = () => runAction(sandboxApi.discardSandboxOperation)

  async function refreshDiff(): Promise<SandboxDiff | null> {
    const operationId = operation.value?.operation_id
    if (!operationId) return null
    actionRunning.value = true
    error.value = null
    try {
      const diff = await sandboxApi.getSandboxDiff(operationId)
      await refreshOperation(operationId)
      return diff
    } catch (cause) {
      error.value = messageOf(cause)
      return null
    } finally {
      actionRunning.value = false
    }
  }

  function scheduleLiveRefresh(): void {
    if (refreshTimer !== null) return
    refreshTimer = setTimeout(() => {
      refreshTimer = null
      void refreshOperation()
    }, 120)
  }

  function connectEvents(): void {
    if (socket) return
    socket = createEventSocket({
      onOpen: () => {
        connected.value = true
        scheduleLiveRefresh()
      },
      onClose: () => {
        connected.value = false
      },
      onEvent: (event) => {
        if (!isWebEventEnvelope(event)) return
        if (event.session_id !== activeSessionId.value) return
        if (!event.type.startsWith("sandbox_")) return
        if (operation.value && event.payload.operation_id !== operation.value.operation_id) {
          return
        }
        scheduleLiveRefresh()
      },
    })
  }

  function disconnectEvents(): void {
    socket?.close()
    socket = null
    connected.value = false
    if (refreshTimer !== null) clearTimeout(refreshTimer)
    refreshTimer = null
  }

  function resetWorkspace(): void {
    activationVersion += 1
    stopPolling()
    disconnectEvents()
    activeSessionId.value = null
    operation.value = null
    events.value = []
    loading.value = false
    actionRunning.value = false
    available.value = true
    error.value = null
  }

  return {
    operation,
    events,
    loading,
    actionRunning,
    available,
    connected,
    error,
    hasOperation,
    busy,
    restoreSession,
    start,
    validate,
    preparePublish,
    publish,
    cancel,
    discard,
    refreshDiff,
    refreshOperation,
    connectEvents,
    disconnectEvents,
    resetWorkspace,
  }
})
