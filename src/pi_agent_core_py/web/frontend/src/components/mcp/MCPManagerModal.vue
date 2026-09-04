<script setup lang="ts">
import { watch } from "vue"

import { useMcpStore } from "../../stores/mcpStore"
import ErrorBanner from "../common/ErrorBanner.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import Modal from "../common/Modal.vue"
import MCPServerForm from "./MCPServerForm.vue"
import MCPServerList from "./MCPServerList.vue"
import MCPToolList from "./MCPToolList.vue"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: "close"): void }>()

const mcpStore = useMcpStore()

watch(
  () => props.open,
  (open) => {
    if (open) {
      mcpStore.error = null
      mcpStore.loadServers()
      mcpStore.loadTools()
    }
  },
)
</script>

<template>
  <Modal :open="open" title="MCP Servers" data-testid="mcp-modal" @close="emit('close')">
    <p class="modal-intro">
      Manage the account-wide stdio and Streamable HTTP MCP catalog. Select servers per Workspace.
    </p>
    <MCPServerForm />
    <ErrorBanner
      v-if="mcpStore.error"
      :message="mcpStore.error"
      dismissible
      @dismiss="mcpStore.error = null"
    />
    <div v-if="mcpStore.loading" class="loading-row">
      <LoadingSpinner :size="14" />
      <span>Loading…</span>
    </div>
    <MCPServerList />
    <MCPToolList />
  </Modal>
</template>

<style scoped>
.modal-intro {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 13px;
}
.loading-row {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
  padding: 6px 0;
}
</style>
