const SESSION_ROUTE_PREFIX = "/chat/"
const SESSION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

export interface SessionRouteState {
  requested: boolean
  sessionId: string | null
}

export function readSessionRoute(pathname = window.location.pathname): SessionRouteState {
  if (pathname === "/" || pathname === "") {
    return { requested: false, sessionId: null }
  }
  if (!pathname.startsWith(SESSION_ROUTE_PREFIX)) {
    return { requested: true, sessionId: null }
  }

  const encoded = pathname.slice(SESSION_ROUTE_PREFIX.length)
  if (!encoded || encoded.includes("/")) {
    return { requested: true, sessionId: null }
  }
  try {
    const sessionId = decodeURIComponent(encoded)
    return {
      requested: true,
      sessionId: SESSION_ID_PATTERN.test(sessionId) ? sessionId : null,
    }
  } catch {
    return { requested: true, sessionId: null }
  }
}

export function sessionRoutePath(sessionId: string): string {
  if (!SESSION_ID_PATTERN.test(sessionId)) {
    throw new Error("invalid session id")
  }
  return `${SESSION_ROUTE_PREFIX}${encodeURIComponent(sessionId)}`
}

function updateSessionRoute(sessionId: string | null, replace: boolean): void {
  const path = sessionId ? sessionRoutePath(sessionId) : "/"
  if (`${window.location.pathname}${window.location.search}${window.location.hash}` === path) {
    return
  }
  const method = replace ? "replaceState" : "pushState"
  window.history[method]({ sessionId }, "", path)
}

export function pushSessionRoute(sessionId: string): void {
  updateSessionRoute(sessionId, false)
}

export function replaceSessionRoute(sessionId: string | null): void {
  updateSessionRoute(sessionId, true)
}

export function clearSessionRoute(): void {
  replaceSessionRoute(null)
}
