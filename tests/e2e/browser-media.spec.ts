import { expect, test, type Page, type TestInfo } from "@playwright/test";

// Exercise the full browser media pipeline used by Web users, not headless shell.
// Full Chromium 149 and 151 pass the same unchanged 30-second AV assertions.
test.use({ channel: "chromium" });

// Optional local decoder comparison; CI keeps its installed Playwright browser.
if (process.env.PI_E2E_MEDIA_BROWSER_EXECUTABLE) {
  test.use({
    launchOptions: {
      executablePath: process.env.PI_E2E_MEDIA_BROWSER_EXECUTABLE,
    },
  });
}

const headers = { "X-PI-Agent-UI": "1" };
const fixtureUrl = "https://browser-fixture.example.test/media";

async function openSyntheticBrowser(page: Page, title: string) {
  await page.setViewportSize({ width: 1600, height: 1100 });
  const created = await page.request.post("/api/sessions", { data: { title } });
  expect(created.ok()).toBe(true);
  const sid = (await created.json()).id as string;
  await page.goto(`/chat/${sid}`);
  await page.getByTestId("desk-new-browser").click();
  await page.getByLabel("Browser address").fill(fixtureUrl);
  await page.getByRole("button", { name: "Go", exact: true }).click();
  await expect(page.getByTestId("desk-tab-browser")).toContainText(
    "Media fixture ready",
    { timeout: 20_000 },
  );
  const pages = (
    await (
      await page.request.get(`/api/sessions/${sid}/browser`, { headers })
    ).json()
  ).pages;
  expect(pages).toHaveLength(1);
  return { sid, pid: pages[0].id as string };
}

async function prepareSilentReceiver(page: Page) {
  // A zero-gain destination keeps the Web Audio graph pulling while emitting only silence.
  await page
    .locator("video.browser-video")
    .evaluate((video: HTMLVideoElement) => {
      const audio = new AudioContext();
      const source = audio.createMediaElementSource(video);
      const analyser = audio.createAnalyser();
      const silentOutput = audio.createGain();
      silentOutput.gain.value = 0;
      analyser.fftSize = 2048;
      source.connect(analyser);
      analyser.connect(silentOutput);
      silentOutput.connect(audio.destination);
      const diagnostics = {
        started: 0,
        appendCalls: 0,
        appendBytes: 0,
        updateEnds: 0,
        seekingEvents: 0,
        mediaFrames: 0,
        removeCalls: 0,
        lastRemoval: null as {
          start: number;
          end: number;
          currentTime: number;
        } | null,
        sourceBuffers: [] as SourceBuffer[],
        history: [] as unknown[],
        timer: 0,
      };
      const rangeValues = (ranges: TimeRanges) =>
        Array.from({ length: ranges.length }, (_, i) => [
          ranges.start(i),
          ranges.end(i),
        ]);
      const snapshot = (reason: string) => {
        const quality = video.getVideoPlaybackQuality();
        const result = {
          reason,
          elapsedMs: Math.round(performance.now() - diagnostics.started),
          currentTime: video.currentTime,
          readyState: video.readyState,
          paused: video.paused,
          seeking: video.seeking,
          buffered: rangeValues(video.buffered),
          totalVideoFrames: quality.totalVideoFrames,
          droppedVideoFrames: quality.droppedVideoFrames,
          callbackFrames: diagnostics.mediaFrames,
          seekingEvents: diagnostics.seekingEvents,
          audioState: audio.state,
          audioTime: audio.currentTime,
          appendCalls: diagnostics.appendCalls,
          appendBytes: diagnostics.appendBytes,
          updateEnds: diagnostics.updateEnds,
          removeCalls: diagnostics.removeCalls,
          lastRemoval: diagnostics.lastRemoval,
          sourceBuffers: diagnostics.sourceBuffers.map((buffer) => {
            try {
              return {
                updating: buffer.updating,
                ranges: rangeValues(buffer.buffered),
                timestampOffset: buffer.timestampOffset,
                mode: buffer.mode,
              };
            } catch {
              return { detached: true };
            }
          }),
          sourceAttached: video.hasAttribute("src"),
          mediaUi: document
            .querySelector('[data-testid="browser-performance"]')
            ?.textContent?.trim(),
          alert: document.querySelector('[role="alert"]')?.textContent?.trim(),
        };
        if (diagnostics.history.length < 64) diagnostics.history.push(result);
        return result;
      };
      // Auto-start may have created SourceBuffer before this analyser is attached.
      // Instrument prototype operations so both the existing stream and retries
      // are observed without replacing or altering any media data.
      const append = SourceBuffer.prototype.appendBuffer;
      SourceBuffer.prototype.appendBuffer = function (bytes) {
        if (!diagnostics.sourceBuffers.includes(this)) {
          diagnostics.sourceBuffers.push(this);
          this.addEventListener("updateend", () => {
            diagnostics.updateEnds += 1;
          });
        }
        diagnostics.appendCalls += 1;
        diagnostics.appendBytes += bytes.byteLength;
        append.call(this, bytes);
      };
      const remove = SourceBuffer.prototype.remove;
      SourceBuffer.prototype.remove = function (start: number, end: number) {
        diagnostics.removeCalls += 1;
        diagnostics.lastRemoval = {
          start,
          end,
          currentTime: video.currentTime,
        };
        remove.call(this, start, end);
      };
      video.addEventListener("seeking", () => {
        diagnostics.seekingEvents += 1;
      });
      const countFrames: VideoFrameRequestCallback = () => {
        diagnostics.mediaFrames += 1;
        video.requestVideoFrameCallback(countFrames);
      };
      video.requestVideoFrameCallback(countFrames);
      const beginDiagnostics = () => {
        diagnostics.started = performance.now();
        diagnostics.history = [];
        clearInterval(diagnostics.timer);
        snapshot("click");
        diagnostics.timer = window.setInterval(() => {
          snapshot("tick");
          if (performance.now() - diagnostics.started >= 32500)
            clearInterval(diagnostics.timer);
        }, 500);
      };
      Object.assign(window, {
        browserMediaProbe: {
          audio,
          analyser,
          silentOutput,
          diagnostics,
          snapshot,
          beginDiagnostics,
        },
      });
    });
}

