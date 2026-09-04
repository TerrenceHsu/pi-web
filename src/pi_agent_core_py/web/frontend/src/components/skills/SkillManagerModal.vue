<script setup lang="ts">
import { watch } from "vue"

import { useSkillStore } from "../../stores/skillStore"
import { useSessionStore } from "../../stores/sessionStore"
import { useWorkspaceExtensionStore } from "../../stores/workspaceExtensionStore"
import ErrorBanner from "../common/ErrorBanner.vue"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import Modal from "../common/Modal.vue"
import SkillList from "./SkillList.vue"
import SkillUploadForm from "./SkillUploadForm.vue"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: "close"): void }>()

const skillStore = useSkillStore()
const sessionStore = useSessionStore()
const workspaceExtensionStore = useWorkspaceExtensionStore()

watch(
  () => props.open,
  (open) => {
    if (open) {
      skillStore.error = null
      skillStore.loadSkills()
      const sessionId = sessionStore.activeSessionId
      if (sessionId) {
        workspaceExtensionStore
          .load(sessionId)
          .then((result) => {
            if (sessionStore.activeSessionId === sessionId) {
              skillStore.setSelectedSkillNames(result.selected_skill_names)
            }
          })
          .catch(() => undefined)
      }
    }
  },
)
</script>

<template>
  <Modal :open="open" title="Skills" data-testid="skills-modal" @close="emit('close')">
    <p class="modal-intro">Manage the global Skill catalog and choose Skills for this Workspace.</p>
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
