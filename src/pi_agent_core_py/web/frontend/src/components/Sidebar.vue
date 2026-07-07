<script setup lang="ts">
import type { AgentStateSummary } from "../types"

defineProps<{
  state: AgentStateSummary
  developerOpen: boolean
}>()

const emit = defineEmits<{
  (e: "new-chat"): void
  (e: "open-developer"): void
  (e: "close-developer"): void
}>()
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-brand">
      <span class="brand-dot"></span>
      <span class="brand-name">pi-agent-core-py</span>
    </div>

    <button class="new-chat-btn" @click="emit('new-chat')">
      <span class="plus">+</span>
      New chat
    </button>

    <div class="session-card">
      <div class="session-card-title">Current session</div>
      <div class="session-card-row">
        <span>Status</span>
        <span :class="['status-pill', state.running ? 'running' : state.last_error ? 'error' : 'idle']">
          {{ state.running ? "running" : state.agent_status }}
        </span>
      </div>
      <div class="session-card-row">
        <span>Turns</span>
        <strong>{{ state.turn_count }}</strong>
      </div>
      <div class="session-card-row">
        <span>Messages</span>
        <strong>{{ state.message_count }}</strong>
      </div>
      <div class="session-card-row">
        <span>Snapshots</span>
        <strong>{{ state.snapshot_count }}</strong>
      </div>
      <div v-if="state.last_error" class="session-card-error">
        {{ state.last_error }}
      </div>
    </div>

    <div class="spacer"></div>

    <button
      class="developer-btn"
      :class="{ active: developerOpen }"
      @click="developerOpen ? emit('close-developer') : emit('open-developer')"
    >
      <span class="dev-icon">▣</span>
      Developer
    </button>
  </aside>
</template>
