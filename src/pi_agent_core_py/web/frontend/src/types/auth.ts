export interface AuthUser {
  id: string
  name: string
}

export interface AuthSessionResponse {
  authenticated: true
  user: AuthUser
}

export interface LogoutResponse {
  authenticated: false
}
