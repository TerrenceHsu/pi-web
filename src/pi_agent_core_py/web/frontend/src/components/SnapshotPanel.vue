<script setup lang="ts">
import { ref, watch } from "vue"
import * as api from "../api"
import type { SnapshotSummary } from "../types"

const props = defineProps<{ refreshTick: number }>()

const snapshots = ref<SnapshotSummary[]>([])
const selected = ref<number | null>(null)
const detail = ref<any>(null)
const error = ref<string | null>(null)

async function loadList() {
  try {
    const resp = await api.getSnapshots()
    snapshots.value = resp.snapshots
    // 如果还没选或选的 index 不在新列表里，自动选最近的
    if (selected.value === null && snapshots.value.length) {
      selected.value = snapshots.value.length - 1
      await loadDetail(selected.value)
    }
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

async function loadDetail(index: number) {
  selected.value = index
  try {
    detail.value = await api.getSnapshot(index)
  } catch (e: any) {
    detail.value = { error: String(e.message || e) }
  }
}

watch(
  () => props.refreshTick,
  () => loadList(),
  { immediate: true },
)
</script>

<template>
  <div>
    <div v-if="error" class="error-text">{{ error }}</div>
    <div v-if="!snapshots.length" class="empty-state">no snapshots yet</div>
    <div v-else>
      <ul class="snapshot-list">
        <li
          v-for="s in snapshots"
          :key="s.index"
          :class="{ active: selected === s.index }"
          @click="loadDetail(s.index)"
        >
          <strong>#{{ s.index }}</strong>
          [{{ s.status }}]
          {{ s.request_type || "?" }}
          <span v-if="s.error" class="error-text">{{ s.error }}</span>
          <span style="color: var(--muted); margin-left: 8px;">
            msgs: {{ s.messages_before_count }} → {{ s.messages_after_count }},
            events: {{ s.events_count }}, tools: {{ s.tool_calls_count }}/{{ s.tool_results_count }},
            {{ s.duration_ms }}ms
          </span>
        </li>
      </ul>
      <div v-if="detail" style="padding: 12px;">
        <h2 style="margin-bottom: 8px;">Snapshot #{{ selected }} detail</h2>
        <pre>{{ JSON.stringify(detail, null, 2) }}</pre>
      </div>
    </div>
  </div>
</template>
