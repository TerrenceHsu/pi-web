export type AppView = "chat" | "knowledge"

export const KNOWLEDGE_ROUTE = "/knowledge"

export function readAppView(pathname = window.location.pathname): AppView {
  return pathname === KNOWLEDGE_ROUTE || pathname.startsWith(`${KNOWLEDGE_ROUTE}/`)
    ? "knowledge"
    : "chat"
}

export function pushKnowledgeRoute(): void {
  if (window.location.pathname === KNOWLEDGE_ROUTE) return
  window.history.pushState({ view: "knowledge" }, "", KNOWLEDGE_ROUTE)
}
