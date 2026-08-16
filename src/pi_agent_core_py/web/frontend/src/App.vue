<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import AppShell from "./components/layout/AppShell.vue"
import SessionSidebar from "./components/layout/SessionSidebar.vue"
import ChatPanel from "./components/chat/ChatPanel.vue"
import LoginPage from "./components/auth/LoginPage.vue"
import { useAuthStore } from "./stores/authStore"
import { useChatStore } from "./stores/chatStore"
import { useFileStore } from "./stores/fileStore"
import { useMcpStore } from "./stores/mcpStore"
import { useProviderStore } from "./stores/providerStore"
import { useSessionStore } from "./stores/sessionStore"
import { useSkillStore } from "./stores/skillStore"

const authStore = useAuthStore()
const sessionStore = useSessionStore()
const chatStore = useChatStore()
const fileStore = useFileStore()
const skillStore = useSkillStore()
const mcpStore = useMcpStore()
const providerStore = useProviderStore()
const workspaceStarted = ref(false)
const workspaceLoading = ref(false)

// Provider Binding 跟随 active session 切换——协调只发生在 App.vue。
// 切到 null（无 session）→ 清状态；切到 id → 异步刷新 binding。
// stale-response 防护在 store 的 token 机制里处理。
watch(
  () => sessionStore.activeSessionId,
  (sessionId) => {
    if (sessionId) {
      void providerStore.refreshForSession(sessionId)
    } else {
      providerStore.clearSessionState()
    }
  },
  { immediate: true },
)

async function bootstrapWorkspace(): Promise<void> {
  if (workspaceStarted.value) return
  workspaceStarted.value = true
  workspaceLoading.value = true

  // 1. sessions
  try {
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
      await Promise.all([chatStore.loadMessages(sid), fileStore.loadFiles(sid)])

      // P1-B3-3: 查 active request——页面刷新后恢复运行中 prompt
      const activeReqId = await chatStore.findActiveRequest(sid)
      if (activeReqId) {
        // 有未完成 request——恢复 currentRequestId + sending/streaming
        // WS 连接后 reconnect replay 会补播该 request 的事件
        chatStore.resumeActiveRequest(activeReqId)
        if (chatStore.checkpointing) {
          void chatStore.pollRequestUntilTerminal(activeReqId).then(() => {
            if (sessionStore.activeSessionId === sid) {
              void fileStore.loadFiles(sid)
            }
          })
        }
      }
    }

    // 3. 当前账号工作区的 skills / mcp / providers
    skillStore.loadSkills().catch((e) => console.error("loadSkills failed", e))
    mcpStore.loadServers().catch((e) => console.error("loadServers failed", e))
    mcpStore.loadTools().catch((e) => console.error("loadTools failed", e))
    providerStore.initialize().catch((e) => console.error("providerStore.initialize failed", e))

    // 4. 认证 Cookie 会随 WebSocket 握手发送
    chatStore.connectEvents()
  } finally {
    workspaceLoading.value = false
  }
}

watch(
  () => authStore.user,
  (user) => {
    if (user) {
      void bootstrapWorkspace()
    } else {
      workspaceStarted.value = false
      chatStore.disconnectEvents()
    }
  },
)

onMounted(async () => {
  await authStore.restoreSession()
})

onBeforeUnmount(() => {
  chatStore.disconnectEvents()
})
</script>

<template>
  <div v-if="authStore.status === 'checking'" class="auth-loading" data-testid="auth-loading">
    Checking session…
  </div>
  <LoginPage v-else-if="!authStore.authenticated" />
  <div v-else-if="workspaceLoading" class="auth-loading" data-testid="workspace-loading">
    Loading {{ authStore.user?.name }} workspace…
  </div>
  <AppShell v-else>
    <template #sidebar>
      <SessionSidebar />
    </template>
    <template #main>
      <ChatPanel />
    </template>
  </AppShell>
</template>

<style scoped>
.auth-loading {
  min-height: 100vh;
  display: grid;
  place-items: center;
  color: var(--muted);
  background: var(--bg);
}
</style>
