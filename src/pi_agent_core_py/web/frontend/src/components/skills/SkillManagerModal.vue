<script setup lang="ts">
import { watch } from "vue"

import { useSkillStore } from "../../stores/skillStore"
import ErrorBanner from "../common/ErrorBanner.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import Modal from "../common/Modal.vue"
import SkillList from "./SkillList.vue"
import SkillUploadForm from "./SkillUploadForm.vue"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: "close"): void }>()

const skillStore = useSkillStore()

watch(
  () => props.open,
  (open) => {
    if (open) {
      skillStore.error = null
      skillStore.loadSkills()
    }
  },
)
</script>

<template>
  <Modal :open="open" title="Skills" data-testid="skills-modal" @close="emit('close')">
    <p class="modal-intro">
      Upload SKILL.md files and choose which skills to use for the current turn.
    </p>
    <SkillUploadForm />
    <ErrorBanner
      v-if="skillStore.error"
      :message="skillStore.error"
      dismissible
      @dismiss="skillStore.error = null"
    />
    <div v-if="skillStore.loading" class="loading-row">
      <LoadingSpinner :size="14" />
      <span>Loading…</span>
    </div>
    <SkillList />
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
