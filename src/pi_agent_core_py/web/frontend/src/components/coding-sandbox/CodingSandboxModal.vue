<script setup lang="ts">
import { computed, ref, watch } from "vue"

import { useCodingSandboxStore } from "../../stores/codingSandboxStore"
import { useSessionStore } from "../../stores/sessionStore"
import type { ManagedSandboxAction, ManagedSandboxEvent } from "../../types/codingSandbox"
import ErrorBanner from "../common/ErrorBanner.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import Modal from "../common/Modal.vue"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (event: "close"): void }>()

const sandboxStore = useCodingSandboxStore()
const sessionStore = useSessionStore()
const approvalConfirmed = ref(false)

const operation = computed(() => sandboxStore.operation)
const canStart = computed(
  () => !!sessionStore.activeSessionId && (!operation.value || operation.value.terminal),
)

function hasAction(action: ManagedSandboxAction): boolean {
  const current = operation.value
  if (!current) return false
  if (current.allowed_actions) return current.allowed_actions.includes(action)
  if (action === "validate") {
    return ["ready", "validation_failed", "validated"].includes(current.status)
  }
  if (action === "prepare_publish") return current.status === "validated"
  if (action === "publish") return current.status === "awaiting_approval"
  if (action === "cancel") return current.cancellable
  return !["published", "publishing", "cancelled", "discarded"].includes(current.status)
}

const canValidate = computed(() => hasAction("validate"))
const canDiff = computed(() =>
  ["ready", "validation_failed", "validated", "awaiting_approval"].includes(
    operation.value?.status ?? "",
  ),
)
const canPreparePublish = computed(() => hasAction("prepare_publish"))
const canPublish = computed(() => hasAction("publish"))
const canCancel = computed(() => hasAction("cancel"))
const canDiscard = computed(() => hasAction("discard"))

watch(
  () => props.open,
  (open) => {
    if (open) void sandboxStore.restoreSession(sessionStore.activeSessionId)
  },
  { immediate: true },
)

watch(
  () => operation.value?.artifact_id,
  () => {
    approvalConfirmed.value = false
  },
)

function statusLabel(value: string): string {
  return value.replaceAll("_", " ")
}

function formatTime(value: number): string {
  return new Date(value).toLocaleTimeString()
}

function eventDetail(event: ManagedSandboxEvent): string {
  const payload = { ...event.payload }
  delete payload.status
  delete payload.workspace_revision
  if (Object.keys(payload).length === 0) return ""
  try {
    return JSON.stringify(payload)
  } catch {
    return "Event details unavailable"
  }
}
</script>

