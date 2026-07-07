<script setup lang="ts">
import { reactive, ref } from "vue"

import { ApiError } from "../../api/client"
import { useMcpStore } from "../../stores/mcpStore"
import LoadingSpinner from "../common/LoadingSpinner.vue"

const mcpStore = useMcpStore()

interface EnvRow {
  key: string
  value: string
}

const name = ref("")
const command = ref("")
const argsJson = ref("[]")
const enabled = ref(false)
const envRows = ref<EnvRow[]>([{ key: "", value: "" }])
const submitting = ref(false)
const localError = ref<string | null>(null)

function addEnvRow() {
  envRows.value.push({ key: "", value: "" })
}

function removeEnvRow(idx: number) {
  envRows.value.splice(idx, 1)
  if (envRows.value.length === 0) {
    envRows.value.push({ key: "", value: "" })
  }
}

function resetForm() {
  name.value = ""
  command.value = ""
  argsJson.value = "[]"
  enabled.value = false
  // 关键：env value 不保留——提交后立即清空，避免残留
  envRows.value = [{ key: "", value: "" }]
  localError.value = null
}

function buildPayload(): { payload: any | null; error: string | null } {
  if (!name.value.trim()) return { payload: null, error: "name is required" }
  if (!command.value.trim()) {
    return { payload: null, error: "command is required" }
  }

  let args: string[] = []
  try {
    const parsed = JSON.parse(argsJson.value || "[]")
    if (!Array.isArray(parsed)) {
      return { payload: null, error: "args must be a JSON array of strings" }
    }
    for (const a of parsed) {
      if (typeof a !== "string") {
        return { payload: null, error: "args entries must be strings" }
      }
    }
    args = parsed
  } catch (e: any) {
    return {
      payload: null,
      error: `args JSON parse error: ${e?.message ?? e}`,
    }
  }

  const env: Record<string, string> = {}
  for (const row of envRows.value) {
    const k = row.key.trim()
    if (!k) continue // 空 key 不提交
    if (!row.value) {
      return {
        payload: null,
        error: `env key "${k}" has empty value`,
      }
    }
    env[k] = row.value
  }

  return {
    payload: {
      name: name.value.trim(),
      command: command.value.trim(),
      args,
      env,
      enabled: enabled.value,
    },
    error: null,
  }
}

async function submit() {
  localError.value = null
  const { payload, error } = buildPayload()
  if (error || !payload) {
    localError.value = error
    return
  }
  submitting.value = true
  try {
    await mcpStore.createServer(payload)
    // 成功——所有字段清空；env value 不再保留
    resetForm()
  } catch (e: any) {
    localError.value =
      e instanceof ApiError ? e.detail : String(e?.message ?? e)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <form class="mcp-server-form" data-testid="mcp-server-form" @submit.prevent="submit">
    <div class="grid">
      <label class="field">
        <span class="field-label">Name</span>
        <input
          v-model="name"
          type="text"
          placeholder="e.g. local-tools"
          autocomplete="off"
          spellcheck="false"
          data-testid="mcp-server-name-input"
        />
      </label>
      <label class="field">
        <span class="field-label">Command</span>
        <input
          v-model="command"
          type="text"
          placeholder="e.g. python"
          autocomplete="off"
          spellcheck="false"
          data-testid="mcp-server-command-input"
        />
      </label>
      <label class="field full">
        <span class="field-label">Args (JSON array of strings)</span>
        <textarea
          v-model="argsJson"
          rows="3"
          spellcheck="false"
          placeholder='["--port", "8080"]'
        ></textarea>
      </label>
      <div class="field full">
        <div class="field-label">Env (key/value — values not echoed back)</div>
        <div class="env-rows">
          <div
            v-for="(row, idx) in envRows"
            :key="idx"
            class="env-row"
          >
            <input
              v-model="row.key"
              type="text"
              placeholder="KEY"
              autocomplete="off"
              spellcheck="false"
              data-testid="mcp-env-key-input"
            />
            <input
              v-model="row.value"
              type="password"
              placeholder="value (hidden after submit)"
              autocomplete="new-password"
              data-testid="mcp-env-value-input"
            />
            <button
              type="button"
              class="env-rm"
              title="Remove"
              @click="removeEnvRow(idx)"
            >×</button>
          </div>
        </div>
        <button type="button" class="env-add" @click="addEnvRow">
          + Add env row
        </button>
      </div>
      <label class="field checkbox-field full">
        <input v-model="enabled" type="checkbox" />
        <span>Enable on submit (will try to attach immediately)</span>
      </label>
    </div>
    <div class="form-actions">
      <button type="button" @click="resetForm">Clear</button>
      <button type="submit" class="primary" :disabled="submitting">
        <LoadingSpinner v-if="submitting" :size="12" />
        <span v-else>Add server</span>
      </button>
    </div>
    <p v-if="localError" class="form-error">{{ localError }}</p>
  </form>
</template>

<style scoped>
.mcp-server-form {
  margin-bottom: 14px;
  padding: 12px;
  border: 1px dashed var(--border-strong);
  border-radius: 8px;
  background: var(--bg);
}
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}
.field.full {
  grid-column: 1 / -1;
}
.field-label {
  color: var(--muted);
  font-size: 11px;
}
.field input[type="text"],
.field input:not([type]) {
  padding: 6px 8px;
  font-size: 13px;
}
.field textarea {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  padding: 6px 8px;
  resize: vertical;
}
.checkbox-field {
  flex-direction: row;
  align-items: center;
  gap: 6px;
  color: var(--fg);
  font-size: 12px;
}
.env-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.env-row {
  display: grid;
  grid-template-columns: 1fr 2fr auto;
  gap: 6px;
  align-items: center;
}
.env-row input {
  padding: 6px 8px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.env-rm {
  padding: 4px 8px;
  border: 1px solid var(--border);
  background: white;
  font-size: 14px;
  line-height: 1;
  color: var(--muted);
  border-radius: 4px;
  cursor: pointer;
}
.env-rm:hover {
  background: var(--code-bg);
}
.env-add {
  margin-top: 6px;
  font-size: 12px;
  padding: 4px 10px;
  border: 1px dashed var(--border-strong);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  border-radius: 4px;
}
.env-add:hover {
  background: var(--code-bg);
  color: var(--fg);
}
.form-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
  margin-top: 10px;
}
.form-actions button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  font-size: 12px;
}
.form-error {
  margin: 8px 0 0;
  color: var(--danger);
  font-size: 12px;
  word-break: break-word;
}
</style>
