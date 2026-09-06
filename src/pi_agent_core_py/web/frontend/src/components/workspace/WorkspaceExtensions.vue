<script setup lang="ts">
import { computed, watch } from "vue"

import { useSessionStore } from "../../stores/sessionStore"
import { useSkillStore } from "../../stores/skillStore"
import { useWorkspaceExtensionStore } from "../../stores/workspaceExtensionStore"

const sessionStore = useSessionStore()
const skillStore = useSkillStore()
const store = useWorkspaceExtensionStore()
const sessionId = computed(() => sessionStore.activeSessionId)

async function load(session: string | null) {
  if (!session) {
    store.reset()
    skillStore.setSelectedSkillNames([])
    return
  }
  try {
    const result = await store.load(session)
    if (sessionStore.activeSessionId === session) {
      skillStore.setSelectedSkillNames(result.selected_skill_names)
    }
  } catch {
    // The store exposes a user-facing error.
  }
}

async function toggleMCP(name: string) {
  const session = sessionId.value
  if (!session) return
  try {
    await store.toggleMCP(session, name)
  } catch {
    // Keep the server snapshot as the source of truth after a failed save.
  }
}

async function toggleSkill(name: string) {
  const session = sessionId.value
  if (!session) return
  try {
    const result = await store.toggleSkill(session, name)
    if (result && sessionStore.activeSessionId === session) {
      skillStore.setSelectedSkillNames(result.selected_skill_names)
    }
  } catch {
    // Store error is rendered below.
  }
}

async function toggleTool(name: string) {
  const session = sessionId.value
  if (!session) return
  try {
    await store.toggleTool(session, name)
  } catch {
    // Store error is rendered below.
  }
}

watch(sessionId, (session) => void load(session), { immediate: true })
</script>

<template>
  <div class="extensions" data-testid="workspace-extensions">
    <div class="extensions-heading">
      <div>
        <strong>Workspace extensions</strong>
        <p>Select optional Tools, MCP servers and Skills for this Workspace.</p>
      </div>
      <button type="button" :disabled="!sessionId || store.loading" @click="load(sessionId)">
        {{ store.loading ? "Loading…" : "Refresh" }}
      </button>
    </div>

    <p v-if="store.error" class="extension-error">{{ store.error }}</p>
    <div v-if="!sessionId" class="extension-empty">Select a Session first.</div>
    <template v-else-if="store.snapshot">
      <section class="extension-group">
        <h3>Tools</h3>
        <label v-for="tool in store.snapshot.tools ?? []" :key="tool.name" class="extension-option">
          <input
            type="checkbox"
            :checked="tool.selected"
            :disabled="(!tool.available && !tool.selected) || store.saving"
            :data-testid="`workspace-tool-${tool.name}`"
            @change="toggleTool(tool.name)"
          />
          <span>
            <strong>{{ tool.label }}</strong>
            <small>{{ tool.available ? "Local calculation · optional" : tool.reason }}</small>
          </span>
        </label>
        <p class="extension-empty">
          Data stays local for calculation. Analysis summaries sent to chat use your selected model
          Provider. Saving results is a separate action. Python Data Analysis requires approval
          every time and runs with local user permissions; its environment is NOT a security sandbox.
        </p>
      </section>
      <section class="extension-group">
        <h3>MCP servers</h3>
        <p v-if="!store.snapshot.mcp_servers.length" class="extension-empty">
          No global MCP servers configured.
        </p>
        <label
          v-for="server in store.snapshot.mcp_servers"
          :key="server.name"
          class="extension-option"
          :class="{ unavailable: !server.available }"
        >
          <input
            type="checkbox"
            :checked="server.selected"
            :disabled="!server.available || store.saving"
            @change="toggleMCP(server.name)"
          />
          <span>
            <strong>{{ server.name }}</strong>
            <small>
              {{ server.transport === "http" ? server.url : server.command }}
              · {{ server.available ? `${server.tool_count} tools` : "unavailable" }}
            </small>
          </span>
          <em v-if="server.builtin">default</em>
        </label>
      </section>

      <section class="extension-group">
        <h3>Skills</h3>
        <p v-if="!store.snapshot.skills.length" class="extension-empty">
          No global Skills configured.
        </p>
        <label
          v-for="skill in store.snapshot.skills"
          :key="skill.name"
          class="extension-option"
          :class="{ unavailable: !skill.available }"
        >
          <input
            type="checkbox"
            :checked="skill.selected"
            :disabled="!skill.available || store.saving"
            @change="toggleSkill(skill.name)"
          />
          <span>
            <strong>{{ skill.name }}</strong>
            <small>{{ skill.description }}</small>
          </span>
        </label>
      </section>
    </template>
  </div>
</template>

<style scoped>
.extensions {
  padding: 14px;
  overflow: auto;
}
.extensions-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.extensions-heading p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}
.extension-group {
  margin-top: 18px;
}
.extension-group h3 {
  margin: 0 0 8px;
  font-size: 12px;
  text-transform: uppercase;
  color: var(--muted);
}
.extension-option {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 9px;
  margin-bottom: 7px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: white;
}
.extension-option > span {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.extension-option small {
  color: var(--muted);
  overflow-wrap: anywhere;
}
.extension-option em {
  color: var(--accent);
  font-size: 10px;
  font-style: normal;
  text-transform: uppercase;
}
.extension-option.unavailable {
  opacity: 0.55;
}
.extension-empty {
  color: var(--muted);
  font-size: 12px;
}
.extension-error {
  color: var(--danger);
  font-size: 12px;
  overflow-wrap: anywhere;
}
</style>
