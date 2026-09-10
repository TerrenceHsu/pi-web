import { flushPromises, mount } from "@vue/test-utils"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import WorkspaceBrowser from "../../src/components/workspace/WorkspaceBrowser.vue"
import type { BrowserPage } from "../../src/api/browser"
import type { FrameMetadata } from "../../src/api/browserStream"

const mocks = vi.hoisted(() => ({
  info: vi.fn(),
  action: vi.fn(),
  mediaSupported: vi.fn(),
  mediaConnections: [] as {
    video: HTMLVideoElement
    callbacks: {
      page: (info: BrowserPage) => void
      error: (code: string) => void
      closed: (code: number) => void
      watch: () => boolean
    }
    close: ReturnType<typeof vi.fn>
  }[],
  connections: [] as {
    display: (metadata: FrameMetadata, image: Blob | null) => Promise<void>
    close: ReturnType<typeof vi.fn>
  }[],
}))
vi.mock("../../src/api/browser", () => ({ info: mocks.info, action: mocks.action }))
vi.mock("../../src/api/browserStream", async (original) => {
  const actual = await original<typeof import("../../src/api/browserStream")>()
  return {
    ...actual,
    BrowserStream: class {
      close = vi.fn()
      constructor(
        _sid: string,
        _pid: string,
        display: (metadata: FrameMetadata, image: Blob | null) => Promise<void>,
      ) {
        mocks.connections.push({ display, close: this.close })
      }
    },
  }
})
const page: BrowserPage = {
  id: "page",
  title: "Fixture",
  url: "https://example.test/",
  dialog: null,
  width: 640,
  height: 480,
  dpr: 2,
  view_version: 1,
  navigation_ms: null,
}
vi.mock("../../src/api/browserMediaStream", async (original) => {
  const actual = await original<typeof import("../../src/api/browserMediaStream")>()
  return {
    ...actual,
    BrowserMediaStream: class {
      static supported = mocks.mediaSupported
      notePresentedFrame = vi.fn()
      close = vi.fn()
      constructor(
        _sid: string,
        _pid: string,
        video: HTMLVideoElement,
        callbacks: (typeof mocks.mediaConnections)[number]["callbacks"],
      ) {
        mocks.mediaConnections.push({ video, callbacks, close: this.close })
      }
    },
  }
})
const wrappers: ReturnType<typeof mount>[] = []
const originalDecode = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "decode")
const originalUrls = ["createObjectURL", "revokeObjectURL"].map((key) => ({
  key,
  descriptor: Object.getOwnPropertyDescriptor(URL, key),
}))
function create() {
  const wrapper = mount(WorkspaceBrowser, {
    props: { sessionId: "session", pageId: "page", active: true },
  })
  wrappers.push(wrapper)
  return wrapper
}
function metadata(version: number): FrameMetadata {
  return { seq: version, page: { ...page, view_version: version }, mime: "image/png" }
}
beforeEach(() => {
  mocks.connections.length = 0
  mocks.mediaConnections.length = 0
  mocks.mediaSupported.mockReset().mockReturnValue(true)
  mocks.info.mockReset().mockResolvedValue(page)
  mocks.action.mockReset().mockResolvedValue(page)
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined)
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {})
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  )
  vi.stubGlobal(
    "Image",
    class {
      src = ""
      naturalWidth = 1280
      naturalHeight = 960
      decode = vi.fn().mockResolvedValue(undefined)
    },
  )
  Object.defineProperty(HTMLImageElement.prototype, "decode", {
    configurable: true,
    value: vi.fn().mockResolvedValue(undefined),
  })
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 640,
    height: 480,
    left: 0,
    top: 0,
    right: 640,
    bottom: 480,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  })
  Object.defineProperties(URL, {
    createObjectURL: { configurable: true, value: vi.fn().mockReturnValue("blob:fixture") },
    revokeObjectURL: { configurable: true, value: vi.fn() },
  })
})

