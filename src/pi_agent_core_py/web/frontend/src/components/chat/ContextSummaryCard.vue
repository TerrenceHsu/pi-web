<script setup lang="ts">
import { computed } from "vue"
import type { ContextSummaryItem } from "../../types"
import { renderMarkdown } from "../../utils/markdown"

const props = defineProps<{ item: ContextSummaryItem }>()
const rendered = computed(() => renderMarkdown(props.item.content))
</script>

<template>
  <details class="summary-card" data-testid="context-summary-card">
    <summary>
      <span>Compacted context</span>
      <small>{{ item.sourceMessageCount }} messages summarized</small>
    </summary>
    <!-- markdown-it raw HTML is disabled. -->
    <!-- eslint-disable vue/no-v-html -->
    <div class="summary-content markdown-body" v-html="rendered"></div>
    <!-- eslint-enable vue/no-v-html -->
  </details>
</template>

<style scoped>
.summary-card {
  width: 100%;
  padding: 9px 12px;
  border: 1px dashed var(--border-strong);
  border-radius: 9px;
  color: var(--muted);
  background: var(--code-bg);
  font-size: 12px;
}
summary { display: flex; justify-content: space-between; gap: 12px; cursor: pointer; }
small { font-size: 10px; font-weight: 400; }
.summary-content { margin-top: 10px; color: var(--fg); }
</style>