async function enableMedia(page: Page) {
  const started = Date.now();
  await page.getByLabel("Browser rendering mode").selectOption("media");
  const sound = page.getByLabel("Browser sound", { exact: true });
  await expect(sound).toBeVisible();
  await sound.evaluate((checkbox) => {
    checkbox.addEventListener(
      "change",
      () => {
        const probe = (
          window as unknown as {
            browserMediaProbe: {
              audio: AudioContext;
              beginDiagnostics: () => void;
            };
          }
        ).browserMediaProbe;
        probe.beginDiagnostics();
        void probe.audio.resume();
      },
      { capture: true, once: true },
    );
  });
  await sound.check();
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { browserMediaProbe: { audio: AudioContext } })
            .browserMediaProbe.audio.state,
      ),
    )
    .toBe("running");
  const video = page.locator("video.browser-video");
  await expect(video).toBeVisible();
  await expect
    .poll(
      async () => {
        const play = page.getByRole("button", {
          name: "Play video",
          exact: true,
        });
        if (await play.isVisible()) await play.click();
        return video.evaluate((element: HTMLVideoElement) => ({
          width: element.videoWidth,
          height: element.videoHeight,
          paused: element.paused,
        }));
      },
      { timeout: 15_000 },
    )
    .toEqual({ width: 1920, height: 1080, paused: false });
  await video.evaluate(
    (element: HTMLVideoElement) =>
      new Promise<void>((resolve, reject) => {
        let frameHandle = 0;
        const timer = setTimeout(() => {
          element.cancelVideoFrameCallback(frameHandle);
          const probe = (
            window as unknown as {
              browserMediaProbe: {
                audio: AudioContext;
                silentOutput: GainNode;
                diagnostics: { history: unknown[] };
              };
            }
          ).browserMediaProbe;
          const quality = element.getVideoPlaybackQuality();
          const diagnostics = {
            currentTime: element.currentTime,
            readyState: element.readyState,
            networkState: element.networkState,
            paused: element.paused,
            ended: element.ended,
            width: element.videoWidth,
            height: element.videoHeight,
            buffered: Array.from(
              { length: element.buffered.length },
              (_, i) => [element.buffered.start(i), element.buffered.end(i)],
            ),
            decodedFrames: quality.totalVideoFrames,
            droppedFrames: quality.droppedVideoFrames,
            audioState: probe.audio.state,
            audioTime: probe.audio.currentTime,
            outputGain: probe.silentOutput.gain.value,
            mediaErrorCode: element.error?.code ?? null,
            sourceAttached: element.hasAttribute("src"),
            documentVisibility: document.visibilityState,
            mediaUi: document
              .querySelector('[data-testid="browser-performance"]')
              ?.textContent?.trim(),
            alert: document
              .querySelector('[role="alert"]')
              ?.textContent?.trim(),
          };
          reject(
            new Error(`No decoded media frame: ${JSON.stringify(diagnostics)}`),
          );
        }, 8000);
        frameHandle = element.requestVideoFrameCallback(() => {
          clearTimeout(timer);
          resolve();
        });
      }),
  );
  return { video, startupMs: Date.now() - started };
}

