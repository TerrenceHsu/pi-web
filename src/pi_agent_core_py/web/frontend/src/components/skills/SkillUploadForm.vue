<script setup lang="ts">
import { computed, ref } from "vue"

import { ApiError } from "../../api/client"
import { useSkillStore } from "../../stores/skillStore"
import LoadingSpinner from "../common/LoadingSpinner.vue"

const skillStore = useSkillStore()
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const localError = ref<string | null>(null)

const errorText = computed(() => localError.value || skillStore.error)

async function upload() {
  const input = fileInput.value
  if (!input || !input.files || input.files.length === 0) return
  localError.value = null
  uploading.value = true
  try {
    await skillStore.uploadSkill(input.files)
    input.value = ""
  } catch (e: any) {
    localError.value =
      e instanceof ApiError ? e.detail : String(e?.message ?? e)
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <div class="skill-upload-form">
    <div class="upload-row">
      <input
        ref="fileInput"
        type="file"
        multiple
        accept=".md,text/markdown"
        data-testid="skill-upload-input"
      />
      <button
        type="button"
        class="primary"
        :disabled="uploading"
        @click="upload"
      >
        <LoadingSpinner v-if="uploading" :size="12" />
        <span v-else>Upload SKILL.md</span>
      </button>
    </div>
    <p v-if="errorText" class="upload-error">{{ errorText }}</p>
    <p class="upload-hint">
      Only <code>.md</code> with YAML frontmatter (<code>name</code> required). Max 256KB each.
    </p>
  </div>
</template>

<style scoped>
.skill-upload-form {
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: 8px;
  background: var(--bg);
}
.upload-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.upload-row input[type="file"] {
  flex: 1;
  font-size: 12px;
}
.upload-row button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  font-size: 12px;
}
.upload-error {
  margin: 6px 0 0;
  color: var(--danger);
  font-size: 12px;
}
.upload-hint {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 11px;
}
</style>
