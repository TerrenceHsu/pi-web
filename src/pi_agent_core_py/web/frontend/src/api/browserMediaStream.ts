import type { BrowserPage } from "./browser"

export const BROWSER_MEDIA_MIME = "video/webm;codecs=vp8,opus"
const CHUNK_LIMIT = 2 * 1024 * 1024
const QUEUE_LIMIT = 8 * 1024 * 1024
const TIMEOUT_MS = 8000
const ERROR_CODES = new Set([
  "browser_media_unavailable",
  "browser_media_unsupported",
  "browser_media_capture_failed",
  "browser_media_stream_failed",
  "browser_media_slow_consumer",
  "browser_media_invalid_message",
  "browser_media_busy",
  "browser_media_dependency_missing",
  "browser_media_limit",
  "browser_media_disabled",
  "browser_media_codec_unsupported",
  "browser_media_protocol_error",
  "browser_media_timeout",
  "browser_capture_failed",
  "browser_page_not_found",
  "browser_session_deleted",
  "browser_stream_limit",
  "browser_start_failed",
  "browser_dependency_missing",
  "browser_timeout",
  "browser_busy",
  "browser_action_failed",
  "browser_unavailable",
])

export interface MediaChunk {
  metadata: {
    type: "chunk"
    generation: string
    seq: number
    mime: typeof BROWSER_MEDIA_MIME
    page: BrowserPage
  }
  bytes: ArrayBuffer
}

export function decodeBrowserMediaChunk(data: ArrayBuffer): MediaChunk {
  if (
    !(data instanceof ArrayBuffer) ||
    data.byteLength < 6 ||
    data.byteLength > CHUNK_LIMIT + 16388
  )
    throw new Error("Invalid browser media chunk")
  const length = new DataView(data).getUint32(0)
  if (
    length < 2 ||
    length > 16384 ||
    length + 4 >= data.byteLength ||
    data.byteLength - length - 4 > CHUNK_LIMIT
  )
    throw new Error("Invalid browser media header")
  const metadata = JSON.parse(new TextDecoder().decode(data.slice(4, 4 + length)))
  if (
    metadata?.type !== "chunk" ||
    metadata.mime !== BROWSER_MEDIA_MIME ||
    typeof metadata.generation !== "string" ||
    !/^[a-zA-Z0-9_-]{1,128}$/.test(metadata.generation) ||
    !Number.isSafeInteger(metadata.seq) ||
    metadata.seq < 1 ||
    metadata.page?.width !== 1920 ||
    metadata.page?.height !== 1080 ||
    metadata.page?.dpr !== 1 ||
    !Number.isSafeInteger(metadata.page?.view_version) ||
    metadata.page.view_version < 0 ||
    typeof metadata.page?.id !== "string" ||
    typeof metadata.page?.url !== "string" ||
    typeof metadata.page?.title !== "string" ||
    (metadata.page?.dialog !== null && typeof metadata.page?.dialog !== "string")
  )
    throw new Error("Invalid browser media metadata")
  return { metadata, bytes: data.slice(4 + length) }
}

/** Map an object-fit:contain video point; reject clicks in the letterbox. */
export function browserVideoPoint(
  rect: Pick<DOMRect, "width" | "height" | "left" | "top">,
  x: number,
  y: number,
) {
  const scale = Math.min(rect.width / 1920, rect.height / 1080)
  if (scale <= 0) return null
  const left = rect.left + (rect.width - 1920 * scale) / 2
  const top = rect.top + (rect.height - 1080 * scale) / 2
  const px = (x - left) / scale
  const py = (y - top) / scale
  if (px < 0 || py < 0 || px >= 1920 || py >= 1080) return null
  return { x: px, y: py }
}

interface MediaCallbacks {
  started?: (generation: string) => void
  page: (page: BrowserPage, generation: string) => void
  error: (code: string) => void
  closed: (code: number) => void
  watch: () => boolean
}

/** Lossless, bounded MSE delivery. ACK means SourceBuffer accepted the complete chunk. */
export class BrowserMediaStream {
  static supported() {
    return typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(BROWSER_MEDIA_MIME)
  }

