<script setup lang="ts">
import { computed } from "vue"

import { useKnowledgeStore } from "../../stores/knowledgeStore"
import { ApiError } from "../../api/client"

const props = defineProps<{
  sessionId: string | null
  libraryId: string
}>()

const knowledgeStore = useKnowledgeStore()

const bound = computed(() => knowledgeStore.isLibraryBound(props.libraryId))
const disabled = computed(
  () => !props.sessionId || knowledgeStore.loadingBindings,
)

const title = computed(() => {
  if (!props.sessionId) return "Select a chat session first"
  if (knowledgeStore.loadingBindings) return "Loading binding state…"
  return bound.value
    ? "Library is available to the current chat session"
    : "Enable to expose this library to the Agent's search_knowledge tool"
})

async function onToggle() {
  if (!props.sessionId || disabled.value) return
  try {
    if (bound.value) {
      await knowledgeStore.unbindLibraryFromSession(props.sessionId, props.libraryId)
    } else {
      await knowledgeStore.bindLibraryToSession(props.sessionId, props.libraryId)
    }
  } catch (e: any) {
    // rollback handled in store; surface a transient error
    if (e instanceof ApiError) {
      knowledgeStore.error = e.detail
    }
  }
}
</script>

<template>
  <div class="binding-toggle" :title="title">
    <span class="label">In this conversation</span>
    <button
      :class="['toggle-btn', { on: bound }]"
      :disabled="disabled"
      :data-testid="`binding-toggle-${libraryId}`"
      :aria-pressed="bound"
      @click="onToggle"
    >
      <span class="slider"></span>
    </button>
  </div>
</template>

<style scoped>
.binding-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.label {
  font-size: 12px;
  color: var(--muted);
}
.toggle-btn {
  position: relative;
  width: 36px;
  height: 20px;
  border-radius: 12px;
  background: var(--border, #ccc);
  border: none;
  cursor: pointer;
  padding: 0;
  transition: background-color 0.15s ease;
}
.toggle-btn.on {
  background: var(--accent, #4a9eff);
}
.toggle-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.slider {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: white;
  transition: transform 0.15s ease;
}
.toggle-btn.on .slider {
  transform: translateX(16px);
}
</style>
