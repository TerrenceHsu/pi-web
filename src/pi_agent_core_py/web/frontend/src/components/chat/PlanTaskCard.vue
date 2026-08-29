<script setup lang="ts">
import { computed } from "vue"

import type { PlanRunItem, PlanTask } from "../../types"
import { useChatStore } from "../../stores/chatStore"

const props = defineProps<{ item: PlanRunItem }>()
const chatStore = useChatStore()

const canApprove = computed(
  () => props.item.plan.status === "awaiting_plan_approval" && !props.item.submitting,
)

function taskIcon(task: PlanTask): string {
  if (task.status === "passed") return "✓"
  if (task.status === "failed" || task.status === "blocked") return "×"
  if (task.status === "executing" || task.status === "awaiting_verification") return "●"
  return "○"
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ")
}

async function approve(): Promise<void> {
  if (!canApprove.value) return
  try {
    await chatStore.approvePlan(props.item.plan.id)
  } catch {
    // Store owns the browser-safe error shown on this card.
  }
}
</script>

<template>
  <section class="plan-card" data-testid="plan-task-card" :data-status="item.plan.status">
    <header>
      <div>
        <span class="eyebrow">Plan mode</span>
        <h3>{{ item.plan.summary || "Preparing task plan…" }}</h3>
      </div>
      <span class="run-status">{{ statusLabel(item.plan.status) }}</span>
    </header>
    <p class="goal"><strong>Goal:</strong> {{ item.plan.goal }}</p>

    <ol v-if="item.plan.tasks.length" class="task-list">
      <li
        v-for="task in item.plan.tasks"
        :key="task.id"
        class="task"
        :class="`task-${task.status}`"
        :data-testid="`plan-task-${task.id}`"
      >
        <span class="task-icon" aria-hidden="true">{{ taskIcon(task) }}</span>
        <div class="task-body">
          <div class="task-title-row">
            <strong>{{ task.ordinal }}. {{ task.title }}</strong>
            <span>{{ statusLabel(task.status) }}<template v-if="task.attempt"> · attempt {{ task.attempt }}</template></span>
          </div>
          <p>{{ task.objective }}</p>
          <ul class="criteria">
            <li v-for="criterion in task.acceptance_criteria" :key="criterion">
              {{ criterion }}
            </li>
          </ul>

          <div
            v-if="task.verification && !task.verification.passed"
            class="verifier-feedback"
            data-testid="verifier-rejection"
          >
            <strong>Verifier: {{ task.verification.reason }}</strong>
            <span v-if="task.verification.classification" class="classification">
              {{ statusLabel(task.verification.classification) }}
            </span>
            <ul v-if="task.verification.suggestions.length">
              <li v-for="suggestion in task.verification.suggestions" :key="suggestion">
                {{ suggestion }}
              </li>
            </ul>
          </div>

          <div v-if="task.blocked" class="verifier-feedback" data-testid="executor-blocker">
            <strong>Executor blocked: {{ task.blocked.reason }}</strong>
            <ul v-if="task.blocked.suggestions.length">
              <li v-for="suggestion in task.blocked.suggestions" :key="suggestion">
                {{ suggestion }}
              </li>
            </ul>
          </div>
        </div>
      </li>
    </ol>
    <div v-else class="planning">Planner is building the task list…</div>

    <footer v-if="item.plan.status === 'awaiting_plan_approval' || item.error">
      <span v-if="item.error" class="card-error">{{ item.error }}</span>
      <button
        v-if="item.plan.status === 'awaiting_plan_approval'"
        type="button"
        class="approve-btn"
        data-testid="approve-plan-button"
        :disabled="!canApprove"
        @click="approve"
      >
        {{ item.submitting ? "Approving…" : "Approve and execute" }}
      </button>
    </footer>
  </section>
</template>

<style scoped>
.plan-card {
  width: min(760px, 100%);
  border: 1px solid #c7d2fe;
  border-radius: 12px;
  background: #f8faff;
  padding: 14px;
  color: var(--fg);
}
header,
.task-title-row,
footer {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.eyebrow {
  color: #4f46e5;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
h3 {
  margin: 3px 0 0;
  font-size: 14px;
  line-height: 1.35;
}
.goal {
  margin: 9px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.run-status,
.classification {
  border-radius: 999px;
  background: #e0e7ff;
  color: #3730a3;
  padding: 3px 8px;
  font-size: 11px;
  white-space: nowrap;
}
.task-list {
  display: grid;
  gap: 8px;
  margin: 13px 0 0;
  padding: 0;
  list-style: none;
}
.task {
  display: flex;
  gap: 9px;
  border-top: 1px solid #e0e7ff;
  padding-top: 9px;
}
.task-icon {
  display: grid;
  place-items: center;
  width: 21px;
  height: 21px;
  flex: 0 0 21px;
  border-radius: 50%;
  background: #e5e7eb;
  color: #4b5563;
  font-weight: 800;
}
.task-passed .task-icon {
  background: #dcfce7;
  color: #15803d;
}
.task-failed .task-icon,
.task-blocked .task-icon {
  background: #fee2e2;
  color: #b91c1c;
}
.task-executing .task-icon,
.task-awaiting_verification .task-icon {
  background: #fef3c7;
  color: #a16207;
}
.task-body {
  flex: 1;
  min-width: 0;
}
.task-title-row span,
.criteria,
.planning {
  color: var(--muted);
  font-size: 12px;
}
.task-body p {
  margin: 4px 0;
  font-size: 13px;
}
.criteria {
  margin: 5px 0 0;
  padding-left: 18px;
}
.verifier-feedback {
  margin-top: 8px;
  padding: 8px 10px;
  border-left: 3px solid #dc2626;
  border-radius: 5px;
  background: #fff1f2;
  color: #881337;
  font-size: 12px;
}
.verifier-feedback ul {
  margin: 5px 0 0;
  padding-left: 18px;
}
.classification {
  margin-left: 7px;
  background: #ffe4e6;
  color: #9f1239;
}
footer {
  align-items: center;
  margin-top: 12px;
}
.approve-btn {
  margin-left: auto;
  border: 0;
  border-radius: 7px;
  padding: 7px 11px;
  background: #4f46e5;
  color: white;
  cursor: pointer;
}
.approve-btn:disabled {
  cursor: wait;
  opacity: 0.65;
}
.card-error {
  color: #b91c1c;
  font-size: 12px;
}
</style>
