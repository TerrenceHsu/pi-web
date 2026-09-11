import { expect, test } from "@playwright/test";
import { installBrowserMediaDiagnostics } from "./browser-media-diagnostics";

// Explicit real-site probe: never retain downloaded media, screenshots, videos,
// traces or page content. Only aggregate playback/audio measurements are saved.
test.use({
  channel: "chromium",
  video: "off",
  trace: "off",
  screenshot: "off",
});
if (process.env.PI_E2E_MEDIA_BROWSER_EXECUTABLE) {
  test.use({
    launchOptions: {
      executablePath: process.env.PI_E2E_MEDIA_BROWSER_EXECUTABLE,
    },
  });
}

const headers = { "X-PI-Agent-UI": "1" };
const userVideoUrl = "https://www.youtube.com/watch?v=CcMPu4Uj50g";

test("explicit YouTube probe receives audio after the Web sound gesture", async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.PI_E2E_YOUTUBE_AUDIO !== "1" ||
      process.env.PI_E2E_BROWSER !== "1",
    "Requires explicit approval for the user-provided real YouTube URL",
  );
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 1600, height: 1100 });
  const diagnostics = await installBrowserMediaDiagnostics(page);
  const created = await page.request.post("/api/sessions", {
    data: { title: "youtube-receiver-audio-probe" },
  });
  expect(created.ok()).toBe(true);
  const sid = (await created.json()).id as string;
  try {
    await page.goto(`/chat/${sid}`);
    await page.getByTestId("desk-new-browser").click();
    const video = page.locator("video.browser-video");
    await expect(video).toBeVisible();
    await expect
      .poll(
        () =>
          video.evaluate((element: HTMLVideoElement) => ({
            width: element.videoWidth,
            height: element.videoHeight,
            muted: element.muted,
            paused: element.paused,
          })),
        { timeout: 15_000 },
      )
      .toEqual({
        width: 1920,
        height: 1080,
        muted: true,
        paused: false,
      });
    // Attach the receiving analyser while still muted. Even after the actual
    // sound gesture, the fixed zero-gain output emits silence on the host.
    await video.evaluate((element: HTMLVideoElement) => {
      const audio = new AudioContext();
      const analyser = audio.createAnalyser();
      analyser.fftSize = 2048;
      const silentOutput = audio.createGain();
      silentOutput.gain.value = 0;
      audio.createMediaElementSource(element).connect(analyser);
      analyser.connect(silentOutput);
      silentOutput.connect(audio.destination);
      Object.assign(window, {
        youtubeReceiverProbe: { audio, analyser, silentOutput },
      });
    });
    await page.getByLabel("Browser address").fill(userVideoUrl);
    await page.getByRole("button", { name: "Go", exact: true }).click();
    await expect(page.getByTestId("desk-tab-browser")).toContainText(
      "YouTube",
      {
        timeout: 15_000,
      },
    );
    const pages = (
      await (
        await page.request.get(`/api/sessions/${sid}/browser`, { headers })
      ).json()
    ).pages as { id: string; url: string; title: string }[];
    expect(pages).toHaveLength(1);
    const sourcePage = pages[0];
    const externalBlock =
      /(?:accounts\.google|consent\.|\/sorry\/|\/signin)/i.test(
        sourcePage.url,
      ) ||
      /(?:captcha|unusual traffic|verify (?:you|your)|sign in to confirm)/i.test(
        sourcePage.title,
      );
    if (externalBlock) {
      await testInfo.attach("youtube-audio-blocked.json", {
        body: JSON.stringify({
          status: "external-blocked",
          url: sourcePage.url,
          title: sourcePage.title,
          reason: "Source requires consent, sign-in or verification",
        }),
        contentType: "application/json",
      });
      test.skip(
        true,
        "YouTube requires user consent/sign-in/verification; no bypass attempted",
      );
    }
    const actualUrl = new URL(sourcePage.url);
    expect(actualUrl.hostname).toMatch(/(^|\.)youtube\.com$/);
    expect(actualUrl.searchParams.get("v")).toBe("CcMPu4Uj50g");

    const sound = page.getByLabel("Browser sound", { exact: true });
    await expect(sound).not.toBeChecked();
    await sound.evaluate((checkbox) =>
      checkbox.addEventListener(
        "change",
        () => {
          const probe = (
            window as unknown as {
              youtubeReceiverProbe: { audio: AudioContext };
            }
          ).youtubeReceiverProbe;
          void probe.audio.resume();
        },
        { capture: true, once: true },
      ),
    );
    await sound.check();
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => ({
          muted: element.muted,
          paused: element.paused,
          audio: (
            window as unknown as {
              youtubeReceiverProbe: { audio: AudioContext };
            }
          ).youtubeReceiverProbe.audio.state,
        })),
      )
      .toEqual({ muted: false, paused: false, audio: "running" });

    const measured = await video.evaluate(async (element: HTMLVideoElement) => {
      const probe = (
        window as unknown as {
          youtubeReceiverProbe: {
            audio: AudioContext;
            analyser: AnalyserNode;
            silentOutput: GainNode;
          };
        }
      ).youtubeReceiverProbe;
      const samples = new Float32Array(probe.analyser.fftSize);
      const started = performance.now();
      const initialTime = element.currentTime;
      let maxRms = 0;
      let audibleSamples = 0;
      let frames = 0;
      let handle = 0;
      let lastFrameAt = started;
      const frame: VideoFrameRequestCallback = (now) => {
        frames++;
        lastFrameAt = now;
        handle = element.requestVideoFrameCallback(frame);
      };
      handle = element.requestVideoFrameCallback(frame);
      while (performance.now() - started < 12_000) {
        probe.analyser.getFloatTimeDomainData(samples);
        const rms = Math.sqrt(
          samples.reduce((sum, value) => sum + value * value, 0) /
            samples.length,
        );
        maxRms = Math.max(maxRms, rms);
        if (rms > 0.005) audibleSamples++;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      element.cancelVideoFrameCallback(handle);
      const elapsedMs = performance.now() - started;
      return {
        width: element.videoWidth,
        height: element.videoHeight,
        elapsedMs,
        timelineProgressSeconds: element.currentTime - initialTime,
        displayedFps: (frames * 1000) / elapsedMs,
        lastFrameStallMs: performance.now() - lastFrameAt,
        audioRmsPeak: maxRms,
        audibleSamples,
        outputGain: probe.silentOutput.gain.value,
        muted: element.muted,
        paused: element.paused,
        audioState: probe.audio.state,
      };
    });
    await testInfo.attach("youtube-receiver-audio-measurements.json", {
      body: JSON.stringify({ source: userVideoUrl, ...measured }, null, 2),
      contentType: "application/json",
    });
    console.log(`YOUTUBE_RECEIVER_MEASUREMENTS ${JSON.stringify(measured)}`);
    expect(measured.outputGain).toBe(0);
    expect(measured.width).toBe(1920);
    expect(measured.height).toBe(1080);
    expect(measured.timelineProgressSeconds).toBeGreaterThan(5);
    expect(
      measured.audioRmsPeak,
      "No decoded site audio: source playback/mute or the receiver is unresolved; this is not a passing result",
    ).toBeGreaterThan(0.005);
    expect(measured.audibleSamples).toBeGreaterThan(3);
    expect(measured.muted).toBe(false);
    expect(measured.paused).toBe(false);
  } finally {
    await testInfo.attach("youtube-receiver-clock.json", {
      body: JSON.stringify(await diagnostics.read(), null, 2),
      contentType: "application/json",
    });
    await page
      .evaluate(async () => {
        const probe = (
          window as unknown as {
            youtubeReceiverProbe?: { audio: AudioContext };
          }
        ).youtubeReceiverProbe;
        if (probe && probe.audio.state !== "closed") await probe.audio.close();
      })
      .catch(() => {});
    await page.request.delete(`/api/sessions/${sid}`, { headers });
  }
});