describe("Workspace browser video and audio", () => {
  async function enable(wrapper: ReturnType<typeof mount>) {
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Video + audio")!
      .trigger("click")
    await flushPromises()
  }
  it("starts only on explicit request, closes screenshots, and fixes the viewport at 1080p", async () => {
    const wrapper = create()
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(0)
    await enable(wrapper)
    expect(mocks.connections[0].close).toHaveBeenCalled()
    expect(mocks.action).toHaveBeenCalledWith("session", "page", {
      action: "resize",
      width: 1920,
      height: 1080,
      dpr: 1,
    })
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("target 30 fps")
    expect(
      wrapper.get('input[aria-label="HD browser rendering"]').attributes("disabled"),
    ).toBeDefined()
  })
  it("keeps screenshots when the browser cannot decode the streaming codec", async () => {
    mocks.mediaSupported.mockReturnValue(false)
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    expect(mocks.mediaConnections).toHaveLength(0)
    expect(mocks.connections[0].close).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain("not supported")
  })
  it("shows an explicit Play button if autoplay is rejected and renews only after playback", async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(
      new DOMException("Denied", "NotAllowedError"),
    )
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    const media = mocks.mediaConnections[0]
    expect(media.callbacks.watch()).toBe(false)
    const play = wrapper.findAll("button").find((button) => button.text() === "Play video + audio")!
    expect(play.exists()).toBe(true)
    await play.trigger("click")
    await flushPromises()
    expect(media.callbacks.watch()).toBe(true)
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("Playing")
  })
  it("awaits media_stop before reconnecting screenshots", async () => {
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    let finish!: (value: BrowserPage) => void
    mocks.action.mockImplementation((_sid, _pid, body) =>
      body.action === "media_stop"
        ? new Promise<BrowserPage>((resolve) => {
            finish = resolve
          })
        : Promise.resolve(page),
    )
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Stop video")!
      .trigger("click")
    await flushPromises()
    expect(mocks.mediaConnections[0].close).toHaveBeenCalledOnce()
    expect(mocks.connections).toHaveLength(1)
    finish(page)
    await flushPromises()
    expect(mocks.connections).toHaveLength(2)
  })
  it("waits for an in-flight media resize before stopping and restoring the image viewport", async () => {
    const wrapper = create()
    await flushPromises()
    let finish!: (value: BrowserPage) => void
    mocks.action.mockImplementation((_sid, _pid, body) =>
      body.action === "resize" && body.width === 1920
        ? new Promise<BrowserPage>((resolve) => {
            finish = resolve
          })
        : Promise.resolve(page),
    )
    await enable(wrapper)
    expect(mocks.mediaConnections).toHaveLength(0)
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Stop video")!
      .trigger("click")
    await flushPromises()
    expect(mocks.action).not.toHaveBeenCalledWith("session", "page", { action: "media_stop" })
    finish({ ...page, width: 1920, height: 1080, dpr: 1 })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(0)
    expect(mocks.action).toHaveBeenCalledWith("session", "page", { action: "media_stop" })
    expect(mocks.connections).toHaveLength(2)
  })
  it("does not keep showing Playing or renewing watch while buffered playback is stalled", async () => {
    const wrapper = create()
    await flushPromises()
    let frame!: VideoFrameRequestCallback
    const element = wrapper.get("video").element as HTMLVideoElement
    element.requestVideoFrameCallback = (callback) => {
      frame = callback
      return 1
    }
    Object.defineProperty(element, "paused", { configurable: true, value: false })
    await enable(wrapper)
    const media = mocks.mediaConnections[0]
    frame(performance.now(), {} as VideoFrameCallbackMetadata)
    expect(media.callbacks.watch()).toBe(true)
    await wrapper.get("video").trigger("waiting")
    expect(wrapper.get('[data-testid="browser-performance"]').text()).not.toContain("Playing")
    expect(media.callbacks.watch()).toBe(false)
    await wrapper.get("video").trigger("stalled")
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("Waiting for video")
    expect(media.close).not.toHaveBeenCalled()
  })
  it("stops on hidden tabs and does not resume media when the tab becomes active", async () => {
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    const media = mocks.mediaConnections[0]
    await wrapper.setProps({ active: false })
    await flushPromises()
    expect(media.close).toHaveBeenCalledOnce()
    expect(media.callbacks.watch()).toBe(false)
    expect(mocks.action).toHaveBeenCalledWith("session", "page", { action: "media_stop" })
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(mocks.connections).toHaveLength(2)
  })
  it("does not navigate a replacement Session after an old-page input finishes", async () => {
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    let finishInput!: (value: BrowserPage) => void
    mocks.action.mockImplementation((_sid, _pid, body) =>
      body.action === "key"
        ? new Promise<BrowserPage>((resolve) => {
            finishInput = resolve
          })
        : Promise.resolve(page),
    )
    await wrapper.get('textarea[aria-label="Browser keyboard input"]').trigger("keydown", {
      key: "Enter",
    })
    await flushPromises()
    await wrapper.get('input[aria-label="Browser address"]').setValue("https://old-page.test/")
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    await wrapper.setProps({ sessionId: "next-session", pageId: "next-page" })
    await flushPromises()
    finishInput(page)
    await flushPromises()
    expect(mocks.action.mock.calls.filter(([, , body]) => body.action === "navigate")).toEqual([])
  })
  it("ignores letterbox clicks and maps visible media pixels into the fixed remote viewport", async () => {
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    const media = mocks.mediaConnections[0]
    media.callbacks.page({ ...page, width: 1920, height: 1080, dpr: 1 })
    Object.defineProperty(media.video, "videoWidth", { value: 1920, configurable: true })
    mocks.action.mockClear()
    await wrapper.get("video").trigger("click", { clientX: 320, clientY: 10 })
    await flushPromises()
    expect(mocks.action).not.toHaveBeenCalled()
    await wrapper.get("video").trigger("click", { clientX: 320, clientY: 240 })
    await flushPromises()
    expect(mocks.action).toHaveBeenCalledWith(
      "session",
      "page",
      expect.objectContaining({ action: "click", x: 960, y: 540, width: 1920, height: 1080 }),
    )
  })
})
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (originalDecode) Object.defineProperty(HTMLImageElement.prototype, "decode", originalDecode)
  else Reflect.deleteProperty(HTMLImageElement.prototype, "decode")
  for (const { key, descriptor } of originalUrls) {
    if (descriptor) Object.defineProperty(URL, key, descriptor)
    else Reflect.deleteProperty(URL, key)
  }
})