async function startToneThroughVideo(page: Page) {
  const video = page.locator("video.browser-video");
  const rect = (await video.boundingBox())!;
  const scale = Math.min(rect.width / 1920, rect.height / 1080);
  await video.click({
    position: {
      x: (rect.width - 1920 * scale) / 2 + 160 * scale,
      y: (rect.height - 1080 * scale) / 2 + 150 * scale,
    },
  });
  await expect(page.getByTestId("desk-tab-browser")).toContainText(
    "Media fixture playing",
    { timeout: 10_000 },
  );
}

async function attachMediaFailure(page: Page, testInfo: TestInfo) {
  const history = await page
    .evaluate(
      () =>
        (
          window as unknown as {
            browserMediaProbe?: { diagnostics: { history: unknown[] } };
          }
        ).browserMediaProbe?.diagnostics.history ?? [],
    )
    .catch(() => []);
  await testInfo.attach("browser-media-clock-failure.json", {
    body: JSON.stringify(history, null, 2),
    contentType: "application/json",
  });
}

test("1080p browser media delivers moving pixels and isolated audio, then restores screenshots", async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.PI_E2E_BROWSER !== "1",
    "Explicit installed Chromium opt-in",
  );
  test.setTimeout(90_000);
  const { sid, pid } = await openSyntheticBrowser(
    page,
    "browser-media-playback",
  );
  try {
    await expect(
      page.getByLabel("Browser sound", { exact: true }),
    ).not.toBeChecked();
    expect(
      await page
        .locator("video.browser-video")
        .evaluate((element: HTMLVideoElement) => element.muted),
    ).toBe(true);
    await prepareSilentReceiver(page);
    const { video, startupMs } = await enableMedia(page);
    await startToneThroughVideo(page);
    const measured = await video.evaluate(async (element: HTMLVideoElement) => {
      const probe = (
        window as unknown as {
          browserMediaProbe: { audio: AudioContext; analyser: AnalyserNode };
        }
      ).browserMediaProbe;
      await probe.audio.resume();
      const samples = new Float32Array(probe.analyser.fftSize);
      const times: number[] = [];
      let maxRms = 0;
      let tailAudioRms = 0;
      let maxBufferedSeconds = 0;
      let handle = 0;
      let ended = false;
      const next = (now: number) => {
        times.push(now);
        if (!ended) handle = element.requestVideoFrameCallback(next);
      };
      handle = element.requestVideoFrameCallback(next);
      const started = performance.now();
      while (performance.now() - started < 30000) {
        probe.analyser.getFloatTimeDomainData(samples);
        const rms = Math.sqrt(
          samples.reduce((sum, sample) => sum + sample * sample, 0) /
            samples.length,
        );
        maxRms = Math.max(maxRms, rms);
        if (performance.now() - started >= 20_000)
          tailAudioRms = Math.max(tailAudioRms, rms);
        if (element.buffered.length)
          maxBufferedSeconds = Math.max(
            maxBufferedSeconds,
            element.buffered.end(element.buffered.length - 1) -
              element.buffered.start(0),
          );
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      ended = true;
      element.cancelVideoFrameCallback(handle);
      const measuredMs = performance.now() - started;
      const fps = (times.length * 1000) / measuredMs;
      return {
        width: element.videoWidth,
        height: element.videoHeight,
        decodedFrames: times.length,
        displayedFps: fps,
        measuredMs,
        tailFps:
          times.filter((time) => performance.now() - time < 10000).length / 10,
        lastFrameStallMs: performance.now() - (times.at(-1) ?? started),
        maxBufferedSeconds,
        audioRms: maxRms,
        tailAudioRms,
        paused: element.paused,
        bufferedSeconds: element.buffered.length
          ? element.buffered.end(element.buffered.length - 1) -
            element.buffered.start(0)
          : 0,
      };
    });
    await testInfo.attach("browser-media-measurements.json", {
      body: JSON.stringify({ startupMs, targetFps: 30, ...measured }, null, 2),
      contentType: "application/json",
    });
    console.log(
      `BROWSER_MEDIA_MEASUREMENTS ${JSON.stringify({ startupMs, ...measured })}`,
    );
    expect(measured.width).toBe(1920);
    expect(measured.height).toBe(1080);
    expect(measured.displayedFps).toBeGreaterThanOrEqual(20);
    expect(measured.tailFps).toBeGreaterThanOrEqual(20);
    expect(measured.lastFrameStallMs).toBeLessThan(1000);
    expect(measured.audioRms).toBeGreaterThan(0.005);
    expect(measured.tailAudioRms).toBeGreaterThan(0.005);
    expect(measured.bufferedSeconds).toBeLessThanOrEqual(8.5);
    expect(measured.maxBufferedSeconds).toBeLessThanOrEqual(8.5);
    expect(measured.paused).toBe(false);
    await page.getByLabel("Browser sound", { exact: true }).uncheck();
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.muted),
    ).toBe(true);
    await page.getByLabel("Browser audio volume").focus();
    await page.getByLabel("Browser audio volume").press("Home");
    for (let step = 0; step < 7; step++)
      await page.getByLabel("Browser audio volume").press("ArrowRight");
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.volume),
    ).toBeCloseTo(0.35);
    await page.getByLabel("Browser sound", { exact: true }).check();
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.muted),
    ).toBe(false);
    await page.getByLabel("Browser rendering mode").selectOption("screenshots");
    await expect(page.getByAltText("Interactive browser page")).toBeVisible({
      timeout: 15_000,
    });
    await expect(video).not.toBeVisible();
    expect(
      await video.evaluate((element: HTMLVideoElement) =>
        element.hasAttribute("src"),
      ),
    ).toBe(false);
    // Closing a page during a second stream must release media and reject later page reads.
    await enableMedia(page);
    expect(
      (
        await page.request.delete(`/api/sessions/${sid}/browser/pages/${pid}`, {
          headers,
        })
      ).ok(),
    ).toBe(true);
    await expect
      .poll(() =>
        video.evaluate(
          (element: HTMLVideoElement) =>
            element.paused && !element.hasAttribute("src"),
        ),
      )
      .toBe(true);
    expect(
      (
        await page.request.get(`/api/sessions/${sid}/browser/pages/${pid}`, {
          headers,
        })
      ).status(),
    ).toBe(404);
  } catch (error) {
    await attachMediaFailure(page, testInfo);
    throw error;
  } finally {
    await page
      .evaluate(async () => {
        const probe = (
          window as unknown as { browserMediaProbe?: { audio: AudioContext } }
        ).browserMediaProbe;
        if (probe && probe.audio.state !== "closed") await probe.audio.close();
      })
      .catch(() => {});
    await page.request.delete(`/api/sessions/${sid}`, { headers });
  }
});

