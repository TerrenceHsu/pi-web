import type { Page } from "@playwright/test";

/** Bounded numeric-only diagnostics: never retain frames, audio, URLs or page text. */
export async function installBrowserMediaDiagnostics(page: Page) {
  const started = Date.now();
  const transport: Record<string, unknown>[] = [];
  function record(event: Record<string, unknown>) {
    if (transport.length >= 512) transport.shift();
    transport.push({ elapsedMs: Date.now() - started, ...event });
  }
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/ws/browser-media/")) return;
    record({ type: "socket-created" });
    socket.on("close", () => record({ type: "socket-closed" }));
    socket.on("socketerror", () => record({ type: "socket-error" }));
    socket.on("framereceived", ({ payload }) => {
      if (typeof payload === "string") {
        try {
          const message = JSON.parse(payload);
          if (message.type === "started") record({ type: "started" });
          else if (message.type === "heartbeat") record({ type: "heartbeat" });
          else if (message.type === "error")
            record({
              type: "error",
              code: /^[a-z_]{1,100}$/.test(message.code)
                ? message.code
                : "invalid-code",
            });
        } catch {
          record({ type: "invalid-text-message" });
        }
      } else {
        // Decode only the framing header's sequence and dimensions; never keep
        // packet bytes, source URL/title or the page itself in test artifacts.
        try {
          const headerSize = payload.readUInt32BE(0);
          if (headerSize > 16384) throw new Error("Invalid header size");
          const header = JSON.parse(
            payload.subarray(4, 4 + headerSize).toString("utf8"),
          );
          record({
            type: "chunk",
            bytes: payload.length,
            seq: header.seq,
            width: header.page?.width,
            height: header.page?.height,
          });
        } catch {
          record({ type: "invalid-binary-message" });
        }
      }
    });
    socket.on("framesent", ({ payload }) => {
      if (typeof payload !== "string") return;
      try {
        const message = JSON.parse(payload);
        if (Number.isSafeInteger(message.ack))
          record({ type: "ack", seq: message.ack });
        else if (message.watch === true) record({ type: "watch" });
      } catch {
        /* No raw client messages retained. */
      }
    });
  });
  await page.addInitScript(() => {
    const started = performance.now();
    const buffers: SourceBuffer[] = [];
    const operations: unknown[] = [];
    const history: unknown[] = [];
    let appendCalls = 0;
    let appendBytes = 0;
    let removeCalls = 0;
    let updateEnds = 0;
    let lastRemoval: unknown = null;
    const videoFrames = new WeakMap<
      HTMLVideoElement,
      { count: number; lastAt: number; lastMediaTime: number }
    >();
    const ranges = (value: TimeRanges) =>
      Array.from({ length: value.length }, (_, i) => [
        value.start(i),
        value.end(i),
      ]);
    const note = (type: string, extra: Record<string, unknown> = {}) => {
      if (operations.length >= 128) operations.shift();
      operations.push({
        elapsedMs: performance.now() - started,
        type,
        ...extra,
      });
    };
    const append = SourceBuffer.prototype.appendBuffer;
    SourceBuffer.prototype.appendBuffer = function (bytes) {
      if (!buffers.includes(this)) {
        if (buffers.length < 16) buffers.push(this);
        this.addEventListener("updateend", () => updateEnds++);
        this.addEventListener("error", () => note("source-buffer-error"));
      }
      appendCalls++;
      appendBytes += bytes.byteLength;
      append.call(this, bytes);
    };
    const remove = SourceBuffer.prototype.remove;
    SourceBuffer.prototype.remove = function (start: number, end: number) {
      removeCalls++;
      lastRemoval = {
        start,
        end,
        elapsedMs: performance.now() - started,
        before: ranges(this.buffered),
      };
      note("remove", { start, end });
      remove.call(this, start, end);
    };
    const snapshot = () => {
      const videos = Array.from(
        document.querySelectorAll<HTMLVideoElement>("video.browser-video"),
      );
      return {
        elapsedMs: performance.now() - started,
        appendCalls,
        appendBytes,
        removeCalls,
        updateEnds,
        lastRemoval,
        sourceBuffers: buffers.map((buffer) => {
          try {
            return {
              updating: buffer.updating,
              buffered: ranges(buffer.buffered),
              mode: buffer.mode,
            };
          } catch {
            return { detached: true };
          }
        }),
        videos: videos.map((video) => {
          let frames = videoFrames.get(video);
          if (!frames) {
            frames = { count: 0, lastAt: 0, lastMediaTime: 0 };
            videoFrames.set(video, frames);
            const counter = frames;
            const frame: VideoFrameRequestCallback = (now, metadata) => {
              counter.count++;
              counter.lastAt = now;
              counter.lastMediaTime = metadata.mediaTime;
              if (video.isConnected) video.requestVideoFrameCallback(frame);
            };
            video.requestVideoFrameCallback(frame);
            for (const name of [
              "waiting",
              "stalled",
              "seeking",
              "seeked",
              "pause",
              "playing",
              "error",
            ])
              video.addEventListener(name, () =>
                note(name, {
                  currentTime: video.currentTime,
                  error: video.error?.code ?? null,
                }),
              );
          }
          const quality = video.getVideoPlaybackQuality();
          return {
            currentTime: video.currentTime,
            width: video.videoWidth,
            height: video.videoHeight,
            paused: video.paused,
            muted: video.muted,
            seeking: video.seeking,
            readyState: video.readyState,
            error: video.error?.code ?? null,
            sourceAttached: video.hasAttribute("src"),
            buffered: ranges(video.buffered),
            presented: frames.count,
            lastPresentedMediaTime: frames.lastMediaTime,
            lastFrameStallMs: frames.lastAt
              ? performance.now() - frames.lastAt
              : null,
            decoded: quality.totalVideoFrames,
            dropped: quality.droppedVideoFrames,
          };
        }),
        // Boolean UI state only: never capture source-site text in this helper.
        hasAppAlert: !!document.querySelector(".browser-error[role='alert']"),
        retryVisible: Array.from(
          document.querySelectorAll(".browser-media-controls button"),
        ).some((button) => button.textContent?.startsWith("Retry")),
      };
    };
    const timer = window.setInterval(() => {
      if (history.length < 90) history.push(snapshot());
      if (performance.now() - started >= 45_000) clearInterval(timer);
    }, 500);
    Object.assign(window, {
      browserMediaDiagnostics: { history, operations, snapshot },
    });
  });
  return {
    transport,
    async read() {
      const receiver = await page
        .evaluate(() => {
          const diagnostics = (
            window as unknown as {
              browserMediaDiagnostics?: {
                history: unknown[];
                operations: unknown[];
                snapshot: () => unknown;
              };
            }
          ).browserMediaDiagnostics;
          return diagnostics
            ? {
                history: diagnostics.history,
                operations: diagnostics.operations,
                final: diagnostics.snapshot(),
              }
            : null;
        })
        .catch(() => null);
      return { receiver, transport };
    },
  };
}
