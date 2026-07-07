<script setup lang="ts">
import { ref } from "vue"
import * as api from "../api"

const props = defineProps<{ running: boolean }>()
const emit = defineEmits<{
  (e: "sent"): void
  (e: "aborted"): void
  (e: "reset"): void
}>()

const text = ref("")
const skillNames = ref("")
const skillTags = ref("")
const error = ref<string | null>(null)
const showSkillOptions = ref(false)

async function send() {
  if (props.running || !text.value.trim()) return
  error.value = null
  const names = skillNames.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  const tags = skillTags.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
  const skillSelection =
    names.length || tags.length
      ? { names: names.length ? names : undefined, tags: tags.length ? tags : undefined }
      : undefined
  try {
    const resp = await api.sendPrompt(text.value, skillSelection)
    if (resp.ok) {
      text.value = ""
      emit("sent")
    } else {
      error.value = resp.error || "unknown error"
    }
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

async function stop() {
  try {
    await api.abortRun("user clicked stop")
    emit("aborted")
  } catch (e: any) {
    error.value = String(e.message || e)
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    send()
  }
}
</script>

<template>
  <div class="composer">
    <div v-if="error" class="composer-error">{{ error }}</div>

    <div v-if="showSkillOptions" class="composer-skills">
      <input
        v-model="skillNames"
        type="text"
        placeholder="skill names (comma-separated, optional)"
        :disabled="running"
      />
      <input
        v-model="skillTags"
        type="text"
        placeholder="skill tags (comma-separated, optional)"
        :disabled="running"
      />
    </div>

    <div class="composer-row">
      <button
        class="composer-tools-btn"
        :class="{ active: showSkillOptions }"
        :disabled="running"
        title="Skill selection"
        aria-label="Skill selection"
        @click="showSkillOptions = !showSkillOptions"
      >
        ⚙
      </button>

      <textarea
        v-model="text"
        rows="1"
        class="composer-input"
        :placeholder="running ? 'Running…' : 'Message pi-agent-core-py…  (Enter to send, Shift+Enter for newline)'"
        :disabled="running"
        @keydown="onKey"
      ></textarea>

      <button
        v-if="!running"
        class="composer-send primary"
        :disabled="!text.trim()"
        title="Send (Enter)"
        @click="send"
      >
        Send
      </button>
      <button
        v-else
        class="composer-stop danger"
        title="Stop generation"
        @click="stop"
      >
        Stop
      </button>
    </div>

    <div class="composer-hint">
      <span v-if="running">● agent is running</span>
      <span v-else>Skill selection is optional — press the ⚙ button.</span>
    </div>
  </div>
</template>
