<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

withDefaults(defineProps<{ workspaceAttention?: boolean }>(), {
  workspaceAttention: false,
})

const emit = defineEmits<{ (event: "workspace-opened"): void }>()
const workspaceOpen = ref(false)
const shell = ref<HTMLElement | null>(null)

type ResizeColumn = "sidebar" | "workspace"

const SIDEBAR_MIN = 180
const SIDEBAR_MAX = 440
const WORKSPACE_MIN = 280
const WORKSPACE_MAX = 760
const MAIN_MIN = 420
const RESIZER_TOTAL = 12
const SIDEBAR_WIDTH_KEY = "pi-agent-layout-sidebar-width"
const WORKSPACE_WIDTH_KEY = "pi-agent-layout-workspace-width"

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

function storedWidth(key: string, fallback: number, minimum: number, maximum: number): number {
  try {
    const value = Number.parseInt(window.localStorage.getItem(key) ?? "", 10)
    return Number.isFinite(value) ? clamp(value, minimum, maximum) : fallback
  } catch {
    return fallback
  }
}

const sidebarWidth = ref(storedWidth(SIDEBAR_WIDTH_KEY, 260, SIDEBAR_MIN, SIDEBAR_MAX))
const workspaceWidth = ref(storedWidth(WORKSPACE_WIDTH_KEY, 380, WORKSPACE_MIN, WORKSPACE_MAX))
const resizing = ref<ResizeColumn | null>(null)
const gridStyle = computed<Record<string, string>>(() => ({
  "--sidebar-width": `${sidebarWidth.value}px`,
  "--workspace-width": `${workspaceWidth.value}px`,
}))

let resizeStartX = 0
let resizeStartWidth = 0

function availableMaximum(column: ResizeColumn): number {
  const total = shell.value?.clientWidth || window.innerWidth
  if (window.innerWidth <= 1050) return column === "sidebar" ? SIDEBAR_MAX : WORKSPACE_MAX
  if (column === "sidebar") {
    return Math.min(SIDEBAR_MAX, total - workspaceWidth.value - MAIN_MIN - RESIZER_TOTAL)
  }
  return Math.min(WORKSPACE_MAX, total - sidebarWidth.value - MAIN_MIN - RESIZER_TOTAL)
}

function setColumnWidth(column: ResizeColumn, nextWidth: number): void {
  const minimum = column === "sidebar" ? SIDEBAR_MIN : WORKSPACE_MIN
  const configuredMaximum = column === "sidebar" ? SIDEBAR_MAX : WORKSPACE_MAX
  const maximum = Math.max(minimum, Math.min(configuredMaximum, availableMaximum(column)))
  if (column === "sidebar") sidebarWidth.value = clamp(nextWidth, minimum, maximum)
  else workspaceWidth.value = clamp(nextWidth, minimum, maximum)
}

function persistWidths(): void {
  try {
    window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth.value))
    window.localStorage.setItem(WORKSPACE_WIDTH_KEY, String(workspaceWidth.value))
  } catch {
    // Layout persistence is optional (for example, storage can be disabled in private mode).
  }
}

function beginResize(column: ResizeColumn, event: PointerEvent): void {
  if (window.innerWidth <= 1050) return
  event.preventDefault()
  resizing.value = column
  resizeStartX = event.clientX
  resizeStartWidth = column === "sidebar" ? sidebarWidth.value : workspaceWidth.value
}

function onPointerMove(event: PointerEvent): void {
  if (!resizing.value) return
  setColumnWidth(resizing.value, resizeStartWidth + event.clientX - resizeStartX)
}

function stopResize(): void {
  if (!resizing.value) return
  resizing.value = null
  persistWidths()
}

function resizeWithKeyboard(column: ResizeColumn, event: KeyboardEvent): void {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return
  event.preventDefault()
  const current = column === "sidebar" ? sidebarWidth.value : workspaceWidth.value
  setColumnWidth(column, current + (event.key === "ArrowRight" ? 16 : -16))
  persistWidths()
}

function normalizeWidths(): void {
  if (window.innerWidth <= 1050) return
  setColumnWidth("sidebar", sidebarWidth.value)
  setColumnWidth("workspace", workspaceWidth.value)
}

function openWorkspace(): void {
  workspaceOpen.value = true
  emit("workspace-opened")
}

function closeWorkspace(): void {
  workspaceOpen.value = false
}

defineExpose({ openWorkspace, closeWorkspace })

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") closeWorkspace()
}

onMounted(() => {
  normalizeWidths()
  window.addEventListener("keydown", onKeydown)
  window.addEventListener("pointermove", onPointerMove)
  window.addEventListener("pointerup", stopResize)
  window.addEventListener("pointercancel", stopResize)
  window.addEventListener("resize", normalizeWidths)
})
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown)
  window.removeEventListener("pointermove", onPointerMove)
  window.removeEventListener("pointerup", stopResize)
  window.removeEventListener("pointercancel", stopResize)
  window.removeEventListener("resize", normalizeWidths)
})
</script>

<template>
  <div ref="shell" class="app-shell" :class="{ resizing }" :style="gridStyle">
    <aside class="app-sidebar" data-testid="session-sidebar">
      <slot name="sidebar" />
    </aside>
    <div
      class="column-resizer sidebar-resizer"
      role="separator"
      aria-label="Resize session sidebar"
      aria-orientation="vertical"
      tabindex="0"
      data-testid="sidebar-resizer"
      @pointerdown="beginResize('sidebar', $event)"
      @keydown="resizeWithKeyboard('sidebar', $event)"
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
    <div
      class="column-resizer workspace-resizer"
      role="separator"
      aria-label="Resize Workspace panel"
      aria-orientation="vertical"
      tabindex="0"
      data-testid="workspace-resizer"
      @pointerdown="beginResize('workspace', $event)"
      @keydown="resizeWithKeyboard('workspace', $event)"
    ></div>
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
  </div>
</template>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns:
    var(--sidebar-width) 6px var(--workspace-width) 6px
    minmax(0, 1fr);
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
.column-resizer {
  position: relative;
  z-index: 4;
  background: var(--sidebar-border);
  cursor: col-resize;
  touch-action: none;
  transition: background 120ms ease;
}
.column-resizer::after {
  position: absolute;
  inset: 0 -3px;
  content: "";
}
.column-resizer:hover,
.column-resizer:focus-visible,
.app-shell.resizing .column-resizer {
  background: var(--accent);
  outline: none;
}
.app-shell.resizing {
  cursor: col-resize;
  user-select: none;
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
  background: #fbfbfc;
}
.workspace-drawer-trigger,
.workspace-drawer-close,
.workspace-backdrop {
  display: none;
}

@media (max-width: 1050px) {
  .app-shell {
    grid-template-columns: 230px minmax(0, 1fr);
  }
  .column-resizer {
    display: none;
  }
  .app-sidebar {
    grid-column: 1;
  }
  .app-main {
    grid-column: 2;
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
  .app-main {
    grid-column: 1;
  }
}
</style>
