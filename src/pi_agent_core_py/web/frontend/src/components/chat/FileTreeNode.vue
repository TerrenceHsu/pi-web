<script setup lang="ts">
import { ref } from "vue"

import { downloadFileUrl } from "../../api/files"
import type { SessionFileTreeNode } from "../../utils/fileTree"

defineOptions({ name: "FileTreeNode" })

defineProps<{
  node: SessionFileTreeNode
  sessionId: string
}>()

const emit = defineEmits<{
  (e: "delete", fileId: string): void
  (e: "edit-file", fileId: string): void
}>()

const expanded = ref(true)
</script>

<template>
  <li
    class="tree-node"
    role="treeitem"
    :aria-expanded="node.kind === 'directory' ? expanded : undefined"
    :data-path="node.path"
  >
    <div v-if="node.kind === 'directory'" class="tree-row directory-row">
      <button
        type="button"
        class="tree-main"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span aria-hidden="true">{{ expanded ? "▾" : "▸" }}</span>
        <span aria-hidden="true">📁</span>
        <span>{{ node.name }}</span>
      </button>
    </div>
    <div v-else-if="node.file" class="tree-row file-row">
      <button
        v-if="node.file.purpose === 'agent_instructions' || node.file.purpose === 'memory'"
        type="button"
        class="tree-main editable-file"
        :aria-label="`Edit ${node.name}`"
        @click="emit('edit-file', node.file.id)"
      >
        <span aria-hidden="true">{{ node.file.purpose === "memory" ? "🧠" : "⚙" }}</span>
        <span>{{ node.name }}</span>
        <span class="file-origin">
          {{ node.file.purpose === "memory" ? "memory" : "instructions" }}
        </span>
      </button>
      <span v-else class="tree-main file-label">
        <span aria-hidden="true">📄</span>
        <span :title="node.path">{{ node.name }}</span>
        <span v-if="node.file.origin" class="file-origin">{{ node.file.origin }}</span>
      </span>
      <span class="tree-actions">
        <a
          :href="downloadFileUrl(sessionId, node.file.id)"
          :download="node.file.name"
          class="tree-action"
          :aria-label="`Download ${node.name}`"
        >↓</a>
        <button
          v-if="node.file.purpose !== 'agent_instructions'"
          type="button"
          class="tree-action"
          :aria-label="`Remove ${node.name}`"
          @click="emit('delete', node.file.id)"
        >×</button>
      </span>
    </div>

    <ul v-if="node.kind === 'directory' && expanded" class="tree-children" role="group">
      <FileTreeNode
        v-for="child in node.children"
        :key="child.kind === 'file' ? child.file?.id : child.path"
        :node="child"
        :session-id="sessionId"
        @delete="emit('delete', $event)"
        @edit-file="emit('edit-file', $event)"
      />
    </ul>
  </li>
</template>

<style scoped>
.tree-node {
  list-style: none;
}
.tree-row {
  display: flex;
  align-items: center;
  min-height: 28px;
  border-radius: 5px;
}
.tree-row:hover {
  background: var(--code-bg);
}
.tree-main {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1;
  padding: 4px 6px;
  border: 0;
  background: transparent;
  color: var(--fg);
  text-align: left;
  font-size: 12px;
}
button.tree-main {
  cursor: pointer;
}
.file-label > span:nth-child(2),
.editable-file > span:nth-child(2) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.editable-file {
  color: var(--accent);
  font-weight: 600;
}
.file-origin {
  margin-left: auto;
  color: var(--muted);
  font-size: 9px;
  font-weight: 400;
  text-transform: uppercase;
}
.tree-actions {
  display: flex;
  padding-right: 4px;
}
.tree-action {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--muted);
  text-decoration: none;
  cursor: pointer;
}
.tree-action:hover {
  background: var(--border);
  color: var(--fg);
}
.tree-children {
  margin: 0 0 0 15px;
  padding: 0 0 0 5px;
  border-left: 1px solid var(--border);
}
</style>