test("media requires authenticated access and stops when its Session is deleted", async ({
  page,
  browser,
}, testInfo) => {
  test.skip(
    process.env.PI_E2E_BROWSER !== "1",
    "Explicit installed Chromium opt-in",
  );
  test.setTimeout(60_000);
  const { sid, pid } = await openSyntheticBrowser(
    page,
    "browser-media-ownership",
  );
  try {
    await prepareSilentReceiver(page);
    const { video } = await enableMedia(page);
    const guest = await browser.newContext({
      storageState: { cookies: [], origins: [] },
    });
    try {
      const guestPage = await guest.newPage();
      await guestPage.goto("/");
      const opened = await guestPage.evaluate(
        ({ sid, pid }) =>
          new Promise<boolean>((resolve) => {
            const url = new URL(
              `/ws/browser-media/${sid}/${pid}`,
              window.location.href,
            );
            url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
            const socket = new WebSocket(url, "pi-browser-media-v2");
            const timer = setTimeout(() => {
              socket.close();
              resolve(false);
            }, 5000);
            socket.onopen = () => {
              clearTimeout(timer);
              socket.close();
              resolve(true);
            };
            socket.onerror = () => {
              clearTimeout(timer);
              resolve(false);
            };
            socket.onclose = () => {
              clearTimeout(timer);
              resolve(false);
            };
          }),
        { sid, pid },
      );
      expect(opened).toBe(false);
    } finally {
      await guest.close();
    }
    expect(
      (await page.request.delete(`/api/sessions/${sid}`, { headers })).ok(),
    ).toBe(true);
    await expect
      .poll(() =>
        video.evaluate(
          (element: HTMLVideoElement) =>
            element.paused && !element.hasAttribute("src"),
        ),
      )
      .toBe(true);
    expect(
      (
        await page.request.get(`/api/sessions/${sid}/browser`, { headers })
      ).status(),
    ).toBe(404);
  } catch (error) {
    await attachMediaFailure(page, testInfo);
    throw error;
  } finally {
    await page
      .evaluate(async () => {
        const probe = (
          window as unknown as { browserMediaProbe?: { audio: AudioContext } }
        ).browserMediaProbe;
        if (probe && probe.audio.state !== "closed") await probe.audio.close();
      })
      .catch(() => {});
    await page.request.delete(`/api/sessions/${sid}`, { headers });
  }
});
