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
const MAIN_MIN = 320
const RESIZER_TOTAL = 16
const DESKTOP_MIN = 1050
const SIDEBAR_WIDTH_KEY = "pi-agent-layout-sidebar-width"
const WORKSPACE_WIDTH_KEY = "pi-agent-layout-workspace-width"

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

function storedWidth(key: string, fallback: number, minimum: number, maximum: number): number {
  try {
    const stored = window.localStorage.getItem(key)
    const value = stored?.trim() ? Number(stored) : Number.NaN
    return Number.isFinite(value) ? clamp(value, minimum, maximum) : fallback
  } catch {
    return fallback
  }
}

// Store the user's preferred sizes separately from the fitted sizes. Resizing the
// window must not overwrite a wide-screen preference with a temporary clamp.
const preferredSidebarWidth = ref(storedWidth(SIDEBAR_WIDTH_KEY, 260, SIDEBAR_MIN, SIDEBAR_MAX))
const preferredWorkspaceWidth = ref(
  storedWidth(WORKSPACE_WIDTH_KEY, 380, WORKSPACE_MIN, Number.MAX_SAFE_INTEGER),
)
const viewportWidth = ref(window.innerWidth)
const desktop = computed(() => viewportWidth.value > DESKTOP_MIN)
const sidebarWidth = computed(() =>
  clamp(
    preferredSidebarWidth.value,
    SIDEBAR_MIN,
    Math.max(
      SIDEBAR_MIN,
      Math.min(SIDEBAR_MAX, viewportWidth.value - WORKSPACE_MIN - MAIN_MIN - RESIZER_TOTAL),
    ),
  ),
)
const workspaceMaximum = computed(() =>
  Math.max(WORKSPACE_MIN, viewportWidth.value - sidebarWidth.value - MAIN_MIN - RESIZER_TOTAL),
)
const workspaceWidth = computed(() =>
  clamp(preferredWorkspaceWidth.value, WORKSPACE_MIN, workspaceMaximum.value),
)
const sidebarMaximum = computed(() =>
  Math.max(
    SIDEBAR_MIN,
    Math.min(SIDEBAR_MAX, viewportWidth.value - workspaceWidth.value - MAIN_MIN - RESIZER_TOTAL),
  ),
)
const mainWidth = computed(() =>
  Math.max(0, viewportWidth.value - sidebarWidth.value - workspaceWidth.value - RESIZER_TOTAL),
)
const resizing = ref<ResizeColumn | null>(null)
const gridStyle = computed<Record<string, string>>(() => ({
  "--sidebar-width": `${sidebarWidth.value}px`,
  "--workspace-width": `${workspaceWidth.value}px`,
}))

let drag: {
  column: ResizeColumn
  pointerId: number
  target: HTMLElement
  startX: number
  startWidth: number
} | null = null

function setColumnWidth(column: ResizeColumn, nextWidth: number): void {
  if (!Number.isFinite(nextWidth)) return
  const minimum = column === "sidebar" ? SIDEBAR_MIN : WORKSPACE_MIN
  const maximum = column === "sidebar" ? sidebarMaximum.value : workspaceMaximum.value
  const fitted = Math.round(clamp(nextWidth, minimum, maximum))
  if (column === "sidebar") preferredSidebarWidth.value = fitted
  else preferredWorkspaceWidth.value = fitted
}

function persistWidths(): void {
  try {
    window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(preferredSidebarWidth.value))
    window.localStorage.setItem(WORKSPACE_WIDTH_KEY, String(preferredWorkspaceWidth.value))
  } catch {
    // Layout persistence is optional (for example, storage can be disabled in private mode).
  }
}

function beginResize(column: ResizeColumn, event: PointerEvent): void {
  if (!desktop.value || drag || event.button !== 0 || event.isPrimary === false) return
  event.preventDefault()
  const target = event.currentTarget as HTMLElement
  target.focus({ preventScroll: true })
  drag = {
    column,
    pointerId: event.pointerId,
    target,
    startX: event.clientX,
    startWidth: column === "sidebar" ? sidebarWidth.value : workspaceWidth.value,
  }
  resizing.value = column
  try {
    target.setPointerCapture(event.pointerId)
  } catch {
    // Window listeners also support environments without pointer capture.
  }
}

function onPointerMove(event: PointerEvent): void {
  if (!drag || event.pointerId !== drag.pointerId) return
  setColumnWidth(drag.column, drag.startWidth + event.clientX - drag.startX)
}

