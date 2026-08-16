<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue"

import type { FileRef } from "../../types"

const props = defineProps<{
  file: FileRef
  loading?: boolean
  saving?: boolean
  loadContent: (fileId: string) => Promise<string>
  saveContent: (fileId: string, content: string, expectedSha256: string) => Promise<FileRef>
}>()

const emit = defineEmits<{
  (e: "close"): void
  (e: "saved", file: FileRef): void
}>()

const content = ref("")
const editorError = ref("")
const busy = ref(false)
const isMemory = computed(() => props.file.purpose === "memory")
const description = computed(() =>
  isMemory.value
    ? "Loaded as durable context in future turns"
    : "Loaded as instructions into every turn in this Session",
)
const footerHint = computed(() =>
  isMemory.value
    ? "Changes apply as durable context from the next Agent turn."
    : "Changes apply to the next Agent turn.",
)

async function load(): Promise<void> {
  busy.value = true
  editorError.value = ""
  try {
    content.value = await props.loadContent(props.file.id)
  } catch (error: any) {
    editorError.value = String(error?.message || `Unable to load ${props.file.name}`)
  } finally {
    busy.value = false
  }
}

async function save(): Promise<void> {
  busy.value = true
  editorError.value = ""
  try {
    const updated = await props.saveContent(
      props.file.id,
      content.value,
      props.file.sha256,
    )
    emit("saved", updated)
  } catch (error: any) {
    editorError.value = String(error?.message || `Unable to save ${props.file.name}`)
  } finally {
    busy.value = false
  }
}

onMounted(load)
watch(() => props.file.id, load)
</script>

<template>
  <section class="text-file-editor" data-testid="session-text-file-editor">
    <div class="editor-header">
      <div>
        <strong>{{ file.name }}</strong>
        <span>{{ description }}</span>
      </div>
      <button type="button" :aria-label="`Close ${file.name} editor`" @click="emit('close')">×</button>
    </div>
    <textarea
      v-model="content"
      :aria-label="`${file.name} content`"
      spellcheck="false"
      :disabled="busy || loading || saving"
    />
    <p v-if="editorError" class="editor-error">{{ editorError }}</p>
    <div class="editor-footer">
      <span>{{ footerHint }}</span>
      <button
        type="button"
        :aria-label="`Save ${file.name}`"
        :disabled="busy || loading || saving"
        @click="save"
      >
        {{ busy || saving ? "Saving…" : `Save ${file.name}` }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.text-file-editor {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.editor-header,
.editor-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.editor-header div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.editor-header span,
.editor-footer span {
  color: var(--muted);
  font-size: 10px;
}
textarea {
  box-sizing: border-box;
  width: 100%;
  min-height: 180px;
  margin: 8px 0;
  padding: 10px;
  resize: vertical;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--chat-bg);
  color: var(--fg);
  font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.editor-error {
  margin: 0 0 8px;
  color: var(--danger, #b91c1c);
  font-size: 11px;
}
</style>
