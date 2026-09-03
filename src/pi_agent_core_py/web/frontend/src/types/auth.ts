export interface AuthUser {
  id: string
  name: string
  is_admin: boolean
}

export interface AuthSessionResponse {
  authenticated: true
  user: AuthUser
}

export interface LogoutResponse {
  authenticated: false
}