  private socket: WebSocket
  private closed = false
  private source?: MediaSource
  private buffer?: SourceBuffer
  private url = ""
  private generation = ""
  private lastSequence = 0
  private queue: MediaChunk[] = []
  private queuedBytes = 0
  private inflight?: MediaChunk
  private operation?: "append" | "remove"
  private timer?: ReturnType<typeof setTimeout>
  private presentedFrames = 0
  private framesAtRepair = 0
  private gapRepairs = 0
  private consecutiveGapRepairs = 0
  private repairedBoundary = -Infinity

  constructor(
    sid: string,
    private pid: string,
    private video: HTMLVideoElement,
    private callbacks: MediaCallbacks,
  ) {
    if (!BrowserMediaStream.supported()) throw new Error("browser_media_unsupported")
    const url = new URL(
      `/ws/browser-media/${encodeURIComponent(sid)}/${encodeURIComponent(pid)}`,
      window.location.href,
    )
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
    this.socket = new WebSocket(url, "pi-browser-media-v2")
    this.socket.binaryType = "arraybuffer"
    this.resetSource()
    this.socket.onmessage = (event) => this.receive(event.data)
    this.socket.onclose = (event) => {
      if (this.closed) return
      this.cleanup()
      callbacks.closed(event.code)
    }
  }

  private receive(data: unknown) {
    if (this.closed) return
    try {
      if (typeof data === "string") {
        if (data.length > 1024) throw new Error("Invalid media message")
        const message = JSON.parse(data)
        if (
          message?.type === "started" &&
          Object.keys(message).length === 3 &&
          typeof message.generation === "string" &&
          /^[a-zA-Z0-9_-]{1,128}$/.test(message.generation) &&
          message.page_id === this.pid &&
          !this.generation
        ) {
          this.generation = message.generation
          this.callbacks.started?.(message.generation)
          return
        }
        if (message?.type === "heartbeat" && Object.keys(message).length === 1) {
          if (this.callbacks.watch() && this.socket.readyState === WebSocket.OPEN)
            this.socket.send(JSON.stringify({ watch: true }))
          return
        }
        if (message?.type === "error" && Object.keys(message).length === 2) {
          this.fail(ERROR_CODES.has(message.code) ? message.code : "browser_media_unavailable")
          return
        }
        throw new Error("Invalid media message")
      }
      const chunk = decodeBrowserMediaChunk(data as ArrayBuffer)
      if (chunk.metadata.page.id !== this.pid) throw new Error("Wrong media page")
      if (!this.generation || chunk.metadata.generation !== this.generation)
        throw new Error("Media generation does not match the owned stream")
      if (chunk.metadata.seq !== this.lastSequence + 1)
        throw new Error("Discontinuous media stream")
      if (this.queuedBytes + chunk.bytes.byteLength > QUEUE_LIMIT) {
        this.fail("browser_media_slow_consumer")
        return
      }
      this.lastSequence = chunk.metadata.seq
      this.queuedBytes += chunk.bytes.byteLength
      this.queue.push(chunk)
      this.pump()
    } catch {
      this.fail("browser_media_invalid_message")
    }
  }

  private resetSource() {
    this.releaseSource()
    this.queue = []
    this.queuedBytes = 0
    this.inflight = undefined
    this.operation = undefined
    this.presentedFrames = 0
    this.framesAtRepair = 0
    this.gapRepairs = 0
    this.consecutiveGapRepairs = 0
    this.repairedBoundary = -Infinity
    this.source = new MediaSource()
    this.source.addEventListener("sourceopen", this.opened)
    this.source.addEventListener("sourceclose", this.sourceClosed)
    this.url = URL.createObjectURL(this.source)
    this.video.src = this.url
    this.armTimeout()
  }

  private opened = () => {
    if (this.closed || this.buffer || this.source?.readyState !== "open") return
    try {
      clearTimeout(this.timer)
      this.buffer = this.source.addSourceBuffer(BROWSER_MEDIA_MIME)
      this.buffer.addEventListener("updateend", this.updated)
      this.buffer.addEventListener("error", this.sourceError)
      this.pump()
    } catch {
      this.fail("browser_media_unsupported")
    }
  }

  private sourceClosed = () => this.fail("browser_media_stream_failed")
  private sourceError = () => this.fail("browser_media_stream_failed")

  private armTimeout() {
    clearTimeout(this.timer)
    this.timer = setTimeout(() => this.fail("browser_media_slow_consumer"), TIMEOUT_MS)
  }

