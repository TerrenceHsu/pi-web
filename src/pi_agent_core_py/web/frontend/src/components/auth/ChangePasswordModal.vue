<script setup lang="ts">
import { ref, watch } from "vue"
import Modal from "../common/Modal.vue"
import { useAuthStore } from "../../stores/authStore"

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (event: "close"): void }>()
const auth = useAuthStore()
const currentPassword = ref("")
const newPassword = ref("")
const confirmation = ref("")
const validation = ref("")

function clear() {
  currentPassword.value = ""
  newPassword.value = ""
  confirmation.value = ""
}
watch(() => props.open, () => {
  clear()
  validation.value = ""
  auth.error = null
})
function close() {
  if (auth.submitting) return
  clear()
  emit("close")
}
async function submit() {
  if (auth.submitting) return
  validation.value = ""
  if (newPassword.value !== confirmation.value) {
    validation.value = "New passwords do not match."
    return
  }
  if (newPassword.value === currentPassword.value) {
    validation.value = "Choose a different new password."
    return
  }
  const changed = await auth.changePassword(currentPassword.value, newPassword.value)
  clear()
  if (changed) emit("close")
}
</script>

<template>
  <Modal :open="open" title="Change password" width="min(460px, 92vw)" @close="close">
    <form class="password-form" data-testid="change-password-form" @submit.prevent="submit">
      <p>Use 12–128 characters. All existing logins will be revoked; sign in again after saving.</p>
      <label>
        Current password
        <input
          v-model="currentPassword"
          type="password"
          autocomplete="current-password"
          required minlength="6" maxlength="128"
          :disabled="auth.submitting"
        >
      </label>
      <label>
        New password
        <input
          v-model="newPassword"
          type="password"
          autocomplete="new-password"
          required minlength="12" maxlength="128"
          :disabled="auth.submitting"
        >
      </label>
      <label>
        Confirm new password
        <input
          v-model="confirmation"
          type="password"
          autocomplete="new-password"
          required minlength="12" maxlength="128"
          :disabled="auth.submitting"
        >
      </label>
      <p v-if="validation || auth.error" role="alert">{{ validation || auth.error }}</p>
      <button type="submit" :disabled="auth.submitting">
        {{ auth.submitting ? "Saving…" : "Change password and sign out" }}
      </button>
    </form>
  </Modal>
</template>

<style scoped>
.password-form { display: grid; gap: 14px; }
label { display: grid; gap: 6px; }
input { width: 100%; box-sizing: border-box; padding: 8px; }
p { margin: 0; color: var(--muted); font-size: 13px; }
[role="alert"] { color: var(--danger); }
</style>
