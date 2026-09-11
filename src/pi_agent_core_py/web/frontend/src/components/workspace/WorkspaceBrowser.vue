<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import * as api from "../../api/browser"
import { ApiError } from "../../api/client"
import { BrowserStream, frameMatchesViewport, type FrameMetadata } from "../../api/browserStream"
import { BrowserMediaStream, browserVideoPoint } from "../../api/browserMediaStream"
import {
  browserRenderingPreference,
  rememberBrowserRendering,
  releaseBrowserMedia,
  reserveBrowserMedia,
} from "../../api/browserMediaCoordinator"

const props = withDefaults(
  defineProps<{ sessionId: string; pageId: string; active: boolean; mediaPreferred?: boolean }>(),
  { mediaPreferred: true },
)
const address = ref("")
const editingAddress = ref(false)
const error = ref("")
const pageInfo = ref<api.BrowserPage | null>(null)
const imageUrl = ref("")
const screen = ref<HTMLImageElement | null>(null)
const video = ref<HTMLVideoElement | null>(null)
const mediaMode = ref(false)
const mediaStarting = ref(false)
const mediaPlaying = ref(false)
const mediaWaiting = ref(false)
const playRequired = ref(false)
const muted = ref(true)
const savedRendering = browserRenderingPreference(props.sessionId, props.pageId)
const renderingMode = ref<"media" | "screenshots">(savedRendering.mode)
const mediaFailed = ref(savedRendering.failed)
const mediaStatic = ref(false)
const volume = ref(0.8)
const viewport = ref<HTMLElement | null>(null)
const keyboard = ref<HTMLTextAreaElement | null>(null)
const navigating = ref(false)
const composing = ref(false)
const promptText = ref("")
const hd = ref(true)
const connected = ref(false)
const pixels = ref("")
const fps = ref(0)
const renderedPage = ref<api.BrowserPage | null>(null)
const frameCurrent = computed(
  () => connected.value && renderedPage.value?.view_version === pageInfo.value?.view_version,
)
let generation = 0
let retryTimer: ReturnType<typeof setTimeout> | undefined
let resizeTimer: ReturnType<typeof setTimeout> | undefined
let inputTimer: ReturnType<typeof setTimeout> | undefined
let observer: ResizeObserver | undefined
let stream: BrowserStream | undefined
let mediaStream: BrowserMediaStream | undefined
let mediaTarget: { sid: string; pid: string; generation?: string } | undefined
let mediaStopping: Promise<boolean> | undefined
type MediaStartupResult = "ready" | "failed" | "cancelled"
let mediaStartup:
  | {
      promise: Promise<MediaStartupResult>
      finish: (result: MediaStartupResult) => void
      timer: ReturnType<typeof setTimeout>
    }
  | undefined
