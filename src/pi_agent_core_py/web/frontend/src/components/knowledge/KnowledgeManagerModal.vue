<script setup lang="ts">
import { onBeforeUnmount, watch } from "vue"

import { useChatStore } from "../../stores/chatStore"
import { useKnowledgeStore } from "../../stores/knowledgeStore"
import { useSessionStore } from "../../stores/sessionStore"
import ErrorBanner from "../common/ErrorBanner.vue"
import Modal from "../common/Modal.vue"
import DocumentList from "./DocumentList.vue"
import DocumentUpload from "./DocumentUpload.vue"
import KnowledgeSearchPanel from "./KnowledgeSearchPanel.vue"
import LibraryList from "./LibraryList.vue"
import SessionBindingToggle from "./SessionBindingToggle.vue"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: "close"): void }>()

const knowledgeStore = useKnowledgeStore()
const sessionStore = useSessionStore()
const chatStore = useChatStore()

// Current chat session id — used for binding endpoints. Pulled fresh each open.
function currentSessionId(): string | null {
  return sessionStore.activeSessionId ?? null
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      const sid = currentSessionId()
      await knowledgeStore.onModalOpen(sid)
    } else {
      knowledgeStore.onModalClose()
    }
  },
)

// Stop polling + abort in-flight if the modal disappears without `open=false`.
onBeforeUnmount(() => {
  knowledgeStore.onModalClose()
})

// Stop active request mid-flight? No — Knowledge modal is independent of chat
// request lifecycle (user can manage libraries while LLM is responding).
// `chatStore` import is for future binding-status surfacing only.
void chatStore
</script>

<template>
  <Modal
    :open="open"
    title="Knowledge"
    width="min(1100px, 94vw)"
    data-testid="knowledge-modal"
    @close="emit('close')"
  >
    <p class="modal-intro">
      Manage libraries and PDF documents. Search a library's indexed content.
      Bind a library to the current chat session to enable Agent retrieval.
    </p>

    <ErrorBanner
      v-if="knowledgeStore.error"
      :message="knowledgeStore.error"
      dismissible
      @dismiss="knowledgeStore.error = null"
    />

    <div class="knowledge-layout">
      <LibraryList class="knowledge-left" />

      <div class="knowledge-right">
        <template v-if="knowledgeStore.selectedLibrary">
          <div class="library-header">
            <div class="library-name">
              {{ knowledgeStore.selectedLibrary.name }}
            </div>
            <SessionBindingToggle
              :session-id="currentSessionId()"
              :library-id="knowledgeStore.selectedLibrary.id"
            />
          </div>

          <DocumentUpload />

          <DocumentList />

          <KnowledgeSearchPanel />
        </template>
        <template v-else>
          <div class="no-selection">
            Select or create a library to manage documents.
          </div>
        </template>
      </div>
    </div>
  </Modal>
</template>

<style scoped>
.modal-intro {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 13px;
}
.knowledge-layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  min-height: 480px;
}
.knowledge-left {
  border-right: 1px solid var(--border);
  padding-right: 12px;
  overflow-y: auto;
  max-height: 70vh;
}
.knowledge-right {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.library-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.library-name {
  font-weight: 600;
  font-size: 16px;
  color: var(--fg);
  word-break: break-all;
}
.no-selection {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--muted);
  font-size: 14px;
  text-align: center;
  padding: 48px 16px;
}
</style>
