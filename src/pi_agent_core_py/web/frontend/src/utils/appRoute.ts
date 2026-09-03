export type AppView = "chat" | "knowledge" | "telemetry"

export const KNOWLEDGE_ROUTE = "/knowledge"
export const TELEMETRY_ROUTE = "/telemetry"

export function readAppView(pathname = window.location.pathname): AppView {
  if (pathname === TELEMETRY_ROUTE || pathname.startsWith(`${TELEMETRY_ROUTE}/`)) {
    return "telemetry"
  }
  return pathname === KNOWLEDGE_ROUTE || pathname.startsWith(`${KNOWLEDGE_ROUTE}/`)
    ? "knowledge"
    : "chat"
}

export function pushTelemetryRoute(): void {
  if (window.location.pathname === TELEMETRY_ROUTE) return
  window.history.pushState({ view: "telemetry" }, "", TELEMETRY_ROUTE)
}

export function pushKnowledgeRoute(): void {
  if (window.location.pathname === KNOWLEDGE_ROUTE) return
  window.history.pushState({ view: "knowledge" }, "", KNOWLEDGE_ROUTE)
}
