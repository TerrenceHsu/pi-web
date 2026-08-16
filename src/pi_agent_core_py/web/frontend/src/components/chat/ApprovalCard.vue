<script setup lang="ts">
import { computed } from "vue"

import type { ToolApprovalDecision, ToolApprovalItem } from "../../types"
import { useChatStore } from "../../stores/chatStore"

const props = defineProps<{ item: ToolApprovalItem }>()
const chatStore = useChatStore()

const argumentsJson = computed(() => {
  try {
    return JSON.stringify(props.item.arguments, null, 2)
  } catch {
    return "{}"
  }
})

const statusLabel = computed(() => {
  if (props.item.status === "approved") return "Approved once"
  if (props.item.status === "denied") return "Denied"
  if (props.item.status === "cancelled") return "Cancelled"
  return "Approval required"
})

async function decide(decision: ToolApprovalDecision) {
  try {
    await chatStore.resolveToolApproval(props.item.approvalId, decision)
  } catch {
    // The store keeps the safe server error on this exact card.
  }
}
</script>

<template>
  <section
    class="approval-card"
    :class="`status-${item.status}`"
    data-testid="tool-approval-card"
    :data-approval-id="item.approvalId"
    :data-status="item.status"
  >
    <header>
      <div>
        <div class="eyebrow">Human approval</div>
        <strong data-testid="approval-tool-name">{{ item.toolLabel }}</strong>
      </div>
      <span class="status">{{ statusLabel }}</span>
    </header>

    <p v-if="item.reason" class="reason">{{ item.reason }}</p>
    <details open>
      <summary>Arguments</summary>
      <pre data-testid="approval-arguments">{{ argumentsJson }}</pre>
    </details>

    <p v-if="item.error" class="error" role="alert">{{ item.error }}</p>
    <div v-if="item.status === 'pending'" class="actions">
      <button
        type="button"
        class="deny"
        data-testid="approval-deny"
        :disabled="item.submitting"
        @click="decide('deny')"
      >
        Deny
      </button>
      <button
        type="button"
        class="approve"
        data-testid="approval-approve"
        :disabled="item.submitting"
        @click="decide('approve')"
      >
        {{ item.submitting ? "Submitting…" : "Approve once" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.approval-card {
  padding: 14px 16px;
  border: 1px solid #f2b84b;
  border-left-width: 4px;
  border-radius: 8px;
  background: #fffbeb;
  color: #5f4307;
}
header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.eyebrow {
  margin-bottom: 2px;
  color: #946200;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.status {
  padding: 2px 8px;
  border-radius: 999px;
  background: #fef3c7;
  font-size: 11px;
  white-space: nowrap;
}
.reason {
  margin: 10px 0;
  font-size: 12px;
}
details {
  margin-top: 10px;
  font-size: 12px;
}
summary {
  cursor: pointer;
  color: #76540b;
}
pre {
  max-height: 220px;
  margin: 7px 0 0;
  padding: 10px;
  overflow: auto;
  border: 1px solid #ead8a8;
  border-radius: 6px;
  background: #fffef8;
  color: #3f310e;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
button {
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
}
button:disabled {
  cursor: default;
  opacity: 0.6;
}
.deny {
  border: 1px solid #d6b86d;
  background: white;
  color: #694b08;
}
.approve {
  border: 1px solid #2563eb;
  background: #2563eb;
  color: white;
}
.error {
  margin: 8px 0 0;
  color: #b91c1c;
  font-size: 12px;
}
.status-approved {
  border-color: #73b987;
  background: #effaf2;
  color: #245c34;
}
.status-denied,
.status-cancelled {
  border-color: #cbd0d8;
  background: #f5f6f8;
  color: #5b6270;
}
</style>
