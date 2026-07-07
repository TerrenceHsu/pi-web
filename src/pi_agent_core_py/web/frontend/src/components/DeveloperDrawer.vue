<script setup lang="ts">
import { ref } from "vue"
import SnapshotPanel from "./SnapshotPanel.vue"
import SessionPanel from "./SessionPanel.vue"
import McpPanel from "./McpPanel.vue"
import SkillsPanel from "./SkillsPanel.vue"
import PolicyAuditPanel from "./PolicyAuditPanel.vue"
import RawJsonPanel from "./RawJsonPanel.vue"
import type { AgentStateSummary } from "../types"

defineProps<{
  refreshTick: number
  state: AgentStateSummary
}>()

const emit = defineEmits<{ (e: "close"): void }>()

const activeTab = ref<
  "snapshots" | "session" | "mcp" | "skills" | "policy" | "raw"
>("snapshots")

const tabs = [
  { key: "snapshots", label: "Snapshots" },
  { key: "session", label: "Session" },
  { key: "mcp", label: "MCP" },
  { key: "skills", label: "Skills" },
  { key: "policy", label: "Policy" },
  { key: "raw", label: "Raw JSON" },
] as const
</script>

<template>
  <div class="drawer-overlay" @click.self="emit('close')">
    <div class="drawer">
      <header class="drawer-header">
        <h2>Developer tools</h2>
        <button class="drawer-close" aria-label="Close" @click="emit('close')">×</button>
      </header>

      <nav class="drawer-tabs">
        <button
          v-for="t in tabs"
          :key="t.key"
          :class="{ active: activeTab === t.key }"
          @click="activeTab = t.key"
        >
          {{ t.label }}
        </button>
      </nav>

      <div class="drawer-body">
        <SnapshotPanel v-if="activeTab === 'snapshots'" :refresh-tick="refreshTick" />
        <SessionPanel v-else-if="activeTab === 'session'" :refresh-tick="refreshTick" />
        <McpPanel v-else-if="activeTab === 'mcp'" :refresh-tick="refreshTick" />
        <SkillsPanel v-else-if="activeTab === 'skills'" :refresh-tick="refreshTick" />
        <PolicyAuditPanel v-else-if="activeTab === 'policy'" :refresh-tick="refreshTick" />
        <RawJsonPanel v-else :state="state" />
      </div>
    </div>
  </div>
</template>
