<script setup lang="ts">
import { ref } from "vue"

import { useAuthStore } from "../../stores/authStore"

const authStore = useAuthStore()
const name = ref("")
const password = ref("")

async function submit(): Promise<void> {
  if (!name.value.trim() || !password.value) return
  const candidate = password.value
  password.value = ""
  await authStore.login(name.value.trim(), candidate)
}
</script>

<template>
  <main class="login-page">
    <form class="login-card" data-testid="login-form" @submit.prevent="submit">
      <div class="login-brand"><span class="login-dot"></span>pi-chat</div>
      <div>
        <h1>Sign in</h1>
        <p>Enter your account to open its private workspace.</p>
      </div>

      <label>
        <span>Username</span>
        <input
          v-model="name"
          data-testid="login-name"
          name="username"
          type="text"
          autocomplete="username"
          autofocus
          :disabled="authStore.submitting"
        />
      </label>

      <label>
        <span>Password</span>
        <input
          v-model="password"
          data-testid="login-password"
          name="password"
          type="password"
          autocomplete="current-password"
          :disabled="authStore.submitting"
        />
      </label>

      <div v-if="authStore.error" class="login-error" role="alert">
        {{ authStore.error }}
      </div>

      <button
        class="primary login-submit"
        data-testid="login-submit"
        type="submit"
        :disabled="authStore.submitting || !name.trim() || !password"
      >
        {{ authStore.submitting ? "Signing in…" : "Sign in" }}
      </button>
    </form>
  </main>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background:
    radial-gradient(circle at 15% 20%, rgba(37, 99, 235, 0.12), transparent 34%),
    radial-gradient(circle at 85% 80%, rgba(99, 102, 241, 0.1), transparent 32%),
    var(--bg);
}
.login-card {
  width: min(400px, 100%);
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: 32px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: white;
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.1);
}
.login-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 650;
}
.login-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
}
h1 {
  margin: 0 0 4px;
  font-size: 24px;
}
p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--fg);
  font-size: 13px;
  font-weight: 500;
}
input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  font: inherit;
}
input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
}
.login-error {
  padding: 9px 11px;
  border: 1px solid rgba(220, 38, 38, 0.25);
  border-radius: 7px;
  background: rgba(220, 38, 38, 0.06);
  color: var(--danger);
  font-size: 12px;
}
.login-submit {
  width: 100%;
  padding: 10px 14px;
  border-radius: 8px;
  font-weight: 600;
}
</style>
