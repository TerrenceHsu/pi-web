import type { BrowserPage } from "./browser"

export interface FrameMetadata {
  seq: number
  page: BrowserPage
  mime?: "image/png" | "image/jpeg"
  capture_ms?: number
}

export function frameMatchesViewport(width: number, height: number, metadata: FrameMetadata) {
  const scale = metadata.mime === "image/png" ? metadata.page.dpr : 1
  const tolerance = metadata.mime === "image/png" ? 1 : 0
  return (
    width > 0 &&
    height > 0 &&
    Math.abs(width - metadata.page.width * scale) <= tolerance &&
    Math.abs(height - metadata.page.height * scale) <= tolerance
  )
}

export async function decodeBrowserFrame(data: ArrayBuffer | string): Promise<{
  metadata: FrameMetadata
  image: Blob | null
}> {
  let metadata: FrameMetadata
  let image: Blob | null = null
  if (typeof data === "string") {
    if (data.length > 16384) throw new Error("Invalid browser metadata")
    metadata = JSON.parse(data)
  } else {
    if (data.byteLength < 5 || data.byteLength > 8 * 1024 * 1024 + 16388)
      throw new Error("Invalid browser frame size")
    const length = new DataView(data).getUint32(0)
    if (length > 16384 || length + 4 >= data.byteLength) throw new Error("Invalid browser frame")
    metadata = JSON.parse(new TextDecoder().decode(data.slice(4, 4 + length)))
    if (!["image/png", "image/jpeg"].includes(metadata.mime ?? ""))
      throw new Error("Invalid browser image format")
    image = new Blob([data.slice(4 + length)], { type: metadata.mime })
  }
  if (
    !Number.isSafeInteger(metadata.seq) ||
    metadata.seq < 1 ||
    !metadata.page ||
    !Number.isInteger(metadata.page.width) ||
    !Number.isInteger(metadata.page.height) ||
    metadata.page.width < 320 ||
    metadata.page.width > 1920 ||
    metadata.page.height < 200 ||
    metadata.page.height > 1400 ||
    !Number.isSafeInteger(metadata.page.view_version) ||
    metadata.page.view_version < 0 ||
    !Number.isFinite(metadata.page.dpr) ||
    metadata.page.dpr < 1 ||
    metadata.page.dpr > 2
  )
    throw new Error("Invalid browser metadata")
  return { metadata, image }
}

export class BrowserStream {
  private socket: WebSocket
  private closed = false

  constructor(
    sid: string,
    pid: string,
    display: (metadata: FrameMetadata, image: Blob | null) => Promise<void>,
    disconnected: (code: number) => void,
  ) {
    const url = new URL(
      `/ws/browser/${encodeURIComponent(sid)}/${encodeURIComponent(pid)}`,
      window.location.href,
    )
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
    this.socket = new WebSocket(url, "pi-browser-v1")
    this.socket.binaryType = "arraybuffer"
    this.socket.onmessage = async (event) => {
      try {
        const { metadata, image } = await decodeBrowserFrame(event.data)
        if (this.closed) return
        await display(metadata, image)
        if (!this.closed && this.socket.readyState === WebSocket.OPEN)
          this.socket.send(JSON.stringify({ ack: metadata.seq }))
      } catch {
        if (!this.closed) this.socket.close(1002, "Invalid frame")
      }
    }
    this.socket.onclose = (event) => {
      if (!this.closed) disconnected(event.code)
    }
  }

  close() {
    this.closed = true
    this.socket.onclose = null
    this.socket.onmessage = null
    this.socket.close()
  }
}
