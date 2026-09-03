<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"

import AppShell from "./components/layout/AppShell.vue"
import SessionSidebar from "./components/layout/SessionSidebar.vue"
import ChatPanel from "./components/chat/ChatPanel.vue"
import WorkspacePanel from "./components/workspace/WorkspacePanel.vue"
import WikiWorkspace from "./components/wiki/WikiWorkspace.vue"
import LoginPage from "./components/auth/LoginPage.vue"
import TelemetryDashboard from "./components/telemetry/TelemetryDashboard.vue"
import { useAuthStore } from "./stores/authStore"
import { useChatStore } from "./stores/chatStore"
import { useCodingSandboxStore } from "./stores/codingSandboxStore"
import { useContextBudgetStore } from "./stores/contextBudgetStore"
import { useFileStore } from "./stores/fileStore"
import { useMcpStore } from "./stores/mcpStore"
import { useProviderStore } from "./stores/providerStore"
import { useSessionStore } from "./stores/sessionStore"
import { useSkillStore } from "./stores/skillStore"
import { useTelemetryStore } from "./stores/telemetryStore"
import { useWikiStore } from "./stores/wikiStore"
import {
  pushKnowledgeRoute,
  pushTelemetryRoute,
  readAppView,
  type AppView,
} from "./utils/appRoute"
import { pushSessionRoute, readSessionRoute, replaceSessionRoute } from "./utils/sessionRoute"

const authStore = useAuthStore()
const sessionStore = useSessionStore()
const chatStore = useChatStore()
const codingSandboxStore = useCodingSandboxStore()
const contextBudgetStore = useContextBudgetStore()
const fileStore = useFileStore()
const skillStore = useSkillStore()
const mcpStore = useMcpStore()
const providerStore = useProviderStore()
const wikiStore = useWikiStore()
const telemetryStore = useTelemetryStore()
const activeView = ref<AppView>(readAppView())
const appShellRef = ref<InstanceType<typeof AppShell> | null>(null)
const workspaceStarted = ref(false)
const workspaceLoading = ref(false)
const sessionCoordinatorReady = ref(false)
let activationVersion = 0
let activeUserId: string | null = null
const workspaceAttention = computed(() => {
  const sid = sessionStore.activeSessionId
  return sid
    ? !!fileStore.latestArtifactBySession[sid]?.unseen ||
        ["awaiting_approval", "publish_conflict"].includes(
          codingSandboxStore.operation?.status ?? "",
        )
    : false
})

function acknowledgeWorkspace(): void {
  const sid = sessionStore.activeSessionId
  if (sid) fileStore.acknowledgeArtifact(sid)
}

function openWorkspaceResults(): void {
  appShellRef.value?.openWorkspace()
}

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
    const route =
      activeView.value === "chat" ? readSessionRoute() : { requested: false, sessionId: null }
    await sessionStore.loadSessions(route.sessionId)
    if (!sessionStore.activeSessionId) {
      try {
        await sessionStore.createNewSession()
      } catch (e) {
        console.error("createNewSession failed", e)
      }
    }

    const sid = sessionStore.activeSessionId
    if (activeView.value === "chat") replaceSessionRoute(sid)

    // 认证 Cookie 会随 WebSocket 握手发送。先连接，再恢复 active request；
    // recoverActiveRequestEvents 会把查询期间的 live event 合并去重。
    chatStore.connectEvents()
    codingSandboxStore.connectEvents()
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
  contextBudgetStore.resetForSession()
  fileStore.resetForSession()
  if (!sessionId) {
    await codingSandboxStore.restoreSession(null)
    return
  }

  await Promise.all([
    chatStore.loadMessages(sessionId),
    fileStore.loadFiles(sessionId),
    contextBudgetStore.load(sessionId),
    codingSandboxStore.restoreSession(sessionId),
  ])
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return

  const activeRequestId = await chatStore.findActiveRequest(sessionId)
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return
  if (!activeRequestId) {
    // 关闭“初次 load 与 active 查询之间刚好完成”的竞态。
    await Promise.all([chatStore.loadMessages(sessionId), fileStore.loadFiles(sessionId)])
    return
  }

  chatStore.resumeActiveRequest(activeRequestId)
  await chatStore.recoverActiveRequestEvents(sessionId, activeRequestId)
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return
  await chatStore.loadPendingApprovals(sessionId, activeRequestId)
  if (version !== activationVersion || sessionStore.activeSessionId !== sessionId) return
  void chatStore.pollRequestUntilTerminal(activeRequestId).then(() => {
    if (sessionStore.activeSessionId === sessionId) {
      void fileStore.loadFiles(sessionId)
      void contextBudgetStore.load(sessionId)
    }
  })
}

