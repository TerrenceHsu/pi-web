<script setup lang="ts">
import { onBeforeUnmount, onMounted, watch } from "vue"

const props = withDefaults(
  defineProps<{
    open: boolean
    title?: string
    width?: string
  }>(),
  {
    title: "",
    width: "min(900px, 92vw)",
  },
)

const emit = defineEmits<{ (e: "close"): void }>()

function onKey(e: KeyboardEvent) {
  if (e.key === "Escape" && props.open) {
    e.preventDefault()
    emit("close")
  }
}

onMounted(() => {
  window.addEventListener("keydown", onKey)
})

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey)
  document.body.style.overflow = ""
})

watch(
  () => props.open,
  (open) => {
    document.body.style.overflow = open ? "hidden" : ""
  },
)

function onOverlayClick() {
  emit("close")
}

function stopPropagation(e: MouseEvent) {
  e.stopPropagation()
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-overlay" @click="onOverlayClick">
      <div
        class="modal-window"
        :style="{ width }"
        data-testid="modal"
        role="dialog"
        aria-modal="true"
        @click="stopPropagation"
      >
        <header v-if="title || $slots.header" class="modal-header">
          <slot name="header">
            <h2 class="modal-title">{{ title }}</h2>
          </slot>
          <button
            type="button"
            class="modal-close"
            data-testid="modal-close"
            aria-label="Close"
            @click="emit('close')"
          >×</button>
        </header>
        <div class="modal-body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="modal-footer">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.modal-window {
  background: white;
  border-radius: 10px;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.18);
  display: flex;
  flex-direction: column;
  max-width: 92vw;
  max-height: 88vh;
  overflow: hidden;
}
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border);
  gap: 8px;
}
.modal-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--fg);
}
.modal-close {
  width: 28px;
  height: 28px;
  padding: 0;
  border: none;
  background: transparent;
  font-size: 22px;
  line-height: 1;
  color: var(--muted);
  border-radius: 6px;
  cursor: pointer;
}
.modal-close:hover {
  background: var(--code-bg);
  color: var(--fg);
}
.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 18px;
  max-height: 80vh;
}
.modal-footer {
  padding: 10px 18px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
