<script setup lang="ts">
/**
 * AppShell —— 两栏布局骨架。
 *
 * - 左侧 sidebar 固定 260px
 * - 中间 main 自适应
 * - 高度 100vh，无右栏 / Drawer / Inspector
 *
 * Slots:
 *   - sidebar：左栏内容（SessionSidebar）
 *   - main：中间内容（ChatPanel）
 *
 * 不提供 right / drawer / inspector slot——刻意避免后续回流到调试器形态。
 */
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar" data-testid="session-sidebar">
      <slot name="sidebar" />
    </aside>
    <main class="app-main" data-testid="chat-panel">
      <slot name="main" />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns: 260px 1fr;
  height: 100vh;
  width: 100vw;
  background: var(--bg);
  overflow: hidden;
}
.app-sidebar {
  background: var(--sidebar-bg);
  border-right: 1px solid var(--sidebar-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.app-main {
  background: var(--chat-bg);
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
