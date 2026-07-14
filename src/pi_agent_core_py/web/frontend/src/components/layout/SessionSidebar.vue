<script setup lang="ts">
import { computed, ref } from "vue"

import { useChatStore } from "../../stores/chatStore"
import { useFileStore } from "../../stores/fileStore"
import { useSessionStore } from "../../stores/sessionStore"
import LoadingSpinner from "../common/LoadingSpinner.vue"
import MCPManagerModal from "../mcp/MCPManagerModal.vue"
import SkillManagerModal from "../skills/SkillManagerModal.vue"

const sessionStore = useSessionStore()
const chatStore = useChatStore()
const fileStore = useFileStore()

const sessions = computed(() => sessionStore.sessions)
const activeId = computed(() => sessionStore.activeSessionId)

const skillsOpen = ref(false)
const mcpOpen = ref(false)

function formatTime(ts?: number): string {
  if (!ts) return ""
  const d = new Date(ts)
  // 简短：MM-DD HH:mm
  const mm = String(d.getMonth() + 1).padStart(2, "0")
  const dd = String(d.getDate()).padStart(2, "0")
  const hh = String(d.getHours()).padStart(2, "0")
  const mi = String(d.getMinutes()).padStart(2, "0")
  return `${mm}-${dd} ${hh}:${mi}`
}

async function activateSession(id: string) {
  if (id === activeId.value) return
  sessionStore.setActiveSession(id)
  // P1-B2.1: 同步 chatStore.activeSessionId + reset turn cards
  chatStore.setActiveSession(id)
  chatStore.resetForSession()
  await Promise.all([
    chatStore.loadMessages(id),
    fileStore.loadFiles(id),
  ])
  fileStore.resetForSession()
}

async function newChat() {
  try {
    const s = await sessionStore.createNewSession()
    if (s) {
      // P1-B2.1: 新建 session 后同步 chatStore.activeSessionId
      chatStore.setActiveSession(s.id)
      chatStore.resetForSession()
      fileStore.resetForSession()
      await Promise.all([
        chatStore.loadMessages(s.id),
        fileStore.loadFiles(s.id),
      ])
    }
  } catch (e) {
    console.error("newChat failed", e)
  }
}

async function renameSession(id: string, currentTitle: string) {
  const title = window.prompt("Rename session", currentTitle)
  if (!title || !title.trim()) return
  try {
    await sessionStore.renameSession(id, title.trim())
  } catch (e) {
    console.error("renameSession failed", e)
  }
}

async function deleteSession(id: string) {
  if (!window.confirm("Delete this session? Messages and uploaded files will be lost.")) return
  try {
    await sessionStore.deleteSession(id)
    // 删的是 active → 切到剩余第一个；没有则创建新的
    if (!sessionStore.activeSessionId) {
      await newChat()
    } else if (sessionStore.activeSessionId) {
      // P1-B2.1: 同步 chatStore.activeSessionId 到新的 active session
      chatStore.setActiveSession(sessionStore.activeSessionId)
      chatStore.resetForSession()
      fileStore.resetForSession()
      await Promise.all([
        chatStore.loadMessages(sessionStore.activeSessionId),
        fileStore.loadFiles(sessionStore.activeSessionId),
      ])
    }
  } catch (e) {
    console.error("deleteSession failed", e)
  }
}

async function exportSession(id: string) {
  try {
    const { exportMarkdown } = await import("../../api/sessions")
    const { downloadBlob } = await import("../../api/client")
    const { blob, filename } = await exportMarkdown(id)
    downloadBlob(blob, filename || "chat-export.md")
  } catch (e: any) {
    console.error("exportSession failed", e)
    chatStore.error = e?.message ? `Export failed: ${e.message}` : "Export failed"
  }
}
</script>

