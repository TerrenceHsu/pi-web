<script setup lang="ts">
import { ref } from "vue"

import { useKnowledgeStore } from "../../stores/knowledgeStore"
import ErrorBanner from "../common/ErrorBanner.vue"

const knowledgeStore = useKnowledgeStore()

const fileInput = ref<HTMLInputElement | null>(null)

function onPick() {
  fileInput.value?.click()
}

async function onChange(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  target.value = "" // reset for re-pick
  if (!file) return

  // Front-end MIME / extension pre-check (backend remains authority).
  const isPdf =
    file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")
  if (!isPdf) {
    knowledgeStore.uploadError = "Only PDF files are accepted."
    return
  }

  try {
    await knowledgeStore.uploadPdf(file)
  } catch {
    // surfaced via store.uploadError
  }
}
</script>

<template>
  <div class="upload-row" data-testid="upload-row">
    <input
      ref="fileInput"
      type="file"
      accept="application/pdf,.pdf"
      class="hidden-file"
      data-testid="upload-input"
      @change="onChange"
    />
    <button
      class="upload-btn"
      data-testid="upload-btn"
      :disabled="knowledgeStore.uploadingDocument || !knowledgeStore.selectedLibraryId"
      @click="onPick"
    >
      {{ knowledgeStore.uploadingDocument ? "Uploading…" : "Upload PDF" }}
    </button>
    <ErrorBanner
      v-if="knowledgeStore.uploadError"
      :message="knowledgeStore.uploadError"
      dismissible
      @dismiss="knowledgeStore.uploadError = null"
    />
  </div>
</template>

<style scoped>
.upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hidden-file {
  display: none;
}
.upload-btn {
  padding: 6px 14px;
  font-size: 13px;
  background: var(--accent, #4a9eff);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.upload-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