describe("Workspace browser frame recovery", () => {
  it("waits for an in-flight resize before subscribing again after a tab switch", async () => {
    let finish!: (value: BrowserPage) => void
    mocks.info.mockResolvedValue({ ...page, width: 320 })
    mocks.action.mockImplementationOnce(
      () =>
        new Promise<BrowserPage>((resolve) => {
          finish = resolve
        }),
    )
    const wrapper = create()
    await flushPromises()
    expect(mocks.connections).toHaveLength(0)
    await wrapper.setProps({ active: false })
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.connections).toHaveLength(0)
    finish(page)
    await flushPromises()
    expect(mocks.connections).toHaveLength(1)
    expect(mocks.action).toHaveBeenCalledTimes(2)
  })

  it("never marks heartbeat-only or out-of-date viewport pixels as Live", async () => {
    const wrapper = create()
    await flushPromises()
    const stream = mocks.connections[0]
    await stream.display(metadata(1), null)
    expect(wrapper.get('[data-testid="browser-performance"]').text()).not.toContain("Live")
    await stream.display(metadata(1), new Blob(["fixture"]))
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("Live")
    await stream.display(metadata(2), null)
    await flushPromises()
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("Updating")
    await stream.display(metadata(1), new Blob(["late"]))
    expect(wrapper.get('[data-testid="browser-performance"]').text()).not.toContain("Live")
  })

  it("requires an explicit reconnect to reset a failed frame source at the same dimensions", async () => {
    mocks.info.mockResolvedValue({ ...page, capture_error: "browser_capture_failed" })
    const wrapper = create()
    await flushPromises()
    expect(mocks.connections).toHaveLength(0)
    expect(wrapper.get('[role="alert"]').text()).toContain("could not recover")
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Reconnect")!
      .trigger("click")
    await flushPromises()
    expect(mocks.action).toHaveBeenCalledWith("session", "page", {
      action: "resize",
      width: 640,
      height: 480,
      dpr: 2,
    })
    expect(mocks.connections).toHaveLength(1)
  })

  it("does not reset a terminal capture failure when browser visibility changes", async () => {
    mocks.info.mockResolvedValue({ ...page, capture_error: "browser_capture_failed" })
    create()
    await flushPromises()
    document.dispatchEvent(new Event("visibilitychange"))
    await flushPromises()
    expect(mocks.action).not.toHaveBeenCalled()
    expect(mocks.connections).toHaveLength(0)
  })

  it("clears previous page geometry and ignores late frames when changing tabs", async () => {
    const wrapper = create()
    await flushPromises()
    const previous = mocks.connections[0]
    await previous.display(metadata(3), new Blob(["fixture"]))
    mocks.info.mockResolvedValue({ ...page, id: "next", view_version: 0 })
    await wrapper.setProps({ pageId: "next" })
    await flushPromises()
    expect(previous.close).toHaveBeenCalled()
    expect(wrapper.find("img").exists()).toBe(false)
    await previous.display(metadata(4), new Blob(["late"]))
    expect(wrapper.find("img").exists()).toBe(false)
    await mocks.connections[1].display(metadata(0), new Blob(["current"]))
    expect(wrapper.get("img").attributes("data-view-version")).toBe("0")
  })
})
