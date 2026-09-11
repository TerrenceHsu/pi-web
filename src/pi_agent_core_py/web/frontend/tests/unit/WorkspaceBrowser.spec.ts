import { flushPromises, mount } from "@vue/test-utils"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import WorkspaceBrowser from "../../src/components/workspace/WorkspaceBrowser.vue"
import type { BrowserPage } from "../../src/api/browser"
import type { FrameMetadata } from "../../src/api/browserStream"
import { rememberBrowserRendering } from "../../src/api/browserMediaCoordinator"
import { ApiError } from "../../src/api/client"

const mocks = vi.hoisted(() => ({
  info: vi.fn(),
  action: vi.fn(),
  mediaSupported: vi.fn(),
  mediaHandshake: true,
  mediaConnections: [] as {
    video: HTMLVideoElement
    callbacks: {
      started?: (generation: string) => void
      page: (info: BrowserPage, generation: string) => void
      error: (code: string) => void
      closed: (code: number) => void
      watch: () => boolean
    }
    close: ReturnType<typeof vi.fn>
    notePresentedFrame: ReturnType<typeof vi.fn>
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
        mocks.mediaConnections.push({
          video,
          callbacks,
          close: this.close,
          notePresentedFrame: this.notePresentedFrame,
        })
        if (mocks.mediaHandshake) callbacks.started?.("generation-a")
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
function create(mediaPreferred = false) {
  const wrapper = mount(WorkspaceBrowser, {
    props: { sessionId: "session", pageId: "page", active: true, mediaPreferred },
  })
  wrappers.push(wrapper)
  return wrapper
}
function metadata(version: number): FrameMetadata {
  return { seq: version, page: { ...page, view_version: version }, mime: "image/png" }
}
beforeEach(() => {
  rememberBrowserRendering("session", "page", { mode: "media", failed: false })
  rememberBrowserRendering("next-session", "next-page", { mode: "media", failed: false })
  mocks.connections.length = 0
  mocks.mediaConnections.length = 0
  mocks.mediaHandshake = true
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
    await wrapper.setProps({ mediaPreferred: true })
    await flushPromises()
  }
  it("automatically streams the preferred visible pane, closes screenshots, and fixes the viewport at 1080p", async () => {
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
    expect(mocks.mediaConnections[0].video.muted).toBe(true)
    expect(wrapper.get('select[aria-label="Browser rendering mode"]').element.value).toBe("media")
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("target 30 fps")
    expect(
      wrapper.get('input[aria-label="HD browser rendering"]').attributes("disabled"),
    ).toBeDefined()
  })
  it("starts a newly mounted preferred view automatically and enables sound only on explicit gesture", async () => {
    const wrapper = create(true)
    await flushPromises()
    expect(mocks.connections).toHaveLength(0)
    expect(mocks.mediaConnections).toHaveLength(1)
    const element = mocks.mediaConnections[0].video
    expect(element.muted).toBe(true)
    vi.mocked(HTMLMediaElement.prototype.play).mockClear()
    await wrapper.get('input[aria-label="Browser sound"]').setValue(true)
    expect(element.muted).toBe(false)
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledOnce()
  })
  it("keeps navigation disabled and queues Enter submission until capture confirms v2 started", async () => {
    mocks.mediaHandshake = false
    const wrapper = create(true)
    await flushPromises()
    for (const title of ["Back", "Forward", "Reload"])
      expect(wrapper.get(`button[title="${title}"]`).attributes("disabled")).toBeDefined()
    expect(wrapper.get('button[type="submit"]').attributes("disabled")).toBeDefined()
    await wrapper.get('input[aria-label="Browser address"]').setValue("https://after-started.test/")
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    expect(mocks.action.mock.calls.filter(([, , body]) => body.action === "navigate")).toHaveLength(
      0,
    )
    mocks.mediaConnections[0].callbacks.started?.("generation-a")
    await flushPromises()
    expect(mocks.action).toHaveBeenCalledWith("session", "page", {
      action: "navigate",
      url: "https://after-started.test/",
    })
    expect(wrapper.get('button[type="submit"]').attributes("disabled")).toBeUndefined()
  })
  it("allows a queued navigation after failed startup cleanup without stopping an unknown generation", async () => {
    mocks.mediaHandshake = false
    const wrapper = create(true)
    await flushPromises()
    await wrapper.get('input[aria-label="Browser address"]').setValue("https://after-failure.test/")
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    mocks.mediaConnections[0].callbacks.error("browser_media_capture_failed")
    await flushPromises()
    expect(mocks.mediaConnections[0].close).toHaveBeenCalledOnce()
    expect(
      mocks.action.mock.calls.filter(([, , body]) => body.action === "media_stop"),
    ).toHaveLength(0)
    expect(mocks.action).toHaveBeenCalledWith("session", "page", {
      action: "navigate",
      url: "https://after-failure.test/",
    })
  })
  it("cancels a queued old-view navigation and ignores its late started receipt after hiding", async () => {
    mocks.mediaHandshake = false
    const wrapper = create(true)
    await flushPromises()
    await wrapper.get('input[aria-label="Browser address"]').setValue("https://old-view.test/")
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    const first = mocks.mediaConnections[0]
    await wrapper.setProps({ active: false })
    await flushPromises()
    first.callbacks.started?.("generation-a")
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(2)
    expect(wrapper.get('button[type="submit"]').attributes("disabled")).toBeDefined()
    expect(mocks.action.mock.calls.filter(([, , body]) => body.action === "navigate")).toHaveLength(
      0,
    )
    mocks.mediaConnections[1].callbacks.started?.("generation-b")
    await flushPromises()
    expect(wrapper.get('button[type="submit"]').attributes("disabled")).toBeUndefined()
  })
  it("bounds capture startup and waits for pending resize cleanup before a queued navigation", async () => {
    vi.useFakeTimers({
      toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout", "performance"],
    })
    let finish!: (value: BrowserPage) => void
    mocks.action.mockImplementation((_sid, _pid, body) =>
      body.action === "resize" && body.width === 1920
        ? new Promise<BrowserPage>((resolve) => {
            finish = resolve
          })
        : Promise.resolve(page),
    )
    const wrapper = create(true)
    await flushPromises()
    await wrapper.get('input[aria-label="Browser address"]').setValue("https://after-timeout.test/")
    await wrapper.get("form").trigger("submit")
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mocks.mediaConnections).toHaveLength(0)
    expect(mocks.action.mock.calls.filter(([, , body]) => body.action === "navigate")).toHaveLength(
      0,
    )
    finish(page)
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(0)
    expect(mocks.action).toHaveBeenCalledWith("session", "page", {
      action: "navigate",
      url: "https://after-timeout.test/",
    })
    expect(
      mocks.action.mock.calls.filter(([, , body]) => body.action === "media_stop"),
    ).toHaveLength(0)
  })
  it("retains the explicit screenshots choice through hide and resume", async () => {
    const wrapper = create(true)
    await flushPromises()
    await wrapper.get('select[aria-label="Browser rendering mode"]').setValue("screenshots")
    await flushPromises()
    await wrapper.setProps({ active: false })
    await flushPromises()
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(mocks.connections).toHaveLength(2)
    expect(wrapper.get('select[aria-label="Browser rendering mode"]').element.value).toBe(
      "screenshots",
    )
  })
  it("remembers an explicit screenshot choice across a Session view remount", async () => {
    const first = create(true)
    await flushPromises()
    await first.get('select[aria-label="Browser rendering mode"]').setValue("screenshots")
    await flushPromises()
    first.unmount()
    await flushPromises()
    const restored = create(true)
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(restored.get('select[aria-label="Browser rendering mode"]').element.value).toBe(
      "screenshots",
    )
  })
  it("waits for the previous local view's confirmed generation-scoped stop before starting another", async () => {
    const first = create(true)
    await flushPromises()
    let finish!: (value: BrowserPage) => void
    mocks.action.mockImplementation((_sid, _pid, body) =>
      body.action === "media_stop"
        ? new Promise<BrowserPage>((resolve) => {
            finish = resolve
          })
        : Promise.resolve(page),
    )
    const second = create(true)
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(mocks.mediaConnections[0].close).toHaveBeenCalledOnce()
    expect(mocks.action).toHaveBeenCalledWith(
      "session",
      "page",
      {
        action: "media_stop",
        media_generation: "generation-a",
      },
      expect.any(AbortSignal),
    )
    expect(first.find('input[aria-label="Browser sound"]').exists()).toBe(false)
    finish(page)
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(2)
    expect(second.find('input[aria-label="Browser sound"]').exists()).toBe(true)
    mocks.action.mockResolvedValue(page)
  })
  it.each(["browser_page_not_found", "browser_session_not_found"])(
    "releases a deleted target only for the stable typed 404 code %s",
    async (code) => {
      const first = create(true)
      await flushPromises()
      mocks.action.mockImplementation((_sid, _pid, body) =>
        body.action === "media_stop"
          ? Promise.reject(new ApiError(404, "Target no longer exists", { detail: { code } }))
          : Promise.resolve(page),
      )
      await first.setProps({ active: false })
      await flushPromises()
      create(true)
      await flushPromises()
      expect(mocks.mediaConnections).toHaveLength(2)
      expect(first.find('[role="alert"]').exists()).toBe(false)
      mocks.action.mockResolvedValue(page)
    },
  )
  it.each([
    [401, "browser_session_not_found"],
    [403, "browser_page_not_found"],
    [409, "browser_page_not_found"],
    [503, "browser_page_not_found"],
    [404, "browser_request_rejected"],
    [404, "browser_session_deleted"],
  ])("retains the lease on an unconfirmed stop %s/%s", async (status, code) => {
    const first = create(true)
    await flushPromises()
    mocks.action.mockImplementation((_sid, _pid, body) =>
      body.action === "media_stop"
        ? Promise.reject(new ApiError(status as number, "Stop rejected", { detail: { code } }))
        : Promise.resolve(page),
    )
    await first.setProps({ active: false })
    await flushPromises()
    create(true)
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(first.get('[role="alert"]').text()).toContain("Unable to confirm")
    mocks.action.mockResolvedValue(page)
  })
  it("bounds stop cleanup, aborts its request, and never releases the retained lease on a late response", async () => {
    vi.useFakeTimers({
      toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout", "performance"],
    })
    const first = create(true)
    await flushPromises()
    const attempts: { finish: (value: BrowserPage) => void; signal: AbortSignal }[] = []
    mocks.action.mockImplementation((_sid, _pid, body, signal) =>
      body.action === "media_stop"
        ? new Promise<BrowserPage>((finish) => {
            attempts.push({ finish, signal })
          })
        : Promise.resolve(page),
    )
    await first.setProps({ active: false })
    await flushPromises()
    expect(attempts).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(attempts[0].signal.aborted).toBe(true)
    expect(first.get('[role="alert"]').text()).toContain("Unable to confirm")
    attempts[0].finish(page)
    await flushPromises()
    create(true)
    await flushPromises()
    // A late response did not clear the lease: the next owner must confirm a
    // fresh scoped cleanup before its own media socket can be created.
    expect(attempts).toHaveLength(2)
    expect(mocks.mediaConnections).toHaveLength(1)
    attempts[1].finish(page)
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(2)
    mocks.action.mockResolvedValue(page)
  })
  it("falls back on quota failure without stopping a foreign stream and requires explicit retry", async () => {
    mocks.mediaHandshake = false
    const wrapper = create(true)
    await flushPromises()
    mocks.mediaConnections[0].callbacks.error("browser_media_limit")
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain("Another browser view")
    expect(
      mocks.action.mock.calls.filter(([, , body]) => body.action === "media_stop"),
    ).toHaveLength(0)
    await wrapper.setProps({ active: false })
    await flushPromises()
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(1)
    mocks.mediaHandshake = true
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Retry 1080p / 30 fps")!
      .trigger("click")
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(2)
  })
  it("keeps a static view alive while authenticated appends and its playback clock advance, without inventing FPS", async () => {
    vi.useFakeTimers({
      toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout", "performance"],
    })
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
    for (let tick = 1; tick <= 24; tick++) {
      element.currentTime = tick / 2
      media.callbacks.page({ ...page, width: 1920, height: 1080, dpr: 1 }, "generation-a")
      await vi.advanceTimersByTimeAsync(500)
    }
    expect(media.close).not.toHaveBeenCalled()
    expect(media.callbacks.watch()).toBe(true)
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("Static page")
    expect(wrapper.get('[data-testid="browser-performance"]').text()).toContain("0 fps")
    expect(wrapper.get('[data-testid="browser-performance"]').text()).not.toContain("Playing")
  })
  it("ignores a queued old video-frame callback after stop and automatic resume", async () => {
    const wrapper = create()
    await flushPromises()
    const frames: VideoFrameRequestCallback[] = []
    const element = wrapper.get("video").element as HTMLVideoElement
    element.requestVideoFrameCallback = (callback) => {
      frames.push(callback)
      return frames.length
    }
    await enable(wrapper)
    await wrapper.setProps({ active: false })
    await flushPromises()
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(frames).toHaveLength(2)
    frames[0](performance.now(), {} as VideoFrameCallbackMetadata)
    expect(mocks.mediaConnections[1].notePresentedFrame).not.toHaveBeenCalled()
    expect(frames).toHaveLength(2)
    frames[1](performance.now(), {} as VideoFrameCallbackMetadata)
    expect(mocks.mediaConnections[1].notePresentedFrame).toHaveBeenCalledOnce()
    expect(frames).toHaveLength(3)
  })
  it("ignores an already queued old health check after automatic resume", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance"] })
    const checks: (() => void)[] = []
    vi.spyOn(globalThis, "setInterval").mockImplementation((callback) => {
      checks.push(callback as () => void)
      return checks.length as unknown as ReturnType<typeof setInterval>
    })
    const wrapper = create(true)
    await flushPromises()
    await wrapper.setProps({ active: false })
    await flushPromises()
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(checks).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(8000)
    checks[0]()
    await flushPromises()
    expect(mocks.mediaConnections[1].close).not.toHaveBeenCalled()
    checks[1]()
    await flushPromises()
    expect(mocks.mediaConnections[1].close).toHaveBeenCalledOnce()
  })
  it("fails a genuinely frozen playback clock despite continuing source appends", async () => {
    vi.useFakeTimers({
      toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout", "performance"],
    })
    const wrapper = create(true)
    await flushPromises()
    const media = mocks.mediaConnections[0]
    for (let tick = 0; tick < 16; tick++) {
      media.callbacks.page({ ...page, width: 1920, height: 1080, dpr: 1 }, "generation-a")
      await vi.advanceTimersByTimeAsync(500)
    }
    await flushPromises()
    expect(media.close).toHaveBeenCalledOnce()
    expect(wrapper.get('[role="alert"]').text()).toContain("stopped progressing")
    expect(mocks.mediaConnections).toHaveLength(1)
    expect(mocks.connections).toHaveLength(1)
  })
  it("keeps screenshots when the browser cannot decode the streaming codec", async () => {
    mocks.mediaSupported.mockReturnValue(false)
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    expect(mocks.mediaConnections).toHaveLength(0)
    expect(mocks.connections).toHaveLength(2)
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
    const play = wrapper.findAll("button").find((button) => button.text() === "Play video")!
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
    expect(
      mocks.action.mock.calls.filter(([, , body]) => body.action === "media_stop"),
    ).toHaveLength(0)
    finish({ ...page, width: 1920, height: 1080, dpr: 1 })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(0)
    // No WebSocket was created, therefore there is no owned generation to stop.
    expect(
      mocks.action.mock.calls.filter(([, , body]) => body.action === "media_stop"),
    ).toHaveLength(0)
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
  it("stops on hidden tabs and resumes preferred 1080p muted when the tab becomes active", async () => {
    const wrapper = create()
    await flushPromises()
    await enable(wrapper)
    const media = mocks.mediaConnections[0]
    await wrapper.get('input[aria-label="Browser sound"]').setValue(true)
    expect(media.video.muted).toBe(false)
    await wrapper.setProps({ active: false })
    await flushPromises()
    expect(media.close).toHaveBeenCalledOnce()
    expect(media.callbacks.watch()).toBe(false)
    expect(mocks.action).toHaveBeenCalledWith(
      "session",
      "page",
      {
        action: "media_stop",
        media_generation: "generation-a",
      },
      expect.any(AbortSignal),
    )
    await wrapper.setProps({ active: true })
    await flushPromises()
    expect(mocks.mediaConnections).toHaveLength(2)
    expect(mocks.mediaConnections[1].video.muted).toBe(true)
    expect(mocks.connections).toHaveLength(1)
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
    media.callbacks.page({ ...page, width: 1920, height: 1080, dpr: 1 }, "generation-a")
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
afterEach(async () => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  await flushPromises()
  vi.useRealTimers()
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