const mediaOwner = Symbol("workspace-browser-media")
let viewRevision = 0
let unmounted = false
let mediaResize: Promise<api.BrowserPage> | undefined
let videoFrameCallback: number | undefined
let mediaHealthTimer: ReturnType<typeof setInterval> | undefined
let lastVideoFrameAt = 0
let lastMediaSourceAt = 0
let lastMediaClockAt = 0
let presentedVideo = false
let retries = 0
let resizing: Promise<void> | undefined
let forceResizePending = false
let pendingText = ""
let pendingWheel: { dx: number; dy: number } | null = null
let frameTimes: number[] = []
let queue = Promise.resolve()
let queued = 0
function update(info: api.BrowserPage) {
  if (pageInfo.value && info.view_version < pageInfo.value.view_version) return
  pageInfo.value = info
  if (!editingAddress.value) address.value = info.url === "about:blank" ? "" : info.url
}
async function resize(force = false) {
  if (mediaMode.value) return
  const version = generation
  forceResizePending ||= force
  if (resizing) {
    await resizing
    if (version !== generation) return
    return resize()
  }
  const rect = viewport.value?.getBoundingClientRect()
  if (!rect || rect.width <= 0 || rect.height <= 0) return
  const width = Math.min(1920, Math.max(320, Math.floor(rect.width)))
  const height = Math.min(1400, Math.max(200, Math.floor(rect.height)))
  const dpr =
    Math.floor(Math.min(hd.value ? 2 : 1, Math.sqrt(4_000_000 / (width * height))) * 100) / 100
  const info = pageInfo.value
  if (!forceResizePending && info?.width === width && info.height === height && info.dpr === dpr)
    return
  forceResizePending = false
  resizing = (async () => {
    try {
      const result = await api.action(props.sessionId, props.pageId, {
        action: "resize",
        width,
        height,
        dpr,
      })
      if (version === generation) update(result)
    } catch (cause) {
      if (version === generation) error.value = (cause as Error).message
    }
  })().finally(() => {
    resizing = undefined
  })
  await resizing
}
function scheduleResize() {
  clearTimeout(resizeTimer)
  if (!mediaMode.value && props.active && !document.hidden)
    resizeTimer = setTimeout(() => void resize(), 90)
}
async function display(metadata: FrameMetadata, frame: Blob | null, version: number) {
  if (version !== generation) return
  const info = metadata.page
  if (info.view_version < (pageInfo.value?.view_version ?? 0)) return
  update(info)
  const now = performance.now()
  frameTimes = frameTimes.filter((time) => now - time < 2000)
  fps.value = Math.round(frameTimes.length / 2)
  if (!frame) return
  const url = URL.createObjectURL(frame)
  try {
    const decoded = new Image()
    decoded.src = url
    await decoded.decode()
    if (
      version !== generation ||
      info.view_version < (pageInfo.value?.view_version ?? 0) ||
      !frameMatchesViewport(decoded.naturalWidth, decoded.naturalHeight, metadata)
    ) {
      URL.revokeObjectURL(url)
      return
    }
    const previous = imageUrl.value
    imageUrl.value = url
    renderedPage.value = info
    connected.value = true
    retries = 0
    pixels.value = `${decoded.naturalWidth} × ${decoded.naturalHeight}`
    frameTimes.push(now)
    await nextTick()
    await screen.value?.decode()
    if (previous) URL.revokeObjectURL(previous)
  } catch {
    URL.revokeObjectURL(url)
    throw new Error("Unable to decode browser frame")
  }
}
async function connect(forceResize = false) {
  if (!props.active || document.hidden || mediaMode.value) return
  const version = generation
  try {
    if (mediaStopping) await mediaStopping
    if (version !== generation || mediaMode.value) return
    await nextTick()
    const info = await api.info(props.sessionId, props.pageId)
    if (version !== generation) return
    update(info)
    if (info.capture_error && !forceResize) {
      error.value =
        "The browser frame source could not recover. Reconnect this view or reopen the tab."
      return
    }
    await resize(forceResize)
    if (version !== generation) return
    stream?.close()
    stream = new BrowserStream(
      props.sessionId,
      props.pageId,
      (metadata, frame) => display(metadata, frame, version),
      (code) => {
        if (version !== generation) return
        connected.value = false
        if ([4401, 4403, 4404].includes(code) || retries >= 3) {
          error.value =
            "Browser view unavailable. Sign in if needed, then reconnect this view or reopen the tab."
          return
        }
        retryTimer = setTimeout(() => void connect(), 500 * 2 ** retries++)
      },
    )
    observer?.disconnect()
    observer = new ResizeObserver(scheduleResize)
    if (viewport.value) observer.observe(viewport.value)
  } catch (cause) {
    if (version === generation) error.value = (cause as Error).message
  }
}
function mediaMessage(code: string) {
  if (["browser_media_unsupported", "browser_media_codec_unsupported"].includes(code))
    return "1080p streaming is not supported by this browser. HD screenshots are available."
  if (["browser_media_busy", "browser_media_limit", "browser_stream_limit"].includes(code))
    return "Another browser view is using the account's media stream. HD screenshots are available; retry after that stream stops."
  if (code === "browser_media_slow_consumer")
    return "Video playback could not keep up. HD screenshots are available; retry 1080p streaming when ready."
  return "1080p streaming is unavailable. HD screenshots are available; retry when ready."
}
function countVideoFrames() {
  if (!video.value?.requestVideoFrameCallback || !mediaMode.value) return
  const version = generation
  videoFrameCallback = video.value.requestVideoFrameCallback((now) => {
    if (version !== generation || !mediaMode.value || !video.value) return
    mediaStream?.notePresentedFrame()
    presentedVideo = true
    lastVideoFrameAt = performance.now()
    mediaPlaying.value = !video.value.paused
    mediaWaiting.value = false
    mediaStatic.value = false
    frameTimes = frameTimes.filter((time) => now - time < 2000)
    frameTimes.push(now)
    fps.value =
      frameTimes.length > 1
        ? Math.round(((frameTimes.length - 1) * 1000) / Math.max(1, now - frameTimes[0]))
        : 0
    pixels.value = `${video.value.videoWidth} × ${video.value.videoHeight}`
    countVideoFrames()
  })
}
function watchMediaHealth() {
  clearInterval(mediaHealthTimer)
  const version = generation
  lastVideoFrameAt = performance.now()
  lastMediaSourceAt = lastVideoFrameAt
  lastMediaClockAt = lastVideoFrameAt
  presentedVideo = false
  let previousTime = video.value?.currentTime ?? 0
  mediaHealthTimer = setInterval(() => {
    if (version !== generation || !mediaMode.value) return
    const now = performance.now()
    const currentTime = video.value?.currentTime ?? 0
    if (!video.value?.paused && currentTime > previousTime + 0.001) lastMediaClockAt = now
    previousTime = currentTime
    const staleMs = now - lastVideoFrameAt
    // A static tab can provide continuous audio/container timestamps without
    // presenting new video frames. Source progress is not proof of playback:
    // require an advancing element clock too, and never synthesize FPS.
    mediaStatic.value =
      presentedVideo &&
      staleMs >= 2000 &&
      now - lastMediaSourceAt < 2000 &&
      now - lastMediaClockAt < 2000
    if (staleMs >= 2000) {
      fps.value = 0
      mediaPlaying.value = false
    }
    if (now - lastMediaSourceAt >= 8000 || (now - lastMediaClockAt >= 8000 && !playRequired.value))
      failMedia(
        "Video playback stopped progressing. HD screenshots are available; retry 1080p streaming when ready.",
      )
  }, 500)
}
async function playMedia() {
  if (!mediaMode.value || !video.value) return
  const version = generation
  video.value.muted = muted.value
  video.value.volume = volume.value
  try {
    await video.value.play()
    if (version !== generation) return
    playRequired.value = false
    mediaPlaying.value = true
  } catch {
    if (version !== generation) return
    mediaPlaying.value = false
    mediaWaiting.value = false
    playRequired.value = true
  }
}
function mediaWanted() {
  return (
    !unmounted &&
    props.active &&
    props.mediaPreferred &&
    !document.hidden &&
    renderingMode.value === "media" &&
    !mediaFailed.value
  )
}
function failMedia(message: string) {
  mediaFailed.value = true
  void stopMedia(true, message, "failed")
}
function finishMediaStartup(result: MediaStartupResult) {
  const startup = mediaStartup
  mediaStartup = undefined
  mediaStarting.value = false
  if (!startup) return
  clearTimeout(startup.timer)
  startup.finish(result)
}
function soundChanged(event: Event) {
  muted.value = !(event.target as HTMLInputElement).checked
  // The unmute and play call both run inside the actual user gesture, including
  // browsers that reject an asynchronous watcher as sound authorization.
  if (video.value) {
    video.value.muted = muted.value
    video.value.volume = volume.value
    if (!muted.value) void playMedia()
  }
}
async function startMedia() {
  if (mediaMode.value || mediaStarting.value || !mediaWanted()) return
  if (!BrowserMediaStream.supported()) {
    mediaFailed.value = true
    error.value = mediaMessage("browser_media_unsupported")
    void connect()
    return
  }
  reset()
  const version = generation
  const target: NonNullable<typeof mediaTarget> = { sid: props.sessionId, pid: props.pageId }
  mediaMode.value = true
  mediaStarting.value = true
  let finish!: (result: MediaStartupResult) => void
  const promise = new Promise<MediaStartupResult>((resolve) => {
    finish = resolve
  })
  mediaStartup = {
    promise,
    finish,
    timer: setTimeout(() => {
      if (version === generation && mediaStarting.value)
        failMedia(
          "1080p streaming did not start in time. HD screenshots are available; retry when ready.",
        )
    }, 10_000),
  }
  mediaWaiting.value = true
  playRequired.value = false
  muted.value = true
  mediaStatic.value = false
  error.value = ""
  try {
    if (mediaStopping && !(await mediaStopping)) throw new Error("Media stop unconfirmed")
    const reserved = await reserveBrowserMedia(
      mediaOwner,
      () => stopMedia(false),
      () => version === generation && mediaWanted(),
    )
    if (!reserved || version !== generation) return
    mediaTarget = target
    if (resizing) await resizing
    await queue
    if (version !== generation) return
    const request = api.action(target.sid, target.pid, {
      action: "resize",
      width: 1920,
      height: 1080,
      dpr: 1,
    })
    mediaResize = request
    const info = await request.finally(() => {
      if (mediaResize === request) mediaResize = undefined
    })
    if (version !== generation) return
    update(info)
    await nextTick()
    if (version !== generation || !video.value) return
    mediaStream = new BrowserMediaStream(target.sid, target.pid, video.value, {
      started: (token) => {
        if (version === generation) {
          target.generation = token
          finishMediaStartup("ready")
        }
      },
      page: (info, token) => {
        if (version !== generation) return
        target.generation = token
        lastMediaSourceAt = performance.now()
        update(info)
        renderedPage.value = info
        connected.value = true
      },
      error: (code) => {
        if (version === generation) failMedia(mediaMessage(code))
      },
      closed: () => {
        if (version === generation) failMedia(mediaMessage("browser_media_unavailable"))
      },
      watch: () =>
        version === generation &&
        props.active &&
        !document.hidden &&
        mediaMode.value &&
        !playRequired.value &&
        (mediaPlaying.value || mediaWaiting.value || mediaStatic.value),
    })
    countVideoFrames()
    watchMediaHealth()
    void playMedia()
  } catch {
    if (version === generation) failMedia(mediaMessage("browser_media_unavailable"))
  }
}
async function stopMedia(
  restore = true,
  message = "",
  startupResult: MediaStartupResult = "cancelled",
) {
  if (mediaStopping) return mediaStopping
  finishMediaStartup(startupResult)
  const target = mediaTarget
  mediaTarget = undefined
  muted.value = true
  if (video.value) video.value.muted = true
  mediaStream?.close()
  mediaStream = undefined
  clearInterval(mediaHealthTimer)
  mediaHealthTimer = undefined
  if (videoFrameCallback !== undefined) video.value?.cancelVideoFrameCallback?.(videoFrameCallback)
  videoFrameCallback = undefined
  mediaMode.value = false
  mediaStarting.value = false
  mediaPlaying.value = false
  mediaWaiting.value = false
  mediaStatic.value = false
  playRequired.value = false
  reset()
  const version = generation
  const pending = (async () => {
    const abort = new AbortController()
    let timeout: ReturnType<typeof setTimeout> | undefined
    try {
      const cleanup = (async () => {
        if (mediaResize) await mediaResize.catch(() => undefined)
        if (resizing) await resizing
        if (abort.signal.aborted) throw new Error("Media stop timed out")
        // An early quota rejection belongs to no local generation. Never use a
        // page-wide stop which could terminate another application window.
        if (target?.generation)
          await api.action(
            target.sid,
            target.pid,
            {
              action: "media_stop",
              media_generation: target.generation,
            },
            abort.signal,
          )
      })()
      await Promise.race([
        cleanup,
        new Promise<never>((_resolve, reject) => {
          timeout = setTimeout(() => {
            abort.abort()
            reject(new Error("Media stop timed out"))
          }, 10_000)
        }),
      ])
      return true
    } catch (cause) {
      if (
        target?.generation &&
        cause instanceof ApiError &&
        cause.status === 404 &&
        ["browser_page_not_found", "browser_session_not_found"].includes(
          cause.payload?.detail?.code,
        )
      )
        return true
      if (version === generation)
        error.value = "Unable to confirm that video stopped. Reconnect this view."
      if (target) mediaTarget = target
      return false
    } finally {
      clearTimeout(timeout)
    }
  })()
  mediaStopping = pending
  releaseBrowserMedia(mediaOwner, pending)
  try {
    const stopped = await pending
    if (stopped && version === generation && restore && props.active && !document.hidden) {
      if (mediaStopping === pending) mediaStopping = undefined
      await connect(true)
    }
    if (version === generation && message) error.value = message
    return stopped
  } finally {
    if (mediaStopping === pending) mediaStopping = undefined
  }
}
async function act(body: Record<string, unknown>) {
  const waiting = mediaStartup
  const originalGeneration = generation
  const revision = viewRevision
  const target = { sid: props.sessionId, pid: props.pageId }
  if (waiting && ["navigate", "back", "forward", "reload"].includes(String(body.action))) {
    const outcome = await waiting.promise
    // Capture's activeTab grant must be consumed before a cross-origin
    // navigation can revoke it. On failed startup, wait for scoped cleanup
    // before allowing navigation in the same still-visible view.
    if (outcome === "cancelled") return
    if (outcome === "failed" && mediaStopping && !(await mediaStopping)) return
    if (
      unmounted ||
      revision !== viewRevision ||
      target.sid !== props.sessionId ||
      target.pid !== props.pageId ||
      (outcome === "ready" && originalGeneration !== generation)
    )
      return
  }
  const version = generation
  try {
    const result = await api.action(props.sessionId, props.pageId, body)
    if (version === generation) {
      update(result)
      error.value = ""
    }
  } catch (cause) {
    if (version === generation) error.value = (cause as Error).message
  }
}
function enqueue(body: Record<string, unknown>) {
  if (queued >= 32) {
    error.value = "Input queue is busy. Wait for the page before typing more."
    return
  }
  const version = generation
  queued++
  queue = queue
    .then(async () => {
      if (version === generation) await act(body)
    })
    .finally(() => {
      queued--
    })
}
function flushInput() {
  clearTimeout(inputTimer)
  inputTimer = undefined
  if (pendingText) {
    enqueue({ action: "text", text: pendingText })
    pendingText = ""
  }
  if (pendingWheel) {
    enqueue({ action: "wheel", ...pendingWheel })
    pendingWheel = null
  }
}
function input(body: Record<string, unknown>) {
  if (body.action === "text" && typeof body.text === "string") {
    if (pendingWheel || pendingText.length + body.text.length > 4096) flushInput()
    pendingText += body.text
    clearTimeout(inputTimer)
    inputTimer = setTimeout(flushInput, 25)
  } else {
    flushInput()
    enqueue(body)
  }
}
async function navigate() {
  const version = generation
  navigating.value = true
  editingAddress.value = false
  const url = address.value
  flushInput()
  await queue
  if (version === generation) await act({ action: "navigate", url })
  navigating.value = false
}
function point(event: MouseEvent) {
  const rect = screen.value!.getBoundingClientRect()
  const width = renderedPage.value!.width
  const height = renderedPage.value!.height
  return {
    x: Math.min(width - 1, Math.max(0, ((event.clientX - rect.left) / rect.width) * width)),
    y: Math.min(height - 1, Math.max(0, ((event.clientY - rect.top) / rect.height) * height)),
  }
}
function click(event: MouseEvent) {
  if (mediaMode.value) {
    if (!video.value?.videoWidth || !renderedPage.value) return
    const position = browserVideoPoint(
      video.value.getBoundingClientRect(),
      event.clientX,
      event.clientY,
    )
    if (!position) return
    keyboard.value?.focus({ preventScroll: true })
    input({
      action: "click",
      ...position,
      width: 1920,
      height: 1080,
      view_version: renderedPage.value.view_version,
      button: event.button === 2 ? "right" : "left",
      clicks: event.detail > 1 ? 2 : 1,
    })
    return
  }
  if (!screen.value?.naturalWidth || !imageUrl.value || !renderedPage.value) return
  keyboard.value?.focus({ preventScroll: true })
  input({
    action: "click",
    ...point(event),
    width: renderedPage.value.width,
    height: renderedPage.value.height,
    view_version: renderedPage.value.view_version,
    button: event.button === 2 ? "right" : "left",
    clicks: event.detail > 1 ? 2 : 1,
  })
}
function wheel(event: WheelEvent) {
  if (pendingText) flushInput()
  pendingWheel = {
    dx: Math.max(-1500, Math.min(1500, (pendingWheel?.dx ?? 0) + event.deltaX)),
    dy: Math.max(-1500, Math.min(1500, (pendingWheel?.dy ?? 0) + event.deltaY)),
  }
  if (!inputTimer)
    inputTimer = setTimeout(() => {
      inputTimer = undefined
      flushInput()
    }, 25)
}
function keydown(event: KeyboardEvent) {
  if (event.isComposing || composing.value || event.key === "Process" || event.key === "Dead")
    return
  if (["Control", "Shift", "Alt", "Meta"].includes(event.key)) return
  // Leave paste to its explicit ClipboardEvent, never read the system clipboard.
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") return
  event.preventDefault()
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey)
    input({ action: "text", text: event.key })
  else {
    const modifiers = [
      event.ctrlKey || event.metaKey ? "Control" : "",
      event.altKey ? "Alt" : "",
      event.shiftKey ? "Shift" : "",
    ].filter(Boolean)
    input({ action: "key", key: [...modifiers, event.key === " " ? "Space" : event.key].join("+") })
  }
}
function paste(event: ClipboardEvent) {
  event.preventDefault()
  const text = event.clipboardData?.getData("text/plain") ?? ""
  if (text.length > 4096) {
    error.value = "Paste is limited to 4,096 characters."
    return
  }
  if (text) input({ action: "text", text })
}
function compositionEnd(event: CompositionEvent) {
  composing.value = false
  if (event.data) input({ action: "text", text: event.data })
  if (keyboard.value) keyboard.value.value = ""
}
function answerDialog(accept: boolean) {
  void act({ action: "dialog", accept, text: promptText.value })
  promptText.value = ""
}
function reset() {
  generation++
  clearTimeout(retryTimer)
  clearTimeout(resizeTimer)
  clearTimeout(inputTimer)
  inputTimer = undefined
  pendingText = ""
  pendingWheel = null
  observer?.disconnect()
  stream?.close()
  stream = undefined
  connected.value = false
  frameTimes = []
  renderedPage.value = null
  pageInfo.value = null
  pixels.value = ""
  fps.value = 0
  forceResizePending = false
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ""
}
function reconnect() {
  if (renderingMode.value === "media" && props.mediaPreferred) {
    mediaFailed.value = false
    void reconcileView()
    return
  }
  if (mediaTarget) {
    void stopMedia(true)
    return
  }
  reset()
  retries = 0
  error.value = ""
  void connect(true)
}
async function reconcileView() {
  const revision = ++viewRevision
  // Stop locally before waiting for network cleanup, including rapid focus
  // changes between two visible split panes.
  if (mediaTarget || mediaMode.value || mediaStopping) {
    if (!(await stopMedia(false))) return
    if (revision !== viewRevision || unmounted) return
  }
  reset()
  if (!props.active || document.hidden || unmounted) return
  if (mediaWanted()) void startMedia()
  else void connect()
}
function visibilityChanged() {
  void reconcileView()
}
function chooseScreenshots() {
  renderingMode.value = "screenshots"
}
function renderingChanged(event: Event) {
  mediaFailed.value = false
  renderingMode.value =
    (event.target as HTMLSelectElement).value === "screenshots" ? "screenshots" : "media"
}
watch(
  () => [props.sessionId, props.pageId],
  () => {
    const saved = browserRenderingPreference(props.sessionId, props.pageId)
    renderingMode.value = saved.mode
    mediaFailed.value = saved.failed
  },
)
watch([renderingMode, mediaFailed], () => {
  rememberBrowserRendering(props.sessionId, props.pageId, {
    mode: renderingMode.value,
    failed: mediaFailed.value,
  })
})
watch(
  () => [props.sessionId, props.pageId, props.active, props.mediaPreferred, renderingMode.value],
  () => void reconcileView(),
  { immediate: true },
)
watch(hd, scheduleResize)
watch([muted, volume], () => {
  if (video.value) {
    video.value.muted = muted.value
    video.value.volume = volume.value
  }
})
onMounted(() => document.addEventListener("visibilitychange", visibilityChanged))
onBeforeUnmount(() => {
  unmounted = true
  viewRevision++
  document.removeEventListener("visibilitychange", visibilityChanged)
  if (mediaTarget || mediaMode.value) void stopMedia(false)
  else reset()
})
</script>

