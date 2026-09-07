import { requestJson } from "./client"
import type { AuthSessionResponse, LogoutResponse } from "../types/auth"

export function getAuthSession(): Promise<AuthSessionResponse> {
  return requestJson<AuthSessionResponse>("/api/auth/session")
}

export function login(name: string, password: string): Promise<AuthSessionResponse> {
  return requestJson<AuthSessionResponse>("/api/auth/login", {
    method: "POST",
    body: { name, password },
  })
}

export function logout(): Promise<LogoutResponse> {
  return requestJson<LogoutResponse>("/api/auth/logout", { method: "POST" })
}

export function changePassword(currentPassword: string, newPassword: string): Promise<LogoutResponse> {
  return requestJson<LogoutResponse>("/api/auth/password", {
    method: "POST",
    body: { current_password: currentPassword, new_password: newPassword },
  })
}
