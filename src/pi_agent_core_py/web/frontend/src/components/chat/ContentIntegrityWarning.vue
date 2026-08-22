<script setup lang="ts">
import { computed } from "vue"

import type { MessageContentWarning } from "../../types"

const props = defineProps<{ warnings: MessageContentWarning[] }>()

const replacementCount = computed(() =>
  props.warnings.reduce(
    (total, warning) => total + Math.max(0, warning.replacement_character_count || 0),
    0,
  ),
)
const affectedPaths = computed(() =>
  Array.from(new Set(props.warnings.flatMap((warning) => warning.affected_paths || []))),
)
const pathsTruncated = computed(() =>
  props.warnings.some((warning) => warning.paths_truncated),
)
</script>

<template>
  <aside
    class="integrity-warning"
    data-testid="content-integrity-warning"
    data-warning-code="unicode_replacement_character"
    :data-replacement-count="replacementCount"
    role="status"
  >
    <div class="warning-title">
      <span aria-hidden="true">⚠</span>
      <strong>疑似编码损坏</strong>
      <span>检测到 {{ replacementCount }} 个 U+FFFD 替换字符</span>
    </div>
    <p>这可能是历史解码失败，也可能是有意输入。当前记录无法自动恢复原字符。</p>
    <details v-if="affectedPaths.length > 0">
      <summary>受影响字段</summary>
      <code v-for="path in affectedPaths" :key="path">{{ path }}</code>
      <span v-if="pathsTruncated" class="truncated">路径列表已截断</span>
    </details>
  </aside>
</template>

<style scoped>
.integrity-warning {
  box-sizing: border-box;
  width: min(var(--content-max-width), 100%);
  margin: 6px 0;
  padding: 8px 10px;
  border: 1px solid #f59e0b;
  border-radius: 7px;
  background: #fffbeb;
  color: #78350f;
  font-size: 12px;
}
.warning-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.integrity-warning p {
  margin: 5px 0 0;
}
.integrity-warning details {
  margin-top: 5px;
}
.integrity-warning code {
  display: block;
  margin-top: 3px;
  overflow-wrap: anywhere;
}
.truncated {
  display: block;
  margin-top: 3px;
  font-style: italic;
}
</style>