<template>
  <section class="browser-view" data-testid="workspace-browser">
    <form class="browser-toolbar" @submit.prevent="navigate">
      <button type="button" title="Back" :disabled="mediaStarting" @click="act({ action: 'back' })">
        ←
      </button>
      <button
        type="button"
        title="Forward"
        :disabled="mediaStarting"
        @click="act({ action: 'forward' })"
      >
        →
      </button>
      <button
        type="button"
        title="Reload"
        :disabled="mediaStarting"
        @click="act({ action: 'reload' })"
      >
        ↻
      </button>
      <input
        v-model="address"
        aria-label="Browser address"
        placeholder="https://example.com"
        autocomplete="off"
        spellcheck="false"
        @focus="editingAddress = true"
        @blur="editingAddress = false"
      />
      <button :disabled="navigating || mediaStarting" type="submit">
        {{ navigating ? "…" : "Go" }}
      </button>
    </form>
    <p class="browser-boundary">
      Private to this Session · Public websites only · No file upload/download · Login clears on
      last tab close or 15 min idle.
    </p>
    <div class="browser-performance" data-testid="browser-performance">
      <label
        ><input
          v-model="hd"
          :disabled="mediaMode"
          type="checkbox"
          aria-label="HD browser rendering"
        />
        HD</label
      >
      <span
        >{{
          mediaMode
            ? mediaPlaying
              ? "Playing"
              : playRequired
                ? "Playback paused"
                : mediaStatic
                  ? "Static page · stream healthy"
                  : "Waiting for video"
            : frameCurrent
              ? "Live"
              : connected
                ? "Updating"
                : "Connecting"
        }}
        · {{ pixels || "Waiting for frame" }} · {{ fps }} fps{{
          mediaMode ? " · target 30 fps" : ""
        }}</span
      >
      <span
        v-if="pageInfo?.navigation_ms != null"
        title="Navigation to response commit; not full page load"
        >Navigation {{ pageInfo.navigation_ms }} ms</span
      >
    </div>
    <div class="browser-media-controls">
      <select :value="renderingMode" aria-label="Browser rendering mode" @change="renderingChanged">
        <option value="media">1080p / 30 fps</option>
        <option value="screenshots">HD screenshots</option>
      </select>
      <button
        v-if="mediaFailed && renderingMode === 'media' && mediaPreferred"
        type="button"
        @click="reconnect"
      >
        Retry 1080p / 30 fps
      </button>
      <span v-if="renderingMode === 'media' && !mediaPreferred"
        >Focus this browser pane for 1080p streaming.</span
      >
      <template v-if="mediaMode">
        <button type="button" @click="chooseScreenshots">Stop video</button>
        <button v-if="playRequired" type="button" @click="playMedia">Play video</button>
        <label
          ><input
            :checked="!muted"
            type="checkbox"
            aria-label="Browser sound"
            @change="soundChanged"
          />
          Sound</label
        >
        <input
          v-model.number="volume"
          type="range"
          min="0"
          max="1"
          step="0.05"
          aria-label="Browser audio volume"
        />
        <span>Sound off on resume · hidden views stop</span>
      </template>
    </div>
    <p v-if="error" class="browser-error" role="alert">
      {{ error }} <button @click="error = ''">Dismiss</button>
      <button v-if="!connected" @click="reconnect">Reconnect</button>
    </p>
    <div v-if="pageInfo?.dialog" class="browser-dialog">
      <p>{{ pageInfo.dialog }}</p>
      <input v-model="promptText" aria-label="Website dialog response" autocomplete="off" />
      <button @click="answerDialog(true)">OK</button>
      <button @click="answerDialog(false)">Cancel</button>
    </div>
    <div ref="viewport" class="browser-screen" @wheel.prevent="wheel">
      <video
        v-show="mediaMode"
        ref="video"
        class="browser-video"
        playsinline
        :muted="muted"
        aria-label="Interactive browser video"
        @click="click"
        @contextmenu.prevent="click"
        @loadedmetadata="playMedia"
        @playing="mediaPlaying = true"
        @waiting="mediaPlaying = false"
        @stalled="mediaPlaying = false"
        @pause="mediaPlaying = false"
        @ended="mediaPlaying = false"
        @error="mediaMode && failMedia(mediaMessage('browser_media_stream_failed'))"
      />
      <img
        v-if="imageUrl && !mediaMode"
        ref="screen"
        :src="imageUrl"
        :data-width="renderedPage?.width"
        :data-height="renderedPage?.height"
        :data-view-version="renderedPage?.view_version"
        :style="{
          width: renderedPage ? `${renderedPage.width}px` : undefined,
          aspectRatio: renderedPage ? `${renderedPage.width} / ${renderedPage.height}` : undefined,
        }"
        alt="Interactive browser page"
        draggable="false"
        @click="click"
        @contextmenu.prevent="click"
      />
      <p v-else-if="!mediaMode">Connecting to local Chromium…</p>
      <textarea
        ref="keyboard"
        class="browser-keyboard"
        aria-label="Browser keyboard input"
        autocomplete="off"
        autocapitalize="off"
        :spellcheck="false"
        @keydown="keydown"
        @paste="paste"
        @compositionstart="composing = true"
        @compositionend="compositionEnd"
      />
    </div>
  </section>
