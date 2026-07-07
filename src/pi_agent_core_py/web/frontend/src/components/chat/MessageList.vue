<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue"

import type { ChatStreamItem } from "../../types"
import EmptyState from "../common/EmptyState.vue"
import MessageBubble from "./MessageBubble.vue"

const props = defineProps<{
  items: ChatStreamItem[]
  sending: boolean
  /** 透传给 MessageBubble——user_message 的 FileChip 下载链接需要 */
  sessionId?: string | null
}>()

const scrollContainer = ref<HTMLElement | null>(null)

const isEmpty = computed(() => props.items.length === 0 && !props.sending)

function scrollToBottom() {
  const el = scrollContainer.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(
  () => props.items,
  async () => {
    await nextTick()
    scrollToBottom()
  },
  { deep: false },
)
watch(
  () => props.sending,
  async () => {
    await nextTick()
    scrollToBottom()
  },
)
</script>

<template>
  <div ref="scrollContainer" class="message-list" data-testid="message-list">
    <div class="message-container">
      <EmptyState
        v-if="isEmpty"
        title="Start a conversation"
        hint="Type a message below. The assistant will reply here."
      />
      <template v-else>
        <MessageBubble
          v-for="item in items"
          :key="item.id"
          :item="item"
          :session-id="sessionId"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 0;
}
.message-container {
  max-width: var(--content-max-width);
  width: 100%;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  flex-direction: column;
}
</style>
