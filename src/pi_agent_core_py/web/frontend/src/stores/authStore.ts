import { defineStore } from "pinia"
import { computed, ref } from "vue"

import * as authApi from "../api/auth"
import { ApiError } from "../api/client"
import type { AuthUser } from "../types/auth"

export type AuthStatus = "checking" | "anonymous" | "authenticated"

export const useAuthStore = defineStore("auth", () => {
  const status = ref<AuthStatus>("checking")
  const user = ref<AuthUser | null>(null)
  const submitting = ref(false)
  const error = ref<string | null>(null)

  const authenticated = computed(() => status.value === "authenticated" && user.value !== null)

  async function restoreSession(): Promise<void> {
    status.value = "checking"
    error.value = null
    try {
      const response = await authApi.getAuthSession()
      user.value = response.user
      status.value = "authenticated"
    } catch (e: unknown) {
      user.value = null
      status.value = "anonymous"
      if (!(e instanceof ApiError && e.status === 401)) {
        error.value = e instanceof ApiError ? e.detail : "Unable to reach the login service."
      }
    }
  }

  async function login(name: string, password: string): Promise<boolean> {
    submitting.value = true
    error.value = null
    try {
      const response = await authApi.login(name, password)
      user.value = response.user
      status.value = "authenticated"
      return true
    } catch (e: unknown) {
      user.value = null
      status.value = "anonymous"
      error.value = e instanceof ApiError ? e.detail : "Login failed."
      return false
    } finally {
      submitting.value = false
    }
  }

  async function logout(): Promise<void> {
    submitting.value = true
    error.value = null
    try {
      await authApi.logout()
    } catch (e: unknown) {
      error.value = e instanceof ApiError ? e.detail : "Logout failed."
    } finally {
      user.value = null
      status.value = "anonymous"
      submitting.value = false
    }
  }

  return {
    status,
    user,
    submitting,
    error,
    authenticated,
    restoreSession,
    login,
    logout,
  }
})
