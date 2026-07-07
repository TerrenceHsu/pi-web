<script setup lang="ts">
import type { AgentStateSummary } from "../types"

defineProps<{ state: AgentStateSummary }>()
</script>

<template>
  <pre style="margin: 12px;">{{ JSON.stringify(state, null, 2) }}</pre>
</template>
