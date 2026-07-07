<script setup lang="ts">
import { ref, watch } from "vue"
import * as api from "../api"
import type { SkillsResponse } from "../types"

const props = defineProps<{ refreshTick: number }>()

const data = ref<SkillsResponse | null>(null)
const includePrompt = ref(false)
const error = ref<string | null>(null)

async function load() {
  try {
    data.value = await api.getSkills(includePrompt.value)
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

watch(
  () => props.refreshTick,
  () => load(),
  { immediate: true },
)

watch(includePrompt, () => load())
</script>

<template>
  <div>
    <div v-if="error" class="error-text">{{ error }}</div>
    <div v-if="!data" class="empty-state">loading...</div>
    <div v-else-if="!data.attached" class="empty-state">
      no skill registry attached
    </div>
    <div v-else style="padding: 12px;">
      <div class="row" style="margin-bottom: 8px;">
        <label>
          <input v-model="includePrompt" type="checkbox" />
          include prompt body
        </label>
      </div>

      <h2>Skills</h2>
      <table v-if="data.skills.length" class="data">
        <thead>
          <tr>
            <th>name</th>
            <th>status</th>
            <th>priority</th>
            <th>tags</th>
            <th>tool_names</th>
            <th>description</th>
            <th>metadata</th>
            <th v-if="includePrompt">prompt</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in data.skills" :key="s.name">
            <td><code>{{ s.name }}</code></td>
            <td>
              <span :class="['status-pill', s.status === 'enabled' ? 'idle' : 'error']">
                {{ s.status }}
              </span>
            </td>
            <td>{{ s.priority }}</td>
            <td>{{ s.tags.join(", ") }}</td>
            <td>{{ s.tool_names.join(", ") }}</td>
            <td>{{ s.description }}</td>
            <td>
              <details>
                <summary>show</summary>
                <pre>{{ JSON.stringify(s.metadata, null, 2) }}</pre>
              </details>
            </td>
            <td v-if="includePrompt">
              <details>
                <summary>show</summary>
                <pre>{{ JSON.stringify(s.prompt, null, 2) }}</pre>
              </details>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty-state">no skills registered</div>

      <h2>Skill Loader Metadata</h2>
      <pre>{{ JSON.stringify(data.skill_loader, null, 2) }}</pre>
    </div>
  </div>
</template>