<template>
  <div class="sidebar">
    <div class="sidebar-header">
      <span class="brand-dot"></span>
      <span class="brand-name">pi-chat</span>
    </div>

    <div class="sidebar-actions">
      <button
        class="new-chat-btn"
        data-testid="new-chat-button"
        :disabled="sessionStore.loading"
        @click="newChat"
      >
        <span class="plus">+</span>
        New chat
      </button>
    </div>

    <div class="sidebar-section-title">Sessions</div>
    <div class="session-list">
      <div v-if="sessionStore.loading && sessions.length === 0" class="loading-row">
        <LoadingSpinner :size="14" />
        <span>Loading…</span>
      </div>
      <div
        v-for="s in sessions"
        :key="s.id"
        :class="['session-item', { active: s.id === activeId }]"
        data-testid="session-item"
        @click="activateSession(s.id)"
      >
        <div class="session-item-main">
          <div class="session-item-title">{{ s.title || "(untitled)" }}</div>
          <div class="session-item-meta">{{ formatTime(s.updated_at) }}</div>
        </div>
        <div class="session-item-actions" @click.stop>
          <button
            class="icon-btn"
            data-testid="session-export-btn"
            title="Export Markdown"
            aria-label="Export Markdown"
            @click="exportSession(s.id)"
          >⤓</button>
          <button
            class="icon-btn"
            data-testid="session-rename-btn"
            title="Rename"
            aria-label="Rename"
            @click="renameSession(s.id, s.title || '')"
          >✎</button>
          <button
            class="icon-btn danger"
            data-testid="session-delete-btn"
            title="Delete"
            aria-label="Delete"
            @click="deleteSession(s.id)"
          >×</button>
        </div>
      </div>
      <div v-if="!sessionStore.loading && sessions.length === 0" class="empty-row">
        No sessions yet
      </div>
    </div>

    <div class="sidebar-spacer"></div>

    <div class="sidebar-footer">
      <button
        class="footer-btn"
        data-testid="skills-button"
        title="Skills manager"
        @click="skillsOpen = true"
      >Skills</button>
      <button
        class="footer-btn"
        data-testid="mcp-button"
        title="MCP manager"
        @click="mcpOpen = true"
      >MCP</button>
    </div>

    <SkillManagerModal :open="skillsOpen" @close="skillsOpen = false" />
    <MCPManagerModal :open="mcpOpen" @close="mcpOpen = false" />
  </div>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 12px 8px;
  user-select: none;
}
.sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px 12px;
}
.brand-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
}
.brand-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--fg);
}
.sidebar-actions {
  padding: 0 4px 8px;
}
.new-chat-btn {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 12px;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
}
.new-chat-btn:hover:not(:disabled) {
  background: var(--accent-hover);
}
.new-chat-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.plus {
  font-size: 16px;
  line-height: 1;
}
.sidebar-section-title {
  padding: 8px 12px 4px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.loading-row,
.empty-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--muted);
}
.session-item {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  margin: 2px 4px;
  border-radius: 6px;
  cursor: pointer;
  gap: 4px;
}
.session-item:hover {
  background: var(--border);
}
.session-item.active {
  background: var(--border-strong);
}
.session-item-main {
  flex: 1;
  min-width: 0;
}
.session-item-title {
  font-size: 13px;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-item-meta {
  font-size: 11px;
  color: var(--muted);
  margin-top: 2px;
}
.session-item-actions {
  display: none;
  gap: 2px;
}
.session-item:hover .session-item-actions,
.session-item.active .session-item-actions {
  display: flex;
}
.icon-btn {
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 14px;
  line-height: 1;
}
.icon-btn:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--fg);
}
.icon-btn.danger:hover {
  color: var(--danger);
}
.sidebar-spacer {
  flex: 0 0 auto;
}
.sidebar-footer {
  display: flex;
  gap: 6px;
  padding: 8px 4px 4px;
  border-top: 1px solid var(--sidebar-border);
}
.footer-btn {
  flex: 1;
  padding: 6px 8px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  color: var(--muted);
}
.footer-btn:hover {
  background: var(--border);
  color: var(--fg);
}
</style>