function finishResize(persist = true): void {
  if (!drag) return
  const previous = drag
  drag = null
  resizing.value = null
  try {
    if (previous.target.hasPointerCapture(previous.pointerId)) {
      previous.target.releasePointerCapture(previous.pointerId)
    }
  } catch {
    // The element/pointer may already be detached (cancel, lost capture, unmount).
  }
  if (persist) persistWidths()
}

function stopResize(event: PointerEvent): void {
  if (event.pointerId === drag?.pointerId) finishResize()
}

function onWindowBlur(): void {
  finishResize()
}

function resizeWithKeyboard(column: ResizeColumn, event: KeyboardEvent): void {
  if (!desktop.value || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
  event.preventDefault()
  finishResize()
  const current = column === "sidebar" ? sidebarWidth.value : workspaceWidth.value
  const step = event.shiftKey ? 64 : 16
  const next =
    event.key === "Home"
      ? column === "sidebar"
        ? SIDEBAR_MIN
        : WORKSPACE_MIN
      : event.key === "End"
        ? column === "sidebar"
          ? sidebarMaximum.value
          : workspaceMaximum.value
        : current + (event.key === "ArrowRight" ? step : -step)
  setColumnWidth(column, next)
  persistWidths()
}

function resetColumn(column: ResizeColumn): void {
  if (!desktop.value) return
  finishResize()
  setColumnWidth(column, column === "sidebar" ? 260 : 380)
  persistWidths()
}

function measureWidth(): void {
  finishResize()
  viewportWidth.value = shell.value?.clientWidth || window.innerWidth
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
  measureWidth()
  window.addEventListener("keydown", onKeydown)
  window.addEventListener("pointermove", onPointerMove)
  window.addEventListener("pointerup", stopResize)
  window.addEventListener("pointercancel", stopResize)
  window.addEventListener("resize", measureWidth)
  window.addEventListener("blur", onWindowBlur)
})
onBeforeUnmount(() => {
  finishResize(false)
  window.removeEventListener("keydown", onKeydown)
  window.removeEventListener("pointermove", onPointerMove)
  window.removeEventListener("pointerup", stopResize)
  window.removeEventListener("pointercancel", stopResize)
  window.removeEventListener("resize", measureWidth)
  window.removeEventListener("blur", onWindowBlur)
})
</script>

<template>
  <div ref="shell" class="app-shell" :class="{ resizing }" :style="gridStyle">
    <aside id="session-sidebar" class="app-sidebar" data-testid="session-sidebar">
      <slot name="sidebar" />
    </aside>
    <div
      class="column-resizer sidebar-resizer"
      role="separator"
      aria-label="Resize session sidebar"
      aria-orientation="vertical"
      aria-controls="session-sidebar"
      :aria-valuemin="SIDEBAR_MIN"
      :aria-valuemax="sidebarMaximum"
      :aria-valuenow="sidebarWidth"
      title="Drag to resize sessions · Double-click to reset"
      tabindex="0"
      data-testid="sidebar-resizer"
      @pointerdown="beginResize('sidebar', $event)"
      @lostpointercapture="stopResize"
      @keydown="resizeWithKeyboard('sidebar', $event)"
      @dblclick="resetColumn('sidebar')"
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
      aria-label="Resize Workspace and chat panels"
      aria-orientation="vertical"
      aria-controls="workspace-sidebar main-chat-panel"
      :aria-valuemin="WORKSPACE_MIN"
      :aria-valuemax="workspaceMaximum"
      :aria-valuenow="workspaceWidth"
      :aria-valuetext="`Workspace ${workspaceWidth}px, chat ${mainWidth}px`"
      title="Drag to resize Workspace and chat · Double-click to reset"
      tabindex="0"
      data-testid="workspace-resizer"
      @pointerdown="beginResize('workspace', $event)"
      @lostpointercapture="stopResize"
      @keydown="resizeWithKeyboard('workspace', $event)"
      @dblclick="resetColumn('workspace')"
    ></div>
    <main id="main-chat-panel" class="app-main" data-testid="chat-panel">
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
    var(--sidebar-width) 8px var(--workspace-width) 8px
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
  inset: 0 -4px;
  content: "";
}
.column-resizer::before {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 3px;
  height: 36px;
  border-radius: 3px;
  background: #94a3b8;
  content: "";
  transform: translate(-50%, -50%);
}
.column-resizer:hover,
.column-resizer:focus-visible,
.app-shell.resizing .column-resizer {
  background: #dbeafe;
  outline: none;
}
.column-resizer:hover::before,
.column-resizer:focus-visible::before,
.app-shell.resizing .column-resizer::before {
  background: var(--accent);
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
