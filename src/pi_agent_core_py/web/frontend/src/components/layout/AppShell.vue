<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue"

withDefaults(defineProps<{ workspaceAttention?: boolean }>(), {
  workspaceAttention: false,
})

const emit = defineEmits<{ (event: "workspace-opened"): void }>()
const workspaceOpen = ref(false)

function openWorkspace(): void {
  workspaceOpen.value = true
  emit("workspace-opened")
}

function closeWorkspace(): void {
  workspaceOpen.value = false
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") closeWorkspace()
}

onMounted(() => window.addEventListener("keydown", onKeydown))
onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown))
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar" data-testid="session-sidebar">
      <slot name="sidebar" />
    </aside>
    <main class="app-main" data-testid="chat-panel">
      <slot name="main" />
    </main>

    <button
      type="button"
      class="workspace-drawer-trigger"
      :class="{ attention: workspaceAttention }"
      :aria-expanded="workspaceOpen"
      aria-controls="workspace-sidebar"
      data-testid="workspace-drawer-trigger"
      @click="openWorkspace"
    >
      <span aria-hidden="true">▣</span>
      Results
      <span v-if="workspaceAttention" class="attention-dot" aria-label="New Agent result"></span>
    </button>
    <div
      v-if="workspaceOpen"
      class="workspace-backdrop"
      data-testid="workspace-drawer-backdrop"
      @click="closeWorkspace"
    ></div>
    <aside
      id="workspace-sidebar"
      :class="['app-workspace', { open: workspaceOpen }]"
      data-testid="workspace-sidebar"
    >
      <button
        type="button"
        class="workspace-drawer-close"
        aria-label="Close Workspace results"
        @click="closeWorkspace"
      >
        ×
      </button>
      <slot name="workspace" />
    </aside>
  </div>
</template>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 380px;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background: var(--bg);
}
.app-sidebar {
  display: flex;
  overflow: hidden;
  flex-direction: column;
  border-right: 1px solid var(--sidebar-border);
  background: var(--sidebar-bg);
}
.app-main {
  display: flex;
  overflow: hidden;
  flex-direction: column;
  background: var(--chat-bg);
}
.app-workspace {
  position: relative;
  min-width: 0;
  overflow: hidden;
  border-left: 1px solid var(--sidebar-border);
  background: #fbfbfc;
}
.workspace-drawer-trigger,
.workspace-drawer-close,
.workspace-backdrop {
  display: none;
}

@media (max-width: 1280px) {
  .app-shell {
    grid-template-columns: 230px minmax(0, 1fr) 340px;
  }
}

@media (max-width: 1050px) {
  .app-shell {
    grid-template-columns: 230px minmax(0, 1fr);
  }
  .app-workspace {
    position: fixed;
    z-index: 42;
    top: 0;
    right: 0;
    width: min(390px, 92vw);
    height: 100vh;
    transform: translateX(102%);
    border-left: 1px solid var(--border-strong);
    box-shadow: -16px 0 40px rgba(15, 23, 42, 0.14);
    transition: transform 160ms ease;
  }
  .app-workspace.open {
    transform: translateX(0);
  }
  .workspace-drawer-trigger {
    position: fixed;
    z-index: 30;
    right: 14px;
    bottom: 84px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 10px;
    border-color: var(--border-strong);
    border-radius: 18px;
    box-shadow: 0 8px 22px rgba(15, 23, 42, 0.14);
    font-size: 11px;
  }
  .workspace-drawer-trigger.attention {
    border-color: #93c5fd;
    background: #eff6ff;
    color: #1d4ed8;
  }
  .attention-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #2563eb;
  }
  .workspace-drawer-close {
    position: absolute;
    z-index: 2;
    top: 7px;
    right: 7px;
    display: grid;
    width: 26px;
    height: 26px;
    place-items: center;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--muted);
    font-size: 17px;
  }
  .workspace-backdrop {
    position: fixed;
    z-index: 41;
    inset: 0;
    display: block;
    background: rgba(15, 23, 42, 0.2);
  }
}

@media (max-width: 720px) {
  .app-shell {
    grid-template-columns: 1fr;
  }
  .app-sidebar {
    display: none;
  }
}
</style>
