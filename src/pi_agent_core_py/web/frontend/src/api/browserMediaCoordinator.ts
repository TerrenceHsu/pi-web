/** One media lease per application window; the server enforces the account-wide limit. */
let transition = Promise.resolve()
let current: { owner: symbol; release: () => Promise<boolean> } | undefined
type RenderingPreference = { mode: "media" | "screenshots"; failed: boolean }
const preferences = new Map<string, RenderingPreference>()

export function browserRenderingPreference(sid: string, pid: string): RenderingPreference {
  return preferences.get(JSON.stringify([sid, pid])) ?? { mode: "media", failed: false }
}

export function rememberBrowserRendering(sid: string, pid: string, value: RenderingPreference) {
  const key = JSON.stringify([sid, pid])
  preferences.delete(key)
  preferences.set(key, { ...value })
  // Keep only bounded in-memory UI choices, never browser URLs or login data.
  if (preferences.size > 256) preferences.delete(preferences.keys().next().value!)
}

export function reserveBrowserMedia(
  owner: symbol,
  release: () => Promise<boolean>,
  wanted: () => boolean,
) {
  const pending = transition.then(async () => {
    if (!wanted()) return false
    if (current && current.owner !== owner) {
      if (!(await current.release())) throw new Error("Previous browser media is still stopping")
      current = undefined
    }
    if (!wanted()) return false
    current = { owner, release }
    return true
  })
  transition = pending.then(
    () => undefined,
    () => undefined,
  )
  return pending
}

export function releaseBrowserMedia(owner: symbol, stopped: Promise<boolean>) {
  // Closing the local element happens immediately, even while another transition
  // is queued. The next owner still waits for confirmed backend cleanup.
  const pending = transition.then(async () => {
    if ((await stopped) && current?.owner === owner) current = undefined
  })
  transition = pending.catch(() => undefined)
}
