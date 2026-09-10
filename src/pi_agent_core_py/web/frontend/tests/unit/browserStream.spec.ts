import { describe, expect, it, vi } from "vitest"
import {
  BrowserStream,
  decodeBrowserFrame,
  frameMatchesViewport,
  type FrameMetadata,
} from "../../src/api/browserStream"

const metadata = {
  seq: 1,
  page: { width: 640, height: 480, view_version: 2, dpr: 2 },
  mime: "image/png",
}

describe("Browser pixel stream", () => {
  it("never stretches stale or differently cropped pixels to the current viewport", () => {
    const frame = metadata as FrameMetadata
    expect(frameMatchesViewport(1280, 960, frame)).toBe(true)
    expect(frameMatchesViewport(640, 480, { ...frame, mime: "image/jpeg" })).toBe(true)
    expect(frameMatchesViewport(640, 480, frame)).toBe(false)
    expect(frameMatchesViewport(726, 480, { ...frame, mime: "image/jpeg" })).toBe(false)
    expect(frameMatchesViewport(0, 0, frame)).toBe(false)
    expect(frameMatchesViewport(774, 581, { ...frame, page: { ...frame.page, dpr: 1.21 } })).toBe(
      true,
    )
  })
  it("decodes a bounded binary envelope and separates pixels from CSS dimensions", async () => {
    const header = new TextEncoder().encode(JSON.stringify(metadata))
    const packet = new Uint8Array(4 + header.length + 3)
    new DataView(packet.buffer).setUint32(0, header.length)
    packet.set(header, 4)
    packet.set([1, 2, 3], 4 + header.length)
    const result = await decodeBrowserFrame(packet.buffer)
    expect(result.metadata.page.width).toBe(640)
    expect(result.metadata.page.dpr).toBe(2)
    expect(result.image?.size).toBe(3)
    expect(result.image?.type).toBe("image/png")
    expect((await decodeBrowserFrame(JSON.stringify(metadata))).image).toBeNull()
  })

  it("rejects truncated or invalid metadata without echoing payloads", async () => {
    await expect(decodeBrowserFrame(new ArrayBuffer(4))).rejects.toThrow("Invalid")
    await expect(decodeBrowserFrame(JSON.stringify({ ...metadata, page: {} }))).rejects.toThrow(
      "Invalid",
    )
    await expect(
      decodeBrowserFrame(JSON.stringify({ ...metadata, page: { ...metadata.page, dpr: 20 } })),
    ).rejects.toThrow("Invalid")
  })

  it("ACKs only after display completes and releases callbacks on close", async () => {
    class Socket {
      static OPEN = 1
      static last: Socket
      readyState = 1
      binaryType = ""
      onmessage: ((event: { data: string }) => Promise<void>) | null = null
      onclose: ((event: { code: number }) => void) | null = null
      send = vi.fn()
      close = vi.fn()
      constructor(
        public url: URL,
        public protocol: string,
      ) {
        Socket.last = this
      }
    }
    vi.stubGlobal("WebSocket", Socket)
    let resolveDisplay!: () => void
    const displayed = new Promise<void>((resolve) => {
      resolveDisplay = resolve
    })
    const closed = vi.fn()
    const stream = new BrowserStream("session", "page", () => displayed, closed)
    try {
      const socket = Socket.last
      expect(socket.url.pathname).toBe("/ws/browser/session/page")
      expect(socket.protocol).toBe("pi-browser-v1")
      const handling = socket.onmessage!({ data: JSON.stringify(metadata) })
      await Promise.resolve()
      expect(socket.send).not.toHaveBeenCalled()
      resolveDisplay()
      await handling
      expect(socket.send).toHaveBeenCalledWith('{"ack":1}')
      stream.close()
      expect(socket.close).toHaveBeenCalled()
      expect(socket.onmessage).toBeNull()
      expect(closed).not.toHaveBeenCalled()
    } finally {
      stream.close()
      vi.unstubAllGlobals()
    }
  })
})