function resetWorkspaceState(): void {
  activationVersion += 1
  sessionCoordinatorReady.value = false
  chatStore.resetWorkspace()
  contextBudgetStore.resetWorkspace()
  fileStore.resetWorkspace()
  skillStore.resetWorkspace()
  mcpStore.resetWorkspace()
  providerStore.resetWorkspace()
  codingSandboxStore.resetWorkspace()
  wikiStore.reset()
  telemetryStore.reset()
  sessionStore.resetWorkspace()
  workspaceStarted.value = false
  workspaceLoading.value = false
}

watch(
  () => authStore.user,
  (user) => {
    if (user) {
      if (activeView.value === "telemetry" && !user.is_admin) {
        activeView.value = "chat"
        replaceSessionRoute(null)
      }
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
    if (activeView.value === "chat") {
      if (sessionId) {
        const route = readSessionRoute()
        if (route.sessionId !== sessionId) pushSessionRoute(sessionId)
      } else {
        replaceSessionRoute(null)
      }
    }
    void restoreWorkspaceSession(sessionId)
  },
)

function handlePopState(): void {
  if (!sessionCoordinatorReady.value || !authStore.authenticated) return
  const nextView = readAppView()
  activeView.value = nextView
  if (nextView === "knowledge") return
  if (nextView === "telemetry") {
    if (authStore.user?.is_admin) return
    activeView.value = "chat"
  }
  const route = readSessionRoute()
  if (route.sessionId && sessionStore.sessions.some((session) => session.id === route.sessionId)) {
    sessionStore.setActiveSession(route.sessionId)
    return
  }
  // 非法、已删除、或不属于当前账号的 Session 绝不发 API 请求。
  replaceSessionRoute(sessionStore.activeSessionId)
}

function openKnowledge(): void {
  pushKnowledgeRoute()
  activeView.value = "knowledge"
}

function openTelemetry(): void {
  if (!authStore.user?.is_admin) return
  pushTelemetryRoute()
  activeView.value = "telemetry"
}

function closeKnowledge(): void {
  activeView.value = "chat"
  const sessionId = sessionStore.activeSessionId
  if (sessionId) pushSessionRoute(sessionId)
  else replaceSessionRoute(null)
}

function closeTelemetry(): void {
  activeView.value = "chat"
  const sessionId = sessionStore.activeSessionId
  if (sessionId) pushSessionRoute(sessionId)
  else replaceSessionRoute(null)
}

onMounted(async () => {
  window.addEventListener("popstate", handlePopState)
  await authStore.restoreSession()
})

onBeforeUnmount(() => {
  window.removeEventListener("popstate", handlePopState)
  chatStore.disconnectEvents()
  codingSandboxStore.disconnectEvents()
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
  <WikiWorkspace v-else-if="activeView === 'knowledge'" @close="closeKnowledge" />
  <TelemetryDashboard
    v-else-if="activeView === 'telemetry' && authStore.user?.is_admin"
    @close="closeTelemetry"
  />
  <AppShell
    v-else
    ref="appShellRef"
    :workspace-attention="workspaceAttention"
    @workspace-opened="acknowledgeWorkspace"
  >
    <template #sidebar>
      <SessionSidebar
        @open-knowledge="openKnowledge"
        @open-telemetry="openTelemetry"
      />
    </template>
    <template #main>
      <ChatPanel @open-workspace="openWorkspaceResults" />
    </template>
    <template #workspace>
      <WorkspacePanel />
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
