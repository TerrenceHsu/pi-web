import { createUiHeaders, requestJson, safeErrorDetail } from "./client"
export interface BrowserPage {
  id: string
  title: string
  url: string
  dialog: string | null
  width: number
  height: number
  dpr: number
  view_version: number
  navigation_ms: number | null
  capture_error?: string | null
}
const base = (sid: string) => `/api/sessions/${encodeURIComponent(sid)}/browser`
const path = (sid: string, pid: string) => `${base(sid)}/pages/${encodeURIComponent(pid)}`
export const list = (sid: string) => requestJson<{ pages: BrowserPage[] }>(base(sid))
export const create = (sid: string) =>
  requestJson<BrowserPage>(`${base(sid)}/pages`, { method: "POST", body: {} })
export const close = (sid: string, pid: string) =>
  requestJson<null>(path(sid, pid), { method: "DELETE" })
export const info = (sid: string, pid: string) => requestJson<BrowserPage>(path(sid, pid))
export const action = (
  sid: string,
  pid: string,
  body: Record<string, unknown>,
  signal?: AbortSignal,
) => requestJson<BrowserPage>(`${path(sid, pid)}/action`, { method: "POST", body, signal })
export async function frame(sid: string, pid: string, signal: AbortSignal): Promise<Blob> {
  const response = await fetch(`${path(sid, pid)}/frame`, {
    headers: createUiHeaders(),
    signal,
    cache: "no-store",
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(safeErrorDetail(body, response.statusText, "Browser frame unavailable."))
  }
  return response.blob()
}
