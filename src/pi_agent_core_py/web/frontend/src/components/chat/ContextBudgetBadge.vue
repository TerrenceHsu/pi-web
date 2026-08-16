<script setup lang="ts">
import { computed } from "vue"

import type { ContextBudgetResponse } from "../../types"

const props = withDefaults(defineProps<{
  budget: ContextBudgetResponse | null
  loading?: boolean
  compacting?: boolean
  disabled?: boolean
}>(), {
  loading: false,
  compacting: false,
  disabled: false,
})

const emit = defineEmits<{ (event: "compact"): void }>()

const level = computed(() => props.budget?.estimate.level ?? "unknown")
const label = computed(() => {
  if (props.loading && !props.budget) return "Context …"
  const ratio = props.budget?.estimate.input_ratio
  return typeof ratio === "number"
    ? `Context ~${Math.round(ratio * 100)}%`
    : "Context unknown"
})
const details = computed(() => {
  const estimate = props.budget?.estimate
  if (!estimate) return "Context estimate is loading."
  const parts = [
    `Input ~${estimate.estimated_input_tokens.toLocaleString()} tokens`,
    `System ~${estimate.system_prompt_tokens.toLocaleString()}`,
    `Messages ~${estimate.message_tokens.toLocaleString()}`,
    `Tools ~${estimate.tool_definition_tokens.toLocaleString()}`,
  ]
  if (estimate.context_window) {
    parts.push(`Window ${estimate.context_window.toLocaleString()}`)
  } else {
    parts.push("Configure this model's context window in Provider settings")
  }
  if (estimate.reserved_output_tokens) {
    parts.push(`Output reserve ${estimate.reserved_output_tokens.toLocaleString()}`)
  }
  return parts.join(" · ")
})
const canCompact = computed(() => level.value === "compact" || level.value === "blocked")
</script>

<template>
  <div class="context-budget" :class="`level-${level}`" :title="details">
    <span data-testid="context-budget-label">{{ label }}</span>
    <button
      v-if="canCompact"
      type="button"
      data-testid="context-compact-button"
      :disabled="disabled || compacting"
      @click="emit('compact')"
    >{{ compacting ? "Compacting…" : "Compact" }}</button>
  </div>
</template>

<style scoped>
.context-budget {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--muted);
  background: var(--chat-bg);
  font-size: 11px;
  white-space: nowrap;
}
.level-warning {
  border-color: #f3c969;
  color: #8a5a00;
  background: #fff9e8;
}
.level-compact {
  border-color: #f0a04b;
  color: #8a3f00;
  background: #fff3e6;
}
.level-blocked {
  border-color: #e58b8b;
  color: #991b1b;
  background: #fff0f0;
}
button {
  padding: 1px 6px;
  border: 0;
  border-radius: 999px;
  color: inherit;
  background: rgb(255 255 255 / 70%);
  cursor: pointer;
  font-size: 10px;
}
button:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