</template>

<style scoped>
.browser-view {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
  min-width: 0;
}
.browser-toolbar {
  display: flex;
  gap: 3px;
  padding: 6px;
  background: #fff;
}
.browser-toolbar input {
  flex: 1;
  min-width: 50px;
  border: 1px solid var(--border, #ddd);
  border-radius: 4px;
  padding: 5px;
  font-size: 12px;
}
.browser-toolbar button {
  flex-shrink: 0;
  padding: 4px 6px;
  border: 1px solid var(--border, #ddd);
  border-radius: 4px;
  background: #f8fafc;
  cursor: pointer;
}
.browser-boundary {
  margin: 0;
  padding: 3px 7px;
  font-size: 9px;
  color: var(--muted);
}
.browser-performance {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: 16px 16px;
  height: 38px;
  box-sizing: border-box;
  flex-shrink: 0;
  gap: 0 7px;
  padding: 3px 7px;
  font-size: 10px;
  color: var(--muted);
}
.browser-performance span {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.browser-performance span:nth-of-type(2) {
  grid-column: 1 / -1;
}
.browser-performance label {
  display: flex;
  align-items: center;
  gap: 3px;
}
.browser-error,
.browser-dialog {
  font-size: 12px;
  padding: 6px;
  margin: 0;
  background: #fff3ed;
  overflow-wrap: anywhere;
}
.browser-screen {
  overflow: hidden;
  flex: 1;
  position: relative;
  min-height: 0;
  background: #e5e7eb;
}
.browser-screen img {
  display: block;
  max-width: 100%;
  width: auto;
  height: auto;
  cursor: default;
  user-select: none;
}
.browser-video {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #111827;
}
.browser-media-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 3px 7px;
  font-size: 11px;
}
.browser-media-controls label {
  display: flex;
  align-items: center;
  gap: 3px;
}
.browser-media-controls input[type="range"] {
  width: 80px;
}
.browser-keyboard {
  position: absolute;
  left: 0;
  top: 0;
  width: 1px;
  height: 1px;
  padding: 0;
  border: 0;
  opacity: 0;
  resize: none;
}
</style>
