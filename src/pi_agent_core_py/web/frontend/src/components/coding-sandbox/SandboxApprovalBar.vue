<script setup lang="ts">
import { computed } from "vue"

import { useCodingSandboxStore } from "../../stores/codingSandboxStore"

const emit = defineEmits<{ (event: "open-workspace"): void }>()
const sandboxStore = useCodingSandboxStore()
const operation = computed(() => sandboxStore.operation)
const visible = computed(() =>
  ["awaiting_approval", "publish_conflict"].includes(operation.value?.status ?? ""),
)
</script>

<template>
  <section v-if="visible" class="sandbox-approval-bar" data-testid="sandbox-chat-approval">
    <div class="sandbox-approval-copy">
      <strong>
        {{
          operation?.status === "publish_conflict"
            ? "发布遇到冲突，冻结文件已保留"
            : operation?.bash_evidence
              ? "Bash 输出已冻结，等待审阅发布（未经功能验证）"
              : "代码已验证，等待批准发布"
        }}
      </strong>
      <span>
        {{ operation?.changed_paths?.length ?? operation?.diff?.entries.length ?? 0 }} 个文件将写入
        Workspace
      </span>
    </div>
    <div class="sandbox-approval-actions">
      <button type="button" data-testid="sandbox-chat-review" @click="emit('open-workspace')">
        查看文件
      </button>
      <template v-if="operation?.status === 'publish_conflict'">
        <button
          v-if="operation?.allowed_actions?.includes('refreeze')"
          type="button"
          :disabled="sandboxStore.busy"
          data-testid="sandbox-chat-refreeze"
          @click="sandboxStore.refreeze"
        >
          重新冻结
        </button>
        <button
          type="button"
          class="primary"
          :disabled="sandboxStore.busy || operation?.publish_available === false || operation?.execution_released === false"
          data-testid="sandbox-chat-retry-publish"
          @click="sandboxStore.retryPublish"
        >
          重新发布
        </button>
      </template>
      <button
        v-else
        type="button"
        class="primary"
        :disabled="sandboxStore.busy || operation?.publish_available === false || operation?.execution_released === false"
        data-testid="sandbox-chat-publish"
        @click="sandboxStore.publish"
      >
        批准并发布
      </button>
    </div>
  </section>
</template>

<style scoped>
.sandbox-approval-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 8px 24px 0;
  padding: 10px 12px;
  border: 1px solid #93c5fd;
  border-radius: 10px;
  background: #eff6ff;
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.08);
}
.sandbox-approval-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.sandbox-approval-copy strong {
  color: #1e3a8a;
  font-size: 12px;
}
.sandbox-approval-copy span {
  color: #64748b;
  font-size: 11px;
}
.sandbox-approval-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 7px;
}
.sandbox-approval-actions button {
  padding: 6px 10px;
  border: 1px solid #bfdbfe;
  border-radius: 7px;
  background: #fff;
  color: #1e40af;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}
.sandbox-approval-actions button.primary {
  border-color: #2563eb;
  background: #2563eb;
  color: #fff;
}
.sandbox-approval-actions button:disabled {
  cursor: default;
  opacity: 0.55;
}
@media (max-width: 720px) {
  .sandbox-approval-bar {
    align-items: stretch;
    flex-direction: column;
    margin-inline: 12px;
  }
  .sandbox-approval-actions {
    justify-content: flex-end;
  }
}
</style>
