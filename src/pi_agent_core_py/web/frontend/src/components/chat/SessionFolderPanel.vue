<script setup lang="ts">
import { computed, ref, watch } from "vue"

import type { FileRef } from "../../types"
import { buildSessionFileTree } from "../../utils/fileTree"
import FileTreeNode from "./FileTreeNode.vue"
import SessionTextFileEditor from "./SessionTextFileEditor.vue"

const props = withDefaults(
  defineProps<{
    files: FileRef[]
    sessionId: string
    loading?: boolean
    loadContent: (fileId: string) => Promise<string>
    saveContent: (
      fileId: string,
      content: string,
      expectedSha256: string,
    ) => Promise<FileRef>
  }>(),
  { loading: false },
)

const emit = defineEmits<{
  (e: "delete", fileId: string): void
  (e: "refresh"): void
}>()

const tree = computed(() => buildSessionFileTree(props.files))
const selectedEditableId = ref<string | null>(null)
const selectedEditableFile = computed(() =>
  props.files.find(
    (file) =>
      file.id === selectedEditableId.value &&
      (file.purpose === "agent_instructions" || file.purpose === "memory"),
  ),
)

watch(
  () => props.sessionId,
  () => {
    selectedEditableId.value = null
  },
)
watch(selectedEditableFile, (file) => {
  if (!file) selectedEditableId.value = null
})
</script>

<template>
  <section class="session-folder" data-testid="session-folder">
    <div class="folder-toolbar">
      <div>
        <strong>Session files</strong>
        <span class="folder-count">{{ files.length }} {{ files.length === 1 ? "file" : "files" }}</span>
      </div>
      <button
        class="folder-refresh"
        type="button"
        :disabled="loading"
        aria-label="Refresh session folder"
        @click="emit('refresh')"
      >
        {{ loading ? "Loading…" : "Refresh" }}
      </button>
    </div>

    <ul v-if="tree.length" class="folder-tree" role="tree" aria-label="Session file tree">
      <FileTreeNode
        v-for="node in tree"
        :key="node.kind === 'file' ? node.file?.id : node.path"
        :node="node"
        :session-id="sessionId"
        @delete="emit('delete', $event)"
        @edit-file="selectedEditableId = $event"
      />
    </ul>
    <p v-else class="folder-empty">
      Initializing Session files…
    </p>

    <SessionTextFileEditor
      v-if="selectedEditableFile"
      :file="selectedEditableFile"
      :loading="loading"
      :load-content="loadContent"
      :save-content="saveContent"
      @close="selectedEditableId = null"
    />
  </section>
</template>

<style scoped>
.session-folder {
  flex: 0 0 auto;
  padding: 10px 24px 12px;
  border-bottom: 1px solid var(--sidebar-border);
  background: #fafafa;
  max-height: min(62vh, 560px);
  overflow-y: auto;
}
.folder-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  color: var(--fg);
  font-size: 12px;
}
.folder-count {
  margin-left: 8px;
  color: var(--muted);
  font-weight: 400;
}
.folder-refresh {
  padding: 3px 8px;
  font-size: 11px;
}
.folder-tree {
  margin: 0;
  padding: 0;
}
.folder-empty {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
</style>
