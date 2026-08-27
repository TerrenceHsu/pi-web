<script setup lang="ts">
import { ref } from "vue"

import { useSessionStore } from "../../stores/sessionStore"
import { useWikiStore } from "../../stores/wikiStore"
import type { WikiConversation } from "../../types/wiki"
import ChatPanel from "../chat/ChatPanel.vue"

const wikiStore = useWikiStore()
const sessionStore = useSessionStore()
const newTitle = ref("")

async function activateConversation(conversation: WikiConversation): Promise<void> {
  if (conversation.status !== "active") return
  wikiStore.selectedConversationId = conversation.id
  if (!sessionStore.setActiveSession(conversation.session_id)) {
    await sessionStore.loadSessions(conversation.session_id)
  }
}

async function createConversation(): Promise<void> {
  const title = newTitle.value.trim() || "New Knowledge conversation"
  const created = await wikiStore.createConversation(title)
  if (!created) return
  newTitle.value = ""
  await sessionStore.loadSessions(created.session_id)
}

async function toggleStatus(conversation: WikiConversation): Promise<void> {
  const next = conversation.status === "active" ? "archived" : "active"
  if (next === "archived" && !window.confirm("Archive this Knowledge conversation?")) return
  const changed = await wikiStore.setConversationStatus(conversation.id, next)
  if (changed && next === "active") await activateConversation({ ...conversation, status: next })
}
</script>

<template>
  <section class="conversations-view" data-testid="wiki-conversations-view">
    <aside class="conversation-list">
      <header>
        <div>
          <h2>Conversations</h2>
          <span>Independent Agent history for this Wiki Space</span>
        </div>
        <button
          type="button"
          :disabled="wikiStore.loadingConversations"
          @click="wikiStore.loadConversations()"
        >
          Refresh
        </button>
      </header>
      <form class="new-conversation" @submit.prevent="createConversation">
        <input
          v-model="newTitle"
          data-testid="wiki-new-conversation-title"
          maxlength="160"
          placeholder="Conversation title"
          :disabled="wikiStore.mutating"
        />
        <button type="submit" data-testid="wiki-create-conversation" :disabled="wikiStore.mutating">
          New
        </button>
      </form>

      <div
        v-if="wikiStore.loadingConversations && !wikiStore.conversations.length"
        class="conversation-empty"
      >
        Loading conversations…
      </div>
      <div v-else-if="!wikiStore.conversations.length" class="conversation-empty">
        Create the first Knowledge Agent conversation for this Space.
      </div>
      <template v-else>
        <article
          v-for="conversation in wikiStore.conversations"
          :key="conversation.id"
          :class="[
            'conversation-row',
            { selected: conversation.id === wikiStore.selectedConversationId },
          ]"
          data-testid="wiki-conversation-row"
          @click="activateConversation(conversation)"
        >
          <div>
            <strong>{{ conversation.title }}</strong>
            <span>
              {{ conversation.status }} ·
              {{ new Date(conversation.updated_at_ms).toLocaleString() }}
            </span>
          </div>
          <button
            type="button"
            :class="{ restore: conversation.status === 'archived' }"
            :disabled="wikiStore.mutating"
            @click.stop="toggleStatus(conversation)"
          >
            {{ conversation.status === "active" ? "Archive" : "Restore" }}
          </button>
        </article>
      </template>
    </aside>

    <main class="knowledge-chat">
      <div class="knowledge-policy">
        <strong>Knowledge mode</strong>
        <span>
          Wiki-only tools · no Workspace attachments or optional Skills · proposed edits require
          Change Set approval
        </span>
      </div>
      <ChatPanel v-if="wikiStore.selectedConversation?.status === 'active'" mode="knowledge" />
      <div v-else class="conversation-empty tall">
        Select an active conversation or create a new one.
      </div>
    </main>
  </section>
</template>

<style scoped>
.conversations-view {
  display: grid;
  height: 100%;
  min-height: 0;
  grid-template-columns: 310px minmax(0, 1fr);
}
.conversation-list {
  min-height: 0;
  overflow-y: auto;
  padding: 16px 12px;
  border-right: 1px solid var(--border);
  background: #f8fafc;
}
.conversation-list > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.conversation-list h2 {
  margin: 0;
  font-size: 17px;
}
.conversation-list header span {
  color: var(--muted);
  font-size: 10px;
}
.conversation-list header button {
  padding: 4px 7px;
  border-color: var(--border);
  background: #fff;
  font-size: 10px;
}
.new-conversation {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px;
  margin: 13px 0;
}
.new-conversation input {
  min-width: 0;
  padding: 7px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  font-size: 11px;
}
.conversation-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
}
.conversation-row:hover,
.conversation-row.selected {
  border-color: #60a5fa;
  background: #f8fbff;
}
.conversation-row > div {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  gap: 3px;
}
.conversation-row strong {
  overflow: hidden;
  color: var(--fg);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conversation-row span {
  color: var(--muted);
  font-size: 9px;
}
.conversation-row button {
  padding: 4px 6px;
  border-color: #fecaca;
  background: #fff;
  color: #b91c1c;
  font-size: 9px;
}
.conversation-row button.restore {
  border-color: #bbf7d0;
  color: #166534;
}
.knowledge-chat {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  background: var(--chat-bg);
}
.knowledge-policy {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 14px;
  border-bottom: 1px solid #bfdbfe;
  background: #eff6ff;
  color: #1e40af;
  font-size: 10px;
}
.knowledge-policy span {
  color: #475569;
}
.knowledge-chat :deep(.chat-panel) {
  min-height: 0;
  flex: 1;
}
.conversation-empty {
  padding: 22px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
  text-align: center;
}
.conversation-empty.tall {
  display: grid;
  height: 100%;
  place-items: center;
}
@media (max-width: 820px) {
  .conversations-view {
    display: block;
    overflow-y: auto;
  }
  .conversation-list {
    max-height: 330px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .knowledge-chat {
    height: 660px;
  }
}
</style>
