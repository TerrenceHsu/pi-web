<script setup lang="ts">
import { computed } from "vue"

import { useSkillStore } from "../../stores/skillStore"
import type { SkillSummary } from "../../types"

const skillStore = useSkillStore()

const skills = computed<SkillSummary[]>(() => skillStore.skills)
const enabledSet = computed(() => new Set(skillStore.enabledSkillNames))
const selectedSet = computed(() => new Set(skillStore.selectedSkillNames))

async function toggleEnabled(s: SkillSummary) {
  try {
    if (enabledSet.value.has(s.name)) {
      await skillStore.disableSkill(s.name)
    } else {
      await skillStore.enableSkill(s.name)
    }
  } catch {
    // store 已 set error
  }
}

function toggleSelected(s: SkillSummary) {
  if (!enabledSet.value.has(s.name)) return
  skillStore.toggleSelectedSkill(s.name)
}
</script>

<template>
  <div class="skill-list">
    <div v-if="skills.length === 0" class="empty">No skills uploaded yet.</div>
    <div
      v-for="s in skills"
      :key="s.name"
      class="skill-card"
      :class="{ disabled: !enabledSet.has(s.name) }"
      data-testid="skill-card"
    >
      <div class="skill-card-main">
        <div class="skill-card-header">
          <span class="skill-name">{{ s.name }}</span>
          <span
            class="badge"
            :class="enabledSet.has(s.name) ? 'badge-on' : 'badge-off'"
          >{{ s.status }}</span>
          <span class="priority">P{{ s.priority }}</span>
        </div>
        <p class="skill-desc">{{ s.description || "(no description)" }}</p>
        <div class="skill-meta">
          <span v-for="tag in s.tags" :key="tag" class="tag">{{ tag }}</span>
          <span
            v-if="s.tool_names && s.tool_names.length"
            class="tool-names"
          >tools: {{ s.tool_names.join(", ") }}</span>
        </div>
      </div>
      <div class="skill-card-actions">
        <label class="switch">
          <input
            type="checkbox"
            :checked="enabledSet.has(s.name)"
            @change="toggleEnabled(s)"
          />
          <span class="switch-label">Enabled</span>
        </label>
        <label
          class="select-box"
          :class="{ disabled: !enabledSet.has(s.name) }"
        >
          <input
            type="checkbox"
            :disabled="!enabledSet.has(s.name)"
            :checked="selectedSet.has(s.name)"
            @change="toggleSelected(s)"
          />
          <span class="select-label">Use this turn</span>
        </label>
      </div>
    </div>
  </div>
</template>

<style scoped>
.skill-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.empty {
  padding: 16px;
  color: var(--muted);
  text-align: center;
  font-style: italic;
  font-size: 13px;
}
.skill-card {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: white;
}
.skill-card.disabled {
  background: var(--bg);
  opacity: 0.85;
}
.skill-card-main {
  flex: 1;
  min-width: 0;
}
.skill-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.skill-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--fg);
  word-break: break-all;
}
.priority {
  font-size: 11px;
  color: var(--muted);
}
.skill-desc {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--fg);
  word-break: break-word;
}
.skill-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 11px;
  color: var(--muted);
}
.tag {
  display: inline-block;
  padding: 1px 8px;
  background: var(--code-bg);
  color: var(--muted);
  border-radius: 10px;
}
.tool-names {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.skill-card-actions {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
  flex-shrink: 0;
}
.switch,
.select-box {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 12px;
  color: var(--fg);
}
.switch input,
.select-box input {
  margin: 0;
}
.select-box.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 500;
}
.badge-on {
  background: var(--success);
  color: white;
}
.badge-off {
  background: var(--code-bg);
  color: var(--muted);
}
</style>
