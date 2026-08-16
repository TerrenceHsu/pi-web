<script setup lang="ts">
import { reactive, ref, watch } from "vue"

import { useMcpStore } from "../../stores/mcpStore"
import type { DDGSSearchSettings, MCPServerSummary } from "../../types"

const props = defineProps<{ server: MCPServerSummary }>()
const mcpStore = useMcpStore()

const saving = ref(false)
const saved = ref(false)
const form = reactive<DDGSSearchSettings>({
  max_results: 5,
  region: "wt-wt",
  safesearch: "moderate",
  timelimit: null,
  timeout_seconds: 10,
  backend: "auto",
})

watch(
  () => props.server.settings,
  (settings) => {
    if (!settings || !("max_results" in settings)) return
    Object.assign(form, settings)
  },
  { immediate: true, deep: true },
)

function setTimelimit(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  form.timelimit = (value || null) as DDGSSearchSettings["timelimit"]
}

async function saveSettings() {
  saving.value = true
  saved.value = false
  try {
    await mcpStore.updateDDGSSettings({ ...form })
    saved.value = true
  } catch {
    // Store exposes the API error in the modal's ErrorBanner.
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <form class="ddgs-settings" data-testid="ddgs-settings-form" @submit.prevent="saveSettings">
    <div class="settings-title">
      <span>DuckDuckGo search defaults</span>
      <span class="locked">enforced by server</span>
    </div>
    <div class="settings-grid">
      <label>
        <span>Results per search</span>
        <input
          v-model.number="form.max_results"
          type="number"
          min="1"
          max="20"
          required
          data-testid="ddgs-max-results"
        />
      </label>
      <label>
        <span>Region</span>
        <input
          v-model.trim="form.region"
          type="text"
          minlength="2"
          maxlength="32"
          list="ddgs-region-options"
          required
          data-testid="ddgs-region"
        />
        <datalist id="ddgs-region-options">
          <option value="wt-wt">Global</option>
          <option value="cn-zh">China</option>
          <option value="us-en">United States</option>
          <option value="uk-en">United Kingdom</option>
        </datalist>
      </label>
      <label>
        <span>Safe search</span>
        <select v-model="form.safesearch" data-testid="ddgs-safesearch">
          <option value="on">Strict</option>
          <option value="moderate">Moderate</option>
          <option value="off">Off</option>
        </select>
      </label>
      <label>
        <span>Time range</span>
        <select
          :value="form.timelimit ?? ''"
          data-testid="ddgs-timelimit"
          @change="setTimelimit"
        >
          <option value="">Any time</option>
          <option value="d">Past day</option>
          <option value="w">Past week</option>
          <option value="m">Past month</option>
          <option value="y">Past year</option>
        </select>
      </label>
      <label>
        <span>Timeout (seconds)</span>
        <input
          v-model.number="form.timeout_seconds"
          type="number"
          min="1"
          max="30"
          required
          data-testid="ddgs-timeout"
        />
      </label>
      <label>
        <span>Backend</span>
        <select v-model="form.backend" data-testid="ddgs-backend">
          <option value="auto">Auto (recommended)</option>
          <option value="duckduckgo">DuckDuckGo only</option>
        </select>
      </label>
    </div>
    <div class="settings-actions">
      <button type="submit" class="primary" :disabled="saving" data-testid="ddgs-save">
        {{ saving ? "Saving…" : "Save search settings" }}
      </button>
      <span v-if="saved" class="saved">Saved</span>
      <span v-if="server.enabled" class="restart-note">Saving reconnects the MCP server.</span>
    </div>
  </form>
</template>

<style scoped>
.ddgs-settings {
  padding: 10px;
  margin: 8px 0;
  border: 1px solid #bfdbfe;
  border-radius: 7px;
  background: #f8fbff;
}
.settings-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  font-weight: 600;
}
.locked {
  padding: 1px 6px;
  border-radius: 8px;
  color: #1d4ed8;
  background: #dbeafe;
  font-size: 9px;
  font-weight: 500;
}
.settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.settings-grid label {
  display: flex;
  flex-direction: column;
  gap: 3px;
  color: var(--muted);
  font-size: 10px;
}
.settings-grid input,
.settings-grid select {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 5px 7px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: white;
  color: var(--fg);
  font-size: 12px;
}
.settings-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 9px;
  font-size: 10px;
}
.settings-actions button {
  padding: 5px 10px;
  font-size: 11px;
}
.saved {
  color: var(--success);
  font-weight: 600;
}
.restart-note {
  color: var(--muted);
}
@media (max-width: 520px) {
  .settings-grid {
    grid-template-columns: 1fr;
  }
}
</style>