<template>
  <Modal :open="open" title="Coding Sandbox" width="min(1120px, 95vw)" @close="emit('close')">
    <p class="intro">
      Code runs in an isolated managed runtime. Changes reach the local workspace only after
      validation, diff review, and explicit publish approval.
    </p>

    <ErrorBanner
      v-if="sandboxStore.error"
      :message="sandboxStore.error"
      dismissible
      @dismiss="sandboxStore.error = null"
    />

    <div v-if="sandboxStore.loading" class="loading-row">
      <LoadingSpinner :size="16" /> Restoring Sandbox state…
    </div>

    <div v-else-if="!sandboxStore.available" class="empty-card">
      Managed Sandbox is unavailable. Enable it and configure an E2B credential in the
      local server configuration.
    </div>

    <div v-else-if="!operation" class="empty-card">
      <p>No Sandbox operation exists for this session.</p>
      <button
        type="button"
        class="primary"
        data-testid="sandbox-start"
        :disabled="sandboxStore.actionRunning || !sessionStore.activeSessionId"
        @click="sandboxStore.start"
      >
        Start Sandbox
      </button>
    </div>

    <template v-else>
      <section class="status-card">
        <div>
          <span :class="['status-pill', `status-${operation.status}`]">
            {{ statusLabel(operation.status) }}
          </span>
          <span class="revision">revision {{ operation.workspace_revision }}</span>
          <span v-if="operation.baseline_workspace_revision != null" class="revision">
            Workspace baseline {{ operation.baseline_workspace_revision }}
          </span>
        </div>
        <div class="operation-id">{{ operation.operation_id }}</div>
        <div v-if="operation.error_code" class="operation-error">
          {{ operation.error_code }}
        </div>
      </section>

      <div class="actions" aria-label="Sandbox actions">
        <button
          v-if="canStart"
          type="button"
          data-testid="sandbox-start-new"
          :disabled="sandboxStore.busy"
          @click="sandboxStore.start"
        >
          Start new
        </button>
        <button
          type="button"
          data-testid="sandbox-diff"
          :disabled="sandboxStore.busy || !canDiff"
          @click="sandboxStore.refreshDiff"
        >
          Refresh diff
        </button>
        <button
          type="button"
          data-testid="sandbox-validate"
          :disabled="sandboxStore.busy || !canValidate"
          @click="sandboxStore.validate"
        >
          Validate
        </button>
        <button
          type="button"
          data-testid="sandbox-prepare-publish"
          :disabled="sandboxStore.busy || !canPreparePublish"
          @click="sandboxStore.preparePublish"
        >
          Freeze for review
        </button>
        <button
          v-if="canCancel"
          type="button"
          data-testid="sandbox-cancel"
          :disabled="sandboxStore.busy"
          @click="sandboxStore.cancel"
        >
          Cancel
        </button>
        <button
          v-if="canDiscard"
          type="button"
          class="danger"
          data-testid="sandbox-discard"
          :disabled="sandboxStore.busy"
          @click="sandboxStore.discard"
        >
          Discard
        </button>
      </div>

      <section v-if="operation.validation" class="panel">
        <h3>Validation</h3>
        <div :class="operation.validation.passed ? 'passed' : 'failed'">
          {{ operation.validation.passed ? "Passed" : "Failed" }} in
          {{ operation.validation.duration_ms }} ms
        </div>
        <div v-for="check in operation.validation.checks" :key="check.check_id" class="check">
          <div class="check-heading">
            <strong>{{ check.check_id }}</strong>
            <span>{{ check.status }} · {{ check.duration_ms }} ms</span>
          </div>
          <pre v-if="check.stdout || check.stderr">{{ check.stdout }}{{ check.stderr }}</pre>
        </div>
      </section>

      <section class="panel">
        <h3>Workspace diff</h3>
        <div v-if="!operation.diff" class="muted">No diff has been captured yet.</div>
        <div v-else-if="operation.diff.entries.length === 0" class="muted">
          No workspace changes.
        </div>
        <template v-else>
          <ul class="diff-list">
            <li v-for="entry in operation.diff.entries" :key="entry.path">
              <span :class="`diff-${entry.status}`">{{ entry.status }}</span>
              <code>{{ entry.path }}</code>
            </li>
          </ul>
          <pre v-if="operation.diff.patch" class="patch">{{ operation.diff.patch }}</pre>
          <div v-if="operation.diff.patch_truncated" class="muted">
            Patch preview was truncated.
          </div>
        </template>
      </section>

      <section v-if="operation.status === 'awaiting_approval'" class="approval-panel">
        <h3>Publish approval</h3>
        <p>
          The artifact is frozen. Publishing applies exactly the reviewed paths transactionally to
          the managed local project.
        </p>
        <p v-if="operation.publish_available === false" class="muted">
          This artifact was created from a Workspace revision. Publishing is paused until the
          transactional Workspace publisher is available; you can review or discard it safely.
        </p>
        <template v-else>
          <label>
            <input v-model="approvalConfirmed" type="checkbox" />
            I reviewed the validation result and complete diff.
          </label>
          <button
            type="button"
            class="primary"
            data-testid="sandbox-publish"
            :disabled="sandboxStore.busy || !canPublish || !approvalConfirmed"
            @click="sandboxStore.publish"
          >
            Publish to workspace
          </button>
        </template>
      </section>

      <section v-if="operation.status === 'published'" class="published-panel">
        Published transaction {{ operation.publish_transaction_id }}.
      </section>

      <section class="panel event-panel">
        <h3>
          Operation log
          <span class="connection">{{ sandboxStore.connected ? "live" : "replaying" }}</span>
        </h3>
        <div v-if="sandboxStore.events.length === 0" class="muted">No events yet.</div>
        <ol v-else class="event-list">
          <li v-for="event in sandboxStore.events.slice(-100)" :key="event.sequence">
            <time>{{ formatTime(event.recorded_at_ms) }}</time>
            <strong>{{ event.event_type }}</strong>
            <code v-if="eventDetail(event)">{{ eventDetail(event) }}</code>
          </li>
        </ol>
      </section>
    </template>
  </Modal>
</template>

<style scoped>
.intro {
  margin: 0 0 14px;
  color: var(--muted);
  font-size: 13px;
}
.loading-row,
.empty-card {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 180px;
  padding: 24px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  color: var(--muted);
  text-align: center;
}
.empty-card {
  flex-direction: column;
}
.status-card,
.panel,
.approval-panel,
.published-panel {
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.status-card {
  display: grid;
  gap: 6px;
}
.status-pill {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 999px;
  background: var(--code-bg);
  color: var(--fg);
  font-size: 12px;
  font-weight: 600;
}
.status-published,
.status-validated,
.status-ready {
  color: #126b39;
}
.status-failed,
.status-validation_failed,
.status-interrupted {
  color: var(--danger);
}
.revision,
.connection {
  margin-left: 8px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 400;
}
.operation-id,
.operation-error {
  color: var(--muted);
  font-family: monospace;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.operation-error,
.failed {
  color: var(--danger);
}
.passed,
.published-panel {
  color: #126b39;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
button {
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: white;
  color: var(--fg);
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
button.primary {
  border-color: var(--accent);
  background: var(--accent);
  color: white;
}
button.danger {
  color: var(--danger);
}
h3 {
  margin: 0 0 8px;
  font-size: 13px;
}
.check {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.check-heading {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}
pre {
  max-height: 260px;
  overflow: auto;
  padding: 10px;
  border-radius: 6px;
  background: var(--code-bg);
  font-size: 11px;
  white-space: pre-wrap;
}
.patch {
  white-space: pre;
}
.diff-list {
  margin: 0 0 8px;
  padding-left: 20px;
  font-size: 12px;
}
.diff-list span {
  display: inline-block;
  width: 62px;
  font-weight: 600;
}
.diff-added {
  color: #126b39;
}
.diff-modified {
  color: #835c00;
}
.diff-deleted {
  color: var(--danger);
}
.approval-panel {
  border-color: var(--accent);
}
.approval-panel label {
  display: block;
  margin: 10px 0;
  font-size: 13px;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.event-list {
  max-height: 220px;
  margin: 0;
  overflow-y: auto;
  padding-left: 24px;
  font-size: 11px;
}
.event-list li {
  margin-bottom: 5px;
}
.event-list time {
  margin-right: 8px;
  color: var(--muted);
}
.event-list code {
  display: block;
  margin-top: 2px;
  overflow-wrap: anywhere;
  color: var(--muted);
}
</style>
