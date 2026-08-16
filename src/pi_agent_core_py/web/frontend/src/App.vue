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
import {
  pushSessionRoute,
  readSessionRoute,
  replaceSessionRoute,
} from "./utils/sessionRoute"

const authStore = useAuthStore()
const sessionStore = useSessionStore()
const chatStore = useChatStore()
const fileStore = useFileStore()
const skillStore = useSkillStore()
const mcpStore = useMcpStore()
const providerStore = useProviderStore()
const workspaceStarted = ref(false)
const workspaceLoading = ref(false)
const sessionCoordinatorReady = ref(false)
let activationVersion = 0
let activeUserId: string | null = null

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

  sessionCoordinatorReady.value = false
  try {
    const route = readSessionRoute()
    await sessionStore.loadSessions(route.sessionId)
    if (!sessionStore.activeSessionId) {
      try {
        await sessionStore.createNewSession()
      } catch (e) {
        console.error("createNewSession failed", e)
      }
    }

    const sid = sessionStore.activeSessionId
    replaceSessionRoute(sid)

    // 认证 Cookie 会随 WebSocket 握手发送。先连接，再恢复 active request；
    // recoverActiveRequestEvents 会把查询期间的 live event 合并去重。
    chatStore.connectEvents()
    await restoreWorkspaceSession(sid)
    sessionCoordinatorReady.value = true

    skillStore.loadSkills().catch((e) => console.error("loadSkills failed", e))
    mcpStore.loadServers().catch((e) => console.error("loadServers failed", e))
    mcpStore.loadTools().catch((e) => console.error("loadTools failed", e))
    providerStore.initialize().catch((e) => console.error("providerStore.initialize failed", e))

  } finally {
    workspaceLoading.value = false
  }
}

async function restoreWorkspaceSession(sessionId: string | null): Promise<void> {
  const version = ++activationVersion
  chatStore.resetForSession()
  chatStore.clearRegenerationForSessionSwitch()
  chatStore.setActiveSession(sessionId)
  fileStore.resetForSession()
  if (!sessionId) return

  await Promise.all([
    chatStore.loadMessages(sessionId),
    fileStore.loadFiles(sessionId),
  ])
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return

  const activeRequestId = await chatStore.findActiveRequest(sessionId)
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return
  if (!activeRequestId) {
    // 关闭“初次 load 与 active 查询之间刚好完成”的竞态。
    await Promise.all([
      chatStore.loadMessages(sessionId),
      fileStore.loadFiles(sessionId),
    ])
    return
  }

  chatStore.resumeActiveRequest(activeRequestId)
  await chatStore.recoverActiveRequestEvents(sessionId, activeRequestId)
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return
  void chatStore.pollRequestUntilTerminal(activeRequestId).then(() => {
    if (sessionStore.activeSessionId === sessionId) {
      void fileStore.loadFiles(sessionId)
    }
  })
}

function resetWorkspaceState(): void {
  activationVersion += 1
  sessionCoordinatorReady.value = false
  chatStore.resetWorkspace()
  fileStore.resetWorkspace()
  skillStore.resetWorkspace()
  mcpStore.resetWorkspace()
  providerStore.resetWorkspace()
  sessionStore.resetWorkspace()
  workspaceStarted.value = false
  workspaceLoading.value = false
}

watch(
  () => authStore.user,
  (user) => {
    if (user) {
      if (activeUserId !== null && activeUserId !== user.id) {
        resetWorkspaceState()
      }
      activeUserId = user.id
      void bootstrapWorkspace()
    } else {
      activeUserId = null
      resetWorkspaceState()
    }
  },
)

watch(
  () => sessionStore.activeSessionId,
  (sessionId) => {
    if (!sessionCoordinatorReady.value || !authStore.authenticated) return
    if (sessionId) {
      const route = readSessionRoute()
      if (route.sessionId !== sessionId) pushSessionRoute(sessionId)
    } else {
      replaceSessionRoute(null)
    }
    void restoreWorkspaceSession(sessionId)
  },
)

function handlePopState(): void {
  if (!sessionCoordinatorReady.value || !authStore.authenticated) return
  const route = readSessionRoute()
  if (
    route.sessionId &&
    sessionStore.sessions.some((session) => session.id === route.sessionId)
  ) {
    sessionStore.setActiveSession(route.sessionId)
    return
  }
  // 非法、已删除、或不属于当前账号的 Session 绝不发 API 请求。
  replaceSessionRoute(sessionStore.activeSessionId)
}

onMounted(async () => {
  window.addEventListener("popstate", handlePopState)
  await authStore.restoreSession()
})

onBeforeUnmount(() => {
  window.removeEventListener("popstate", handlePopState)
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
