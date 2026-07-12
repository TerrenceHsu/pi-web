<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue"

import AppShell from "./components/layout/AppShell.vue"
import SessionSidebar from "./components/layout/SessionSidebar.vue"
import ChatPanel from "./components/chat/ChatPanel.vue"
import { useChatStore } from "./stores/chatStore"
import { useFileStore } from "./stores/fileStore"
import { useMcpStore } from "./stores/mcpStore"
import { useSessionStore } from "./stores/sessionStore"
import { useSkillStore } from "./stores/skillStore"

const sessionStore = useSessionStore()
const chatStore = useChatStore()
const fileStore = useFileStore()
const skillStore = useSkillStore()
const mcpStore = useMcpStore()

onMounted(async () => {
  // 1. sessions
  await sessionStore.loadSessions()
  if (!sessionStore.activeSessionId) {
    try {
      await sessionStore.createNewSession()
    } catch (e) {
      console.error("createNewSession failed", e)
    }
  }

  // 2. 当前 session 的 messages / files
  const sid = sessionStore.activeSessionId
  if (sid) {
    // P1-B2.1: 同步 chatStore.activeSessionId——handleEvent 用它做 session 过滤
    chatStore.setActiveSession(sid)
    await Promise.all([
      chatStore.loadMessages(sid),
      fileStore.loadFiles(sid),
    ])
  }

  // 3. skills / mcp——失败不阻塞主聊天
  skillStore.loadSkills().catch((e) => console.error("loadSkills failed", e))
  mcpStore.loadServers().catch((e) => console.error("loadServers failed", e))
  mcpStore.loadTools().catch((e) => console.error("loadTools failed", e))

  // 4. WS 事件流
  chatStore.connectEvents()
})

onBeforeUnmount(() => {
  chatStore.disconnectEvents()
})
</script>

<template>
  <AppShell>
    <template #sidebar>
      <SessionSidebar />
    </template>
    <template #main>
      <ChatPanel />
    </template>
  </AppShell>
</template>
