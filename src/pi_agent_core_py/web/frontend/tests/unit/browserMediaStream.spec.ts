import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  BROWSER_MEDIA_MIME,
  BrowserMediaStream,
  browserVideoPoint,
  decodeBrowserMediaChunk,
} from "../../src/api/browserMediaStream"

const page = {
  id: "page",
  title: "Synthetic",
  url: "https://example.test/",
  dialog: null,
  width: 1920,
  height: 1080,
  dpr: 1,
  view_version: 2,
  navigation_ms: null,
}
function packet(seq = 1, generation = "generation-a", bytes = 3) {
  const header = new TextEncoder().encode(
    JSON.stringify({ type: "chunk", seq, generation, mime: BROWSER_MEDIA_MIME, page }),
  )
  const result = new Uint8Array(4 + header.length + bytes)
  new DataView(result.buffer).setUint32(0, header.length)
  result.set(header, 4)
  result.fill(1, 4 + header.length)
  return result.buffer
}
class Socket {
  static OPEN = 1
  static last: Socket
  readyState = 1
  binaryType = ""
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor(
    public url: URL,
    public protocol: string,
  ) {
    Socket.last = this
  }
  receive(data: unknown) {
    this.onmessage?.({ data })
  }
}
class Buffer extends EventTarget {
  updating = false
  ranges: [number, number][] = []
  get buffered() {
    return {
      length: this.ranges.length,
      start: (index: number) => this.ranges[index][0],
      end: (index: number) => this.ranges[index][1],
    }
  }
  appendBuffer = vi.fn(() => {
    this.updating = true
  })
  remove = vi.fn(() => {
    this.updating = true
  })
  abort = vi.fn(() => {
    this.updating = false
  })
  finish() {
    this.updating = false
    this.dispatchEvent(new Event("updateend"))
  }
}
class Source extends EventTarget {
  static instances: Source[] = []
  static isTypeSupported = vi.fn(() => true)
  readyState = "closed"
  buffer = new Buffer()
  addSourceBuffer = vi.fn(() => this.buffer)
  constructor() {
    super()
    Source.instances.push(this)
  }
  open() {
    this.readyState = "open"
    this.dispatchEvent(new Event("sourceopen"))
  }
}
const streams: BrowserMediaStream[] = []
function create(started = true) {
  const video = {
    src: "",
    currentTime: 0,
    seeking: false,
    pause: vi.fn(),
    load: vi.fn(),
    removeAttribute: vi.fn(),
  }
  const callbacks = {
    started: vi.fn(),
    page: vi.fn(),
    error: vi.fn(),
    closed: vi.fn(),
    watch: vi.fn(() => true),
  }
  const stream = new BrowserMediaStream(
    "session",
    "page",
    video as unknown as HTMLVideoElement,
    callbacks,
  )
  streams.push(stream)
  if (started)
    Socket.last.receive('{"type":"started","generation":"generation-a","page_id":"page"}')
  return { stream, video, callbacks, socket: Socket.last, source: Source.instances.at(-1)! }
}
beforeEach(() => {
  vi.useFakeTimers()
  Source.instances.length = 0
  Source.isTypeSupported.mockReturnValue(true)
  vi.stubGlobal("WebSocket", Socket)
  vi.stubGlobal("MediaSource", Source)
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = vi.fn(() => "blob:media")
      static revokeObjectURL = vi.fn()
    },
  )
})
afterEach(() => {
  streams.splice(0).forEach((stream) => stream.close())
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("Browser media transport", () => {
  it("decodes bounded WebM chunks and refuses malformed or oversized envelopes", () => {
    expect(decodeBrowserMediaChunk(packet()).bytes.byteLength).toBe(3)
    expect(() => decodeBrowserMediaChunk(packet(1, "a", 2 * 1024 * 1024 + 1))).toThrow("Invalid")
    expect(() => decodeBrowserMediaChunk(new ArrayBuffer(4))).toThrow("Invalid")
    expect(() => decodeBrowserMediaChunk(packet(0))).toThrow("Invalid")
  })
  it("serializes dependent chunks and ACKs only completed appends", () => {
    const { socket, source, callbacks } = create()
    expect(socket.url.pathname).toBe("/ws/browser-media/session/page")
    expect(socket.protocol).toBe("pi-browser-media-v2")
    expect(callbacks.started).toHaveBeenCalledWith("generation-a")
    socket.receive(packet(1))
    socket.receive(packet(2))
    expect(socket.send).not.toHaveBeenCalled()
    source.open()
    expect(source.buffer.appendBuffer).toHaveBeenCalledTimes(1)
    source.buffer.finish()
    expect(socket.send).toHaveBeenLastCalledWith('{"ack":1}')
    expect(source.buffer.appendBuffer).toHaveBeenCalledTimes(2)
    source.buffer.finish()
    expect(socket.send).toHaveBeenLastCalledWith('{"ack":2}')
    expect(callbacks.page).toHaveBeenCalledTimes(2)
  })
  it("fails closed on a gap rather than dropping a dependent chunk", () => {
    const { socket, source, callbacks } = create()
    source.open()
    socket.receive(packet(1))
    socket.receive(packet(3))
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_invalid_message")
    expect(socket.close).toHaveBeenCalledOnce()
    source.buffer.finish()
    expect(socket.send).not.toHaveBeenCalled()
  })
  it("rejects foreign generations instead of replacing the owned MediaSource", () => {
    const { socket, source, callbacks } = create()
    source.open()
    socket.receive(packet(1))
    socket.receive(packet(1, "generation-b"))
    expect(Source.instances).toHaveLength(1)
    source.buffer.finish()
    expect(socket.send).not.toHaveBeenCalled()
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_invalid_message")
    expect(callbacks.started).toHaveBeenCalledOnce()
  })
  it("requires the started receipt before accepting chunks or generation ownership", () => {
    const { socket, callbacks } = create(false)
    socket.receive(packet())
    expect(callbacks.started).not.toHaveBeenCalled()
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_invalid_message")
  })
  it.each([
    { type: "started", generation: "generation-a", page_id: "foreign-page" },
    { type: "started", generation: "invalid token", page_id: "page" },
    { type: "started", generation: "generation-a", page_id: "page", extra: true },
  ])("rejects a malformed started receipt %j", (message) => {
    const { socket, callbacks } = create(false)
    socket.receive(JSON.stringify(message))
    expect(callbacks.started).not.toHaveBeenCalled()
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_invalid_message")
  })
  it("never accepts a duplicate started receipt even with the same generation", () => {
    const { socket, callbacks } = create()
    socket.receive('{"type":"started","generation":"generation-a","page_id":"page"}')
    expect(callbacks.started).toHaveBeenCalledOnce()
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_invalid_message")
  })
  it("bounds the sum of pending and in-flight bytes at 8 MiB", () => {
    const { socket, callbacks } = create()
    for (let sequence = 1; sequence <= 5; sequence++)
      socket.receive(packet(sequence, "generation-a", 2 * 1024 * 1024))
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_slow_consumer")
    expect(socket.send).not.toHaveBeenCalled()
  })
  it("responds to heartbeats only while the view is eligible for watch renewal", () => {
    const { socket, callbacks } = create()
    socket.receive('{"type":"heartbeat"}')
    expect(socket.send).toHaveBeenCalledTimes(1)
    expect(socket.send).toHaveBeenCalledWith('{"watch":true}')
    callbacks.watch.mockReturnValue(false)
    socket.receive('{"type":"heartbeat"}')
    expect(socket.send).toHaveBeenCalledTimes(1)
    socket.receive('{"type":"error","code":"sensitive arbitrary payload"}')
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_unavailable")
  })
  it("removes played history while retaining three seconds before appending more", () => {
    const { socket, source, video } = create()
    source.open()
    source.buffer.ranges = [[0, 10]]
    video.currentTime = 8
    socket.receive(packet(1))
    expect(source.buffer.remove).toHaveBeenCalledWith(0, 5)
    expect(source.buffer.appendBuffer).not.toHaveBeenCalled()
    source.buffer.ranges = [[5, 10]]
    source.buffer.finish()
    expect(source.buffer.appendBuffer).toHaveBeenCalledOnce()
  })
  it("repairs a media timestamp gap even after audio crossed it, once per boundary", () => {
    const { socket, source, video, callbacks } = create()
    source.open()
    source.buffer.ranges = [
      [0, 0.398],
      [0.7, 1.2],
    ]
    let time = 0.899848
    const seek = vi.fn((value: number) => {
      time = value
    })
    Object.defineProperty(video, "currentTime", { get: () => time, set: seek })
    socket.receive(packet(1))
    expect(seek).toHaveBeenCalledTimes(1)
    expect(seek).toHaveBeenCalledWith(0.899848)
    expect(socket.send).not.toHaveBeenCalled()
    source.buffer.finish()
    socket.receive(packet(2))
    source.buffer.finish()
    expect(seek).toHaveBeenCalledTimes(1)
    expect(socket.send).toHaveBeenLastCalledWith('{"ack":2}')
    expect(callbacks.error).not.toHaveBeenCalled()
  })
  it("waits until a gap is reached and seeks safely inside its next buffered range", () => {
    const { socket, source, video } = create()
    source.open()
    source.buffer.ranges = [
      [0, 0.398],
      [0.7, 1.2],
    ]
    video.currentTime = 0.1
    socket.receive(packet(1))
    source.buffer.finish()
    expect(video.currentTime).toBe(0.1)
    video.currentTime = 0.36
    socket.receive(packet(2))
    source.buffer.finish()
    expect(video.currentTime).toBeCloseTo(0.71)
  })
  it("permits only two repairs without ten newly presented frames", () => {
    const { socket, source, video, callbacks } = create()
    source.open()
    for (let index = 0; index < 3; index++) {
      source.buffer.ranges = [
        [index, index + 0.398],
        [index + 0.7, index + 1.2],
      ]
      video.currentTime = index + 0.8
      socket.receive(packet(index + 1))
      source.buffer.finish()
    }
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_stream_failed")
    expect(socket.send).toHaveBeenCalledTimes(2)
    expect(socket.close).toHaveBeenCalledOnce()
  })
  it("uses presented callbacks for recovery but keeps a four-repair generation budget", () => {
    const { socket, source, video, stream, callbacks } = create()
    source.open()
    for (let index = 0; index < 5; index++) {
      for (let frame = 0; frame < 10; frame++) stream.notePresentedFrame()
      source.buffer.ranges = [
        [index, index + 0.398],
        [index + 0.7, index + 1.2],
      ]
      video.currentTime = index + 0.8
      socket.receive(packet(index + 1))
      source.buffer.finish()
    }
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_stream_failed")
    expect(socket.send).toHaveBeenCalledTimes(4)
  })
  it("keeps ownership fixed even after exhausting gap repairs", () => {
    const { socket, source, video, callbacks } = create()
    source.open()
    for (let index = 0; index < 2; index++) {
      source.buffer.ranges = [
        [index, index + 0.398],
        [index + 0.7, index + 1.2],
      ]
      video.currentTime = index + 0.8
      socket.receive(packet(index + 1))
      source.buffer.finish()
    }
    socket.receive(packet(1, "generation-b"))
    expect(Source.instances).toHaveLength(1)
    expect(callbacks.error).toHaveBeenCalledWith("browser_media_invalid_message")
    expect(socket.send).toHaveBeenLastCalledWith('{"ack":2}')
  })
  it("stops a stalled append or more than eight seconds of unconsumed media", () => {
    const first = create()
    first.source.open()
    first.socket.receive(packet())
    vi.advanceTimersByTime(8000)
    expect(first.callbacks.error).toHaveBeenCalledWith("browser_media_slow_consumer")
    const second = create()
    second.source.open()
    second.source.buffer.ranges = [[0, 9]]
    second.socket.receive(packet())
    expect(second.callbacks.error).toHaveBeenCalledWith("browser_media_slow_consumer")
  })
  it("releases listeners, URLs, pending chunks, and playback on close without retrying", () => {
    const { socket, source, stream, video, callbacks } = create()
    source.open()
    socket.receive(packet())
    stream.close()
    source.buffer.finish()
    expect(socket.onmessage).toBeNull()
    expect(socket.onclose).toBeNull()
    expect(socket.send).not.toHaveBeenCalled()
    expect(video.pause).toHaveBeenCalledOnce()
    expect(video.removeAttribute).toHaveBeenCalledWith("src")
    expect(URL.revokeObjectURL).toHaveBeenCalled()
    expect(callbacks.closed).not.toHaveBeenCalled()
  })
  it("maps contained video pixels and ignores the letterbox", () => {
    const rect = { left: 10, top: 20, width: 640, height: 480 }
    expect(browserVideoPoint(rect, 330, 260)).toEqual({ x: 960, y: 540 })
    expect(browserVideoPoint(rect, 330, 30)).toBeNull()
    expect(browserVideoPoint(rect, 330, 490)).toBeNull()
  })
})
