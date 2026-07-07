<script setup lang="ts">
import { computed } from "vue"

import { useMcpStore } from "../../stores/mcpStore"

const mcpStore = useMcpStore()

const tools = computed(() => mcpStore.tools)

async function onEnable(name: string) {
  try {
    await mcpStore.enableTool(name)
  } catch {
    // store 已写 error
  }
}

async function onDisable(name: string) {
  try {
    await mcpStore.disableTool(name)
  } catch {
    // store 已写 error
  }
}
</script>

<template>
  <div class="mcp-tool-list">
    <h3 class="block-title">Tools</h3>
    <p v-if="tools.length === 0" class="empty">
      No MCP tools available. Enable a server to see its tools.
    </p>
    <div v-for="t in tools" :key="t.name" class="tool-row">
      <div class="tool-info">
        <div class="tool-name-row">
          <span class="tool-name">{{ t.name }}</span>
          <span
            class="badge"
            :class="t.enabled ? 'badge-on' : 'badge-off'"
          >{{ t.enabled ? "enabled" : "disabled" }}</span>
        </div>
        <div class="tool-meta">
          <span class="muted">server: {{ t.server }}</span>
          <span v-if="t.mcp_tool && t.mcp_tool !== t.name" class="muted">
            · mcp_tool: {{ t.mcp_tool }}
          </span>
        </div>
        <p v-if="t.description" class="tool-desc">{{ t.description }}</p>
      </div>
      <div class="tool-actions">
        <button
          v-if="!t.enabled"
          type="button"
          class="primary"
          @click="onEnable(t.name)"
        >Enable</button>
        <button
          v-else
          type="button"
          @click="onDisable(t.name)"
        >Disable</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mcp-tool-list {
  margin-top: 8px;
}
.block-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg);
}
.empty {
  padding: 12px;
  color: var(--muted);
  text-align: center;
  font-style: italic;
  font-size: 12px;
}
.tool-row {
  display: flex;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: white;
  margin-bottom: 6px;
  align-items: flex-start;
}
.tool-info {
  flex: 1;
  min-width: 0;
}
.tool-name-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.tool-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--fg);
  word-break: break-all;
}
.tool-meta {
  font-size: 11px;
  margin-top: 2px;
}
.tool-meta .muted {
  color: var(--muted);
}
.tool-desc {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--fg);
  word-break: break-word;
}
.tool-actions {
  flex-shrink: 0;
}
.tool-actions button {
  padding: 4px 10px;
  font-size: 12px;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 500;
}
.badge-on {
  background: var(--success);
  color: white;
}
.badge-off {
  background: var(--code-bg);
  color: var(--muted);
}
</style>