  private updated = () => {
    if (this.closed) return
    clearTimeout(this.timer)
    if (this.operation === "append" && this.inflight) {
      const { metadata, bytes } = this.inflight
      this.queuedBytes -= bytes.byteLength
      this.inflight = undefined
      this.callbacks.page(metadata.page, metadata.generation)
      if (this.socket.readyState === WebSocket.OPEN)
        this.socket.send(JSON.stringify({ ack: metadata.seq }))
    }
    this.operation = undefined
    this.pump()
  }

  /** Called only by the view's requestVideoFrameCallback, not by decode/append counters. */
  notePresentedFrame() {
    if (!this.closed) this.presentedFrames++
  }

  private repairGap(ranges: TimeRanges) {
    if (this.video.seeking || ranges.length < 2) return true
    let nextRange: { start: number; end: number } | undefined
    for (let index = 1; index < ranges.length; index++) {
      const start = ranges.start(index)
      const end = ranges.end(index)
      if (
        start > this.repairedBoundary + 0.001 &&
        this.video.currentTime >= ranges.end(index - 1) - 0.05 &&
        end - start > 0.07
      )
        nextRange = { start, end }
    }
    if (!nextRange) return true
    if (this.presentedFrames - this.framesAtRepair >= 10) this.consecutiveGapRepairs = 0
    if (this.gapRepairs >= 4 || this.consecutiveGapRepairs >= 2) {
      this.fail("browser_media_stream_failed")
      return false
    }
    this.repairedBoundary = nextRange.start
    this.gapRepairs++
    this.consecutiveGapRepairs++
    this.framesAtRepair = this.presentedFrames
    // Tab capture can have a timestamp hole even while audio advances past it.
    // A single seek into the next range restarts both tracks together; timestamps
    // and chunk ordering stay intact, and a boundary is never repaired twice.
    this.video.currentTime = Math.min(
      Math.max(this.video.currentTime, nextRange.start + 0.01),
      nextRange.end - 0.05,
    )
    return true
  }

  private pump() {
    if (this.closed || !this.buffer || this.buffer.updating || this.operation) return
    try {
      const ranges = this.buffer.buffered
      if (ranges.length) {
        const start = ranges.start(0)
        const end = ranges.end(ranges.length - 1)
        // MediaRecorder may start at a small positive timestamp rather than exactly zero.
        if (this.video.currentTime < start) this.video.currentTime = start
        if (!this.repairGap(ranges)) return
        const removeBefore = this.video.currentTime - 3
        if (removeBefore > start + 0.5) {
          this.operation = "remove"
          this.armTimeout()
          this.buffer.remove(start, Math.min(removeBefore, end))
          return
        }
        if (end - start > 8 || end - this.video.currentTime > 8) {
          this.fail("browser_media_slow_consumer")
          return
        }
      }
      const chunk = this.queue.shift()
      if (!chunk) return
      this.inflight = chunk
      this.operation = "append"
      this.armTimeout()
      this.buffer.appendBuffer(chunk.bytes)
    } catch {
      this.fail("browser_media_stream_failed")
    }
  }

  private releaseSource() {
    clearTimeout(this.timer)
    this.source?.removeEventListener("sourceopen", this.opened)
    this.source?.removeEventListener("sourceclose", this.sourceClosed)
    this.buffer?.removeEventListener("updateend", this.updated)
    this.buffer?.removeEventListener("error", this.sourceError)
    if (this.buffer?.updating && this.source?.readyState === "open") {
      try {
        this.buffer.abort()
      } catch {
        /* Already detached. */
      }
    }
    this.buffer = undefined
    this.source = undefined
    if (this.url) URL.revokeObjectURL(this.url)
    this.url = ""
  }

  private cleanup() {
    this.closed = true
    this.socket.onmessage = null
    this.socket.onclose = null
    this.releaseSource()
    this.queue = []
    this.inflight = undefined
    this.queuedBytes = 0
    this.video.pause()
    this.video.removeAttribute("src")
    this.video.load()
  }

  private fail(code: string) {
    if (this.closed) return
    this.close()
    this.callbacks.error(code)
  }

  close() {
    if (this.closed) return
    this.cleanup()
    this.socket.close()
  }
}
