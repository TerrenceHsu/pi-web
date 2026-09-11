import { expect, test, type Locator, type Page } from "@playwright/test";
import { installBrowserMediaDiagnostics } from "./browser-media-diagnostics";

// The product and this receiver use full Chromium. Headless shell has a known
// mixed VP8/Opus playback failure and is not representative of the Web client.
test.use({ channel: "chromium" });
if (process.env.PI_E2E_MEDIA_BROWSER_EXECUTABLE) {
  test.use({
    launchOptions: {
      executablePath: process.env.PI_E2E_MEDIA_BROWSER_EXECUTABLE,
    },
  });
}

const headers = { "X-PI-Agent-UI": "1" };
const fixtureUrl = "https://browser-fixture.example.test/persistent";

function observeMedia(page: Page) {
  const opened: string[] = [];
  const live = new Set<string>();
  let maxLive = 0;
  page.on("websocket", (socket) => {
    if (!socket.url().includes("/ws/browser-media/")) return;
    const id = `${opened.length}:${socket.url()}`;
    opened.push(socket.url());
    live.add(id);
    maxLive = Math.max(maxLive, live.size);
    socket.on("close", () => live.delete(id));
  });
  return { opened, live, maxLive: () => maxLive };
}

async function newSession(page: Page, title: string) {
  const response = await page.request.post("/api/sessions", {
    data: { title },
  });
  expect(response.ok()).toBe(true);
  return (await response.json()).id as string;
}

function browserPane(page: Page, sid: string, pid: string) {
  return page.locator(`[id="desk-view-${sid}-browser:${pid}"]`);
}

function browserTab(page: Page, sid: string, pid: string) {
  return page.locator(`[id="desk-tab-${sid}-browser:${pid}"]`);
}

async function newBrowser(page: Page, sid: string) {
  // A click resolves before the async UI handler finishes creating the page.
  // Bind to this exact POST rather than racing an immediate list request.
  const creation = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/sessions/${sid}/browser/pages`,
  );
  await page.getByTestId("desk-new-browser").click();
  const response = await creation;
  expect(response.ok()).toBe(true);
  const pid = (await response.json()).id as string;
  const pane = browserPane(page, sid, pid);
  await pane.getByLabel("Browser address").fill(fixtureUrl);
  await pane.getByRole("button", { name: "Go", exact: true }).click();
  await expect(browserTab(page, sid, pid)).toContainText(
    "Persistent fixture static",
    {
      timeout: 20_000,
    },
  );
  return { pid, pane };
}

async function expectMutedMedia(pane: Locator) {
  const video = pane.locator("video.browser-video");
  await expect(pane.getByLabel("Browser rendering mode")).toHaveValue("media");
  await expect(video).toBeVisible();
  await expect
    .poll(
      () =>
        video.evaluate((element: HTMLVideoElement) => ({
          width: element.videoWidth,
          height: element.videoHeight,
          paused: element.paused,
          muted: element.muted,
          source: element.hasAttribute("src"),
        })),
      { timeout: 15_000 },
    )
    .toEqual({
      width: 1920,
      height: 1080,
      paused: false,
      muted: true,
      source: true,
    });
  await expect(
    pane.getByLabel("Browser sound", { exact: true }),
  ).not.toBeChecked();
  return video;
}

async function expectScreenshots(pane: Locator, explicit = false) {
  if (explicit)
    await expect(pane.getByLabel("Browser rendering mode")).toHaveValue(
      "screenshots",
    );
  await expect(pane.getByAltText("Interactive browser page")).toBeVisible({
    timeout: 15_000,
  });
  await expect(pane.locator("video.browser-video")).not.toBeVisible();
  await expect
    .poll(() =>
      pane
        .locator("video.browser-video")
        .evaluate(
          (video: HTMLVideoElement) =>
            video.paused && !video.hasAttribute("src"),
        ),
    )
    .toBe(true);
}

async function clickMotion(video: Locator) {
  const rect = (await video.boundingBox())!;
  const scale = Math.min(rect.width / 1920, rect.height / 1080);
  await video.click({
    position: {
      x: (rect.width - 1920 * scale) / 2 + 160 * scale,
      y: (rect.height - 1080 * scale) / 2 + 150 * scale,
    },
  });
}

test("default muted 1080p survives a genuinely static page then resumes 30 fps motion", async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.PI_E2E_BROWSER !== "1",
    "Explicit installed Chromium opt-in",
  );
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1600, height: 1100 });
  const media = observeMedia(page);
  const diagnostics = await installBrowserMediaDiagnostics(page);
  const sid = await newSession(page, "persistent-static-motion");
  try {
    await page.goto(`/chat/${sid}`);
    const { pid, pane } = await newBrowser(page, sid);
    const video = await expectMutedMedia(pane);
    const initialSource = await video.getAttribute("src");
    const streams = media.opened.length;
    // Thirty seconds without animation, timers, audio, input or page refresh.
    // Zero presented FPS on a static surface is not itself a capture failure.
    const staticResult = await video.evaluate(
      async (element: HTMLVideoElement) => {
        const started = performance.now();
        const source = element.getAttribute("src");
        let maxBufferedSeconds = 0;
        let uninterrupted = true;
        while (performance.now() - started < 30_000) {
          uninterrupted &&=
            element.getAttribute("src") === source &&
            !element.paused &&
            element.videoWidth === 1920 &&
            element.videoHeight === 1080 &&
            element.muted;
          if (element.buffered.length)
            maxBufferedSeconds = Math.max(
              maxBufferedSeconds,
              element.buffered.end(element.buffered.length - 1) -
                element.buffered.start(0),
            );
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
        return {
          uninterrupted,
          maxBufferedSeconds,
          measuredMs: performance.now() - started,
        };
      },
    );
    await testInfo.attach("persistent-static-clock.json", {
      body: JSON.stringify(
        { staticResult, ...(await diagnostics.read()) },
        null,
        2,
      ),
      contentType: "application/json",
    });
    expect(staticResult.uninterrupted).toBe(true);
    expect(staticResult.measuredMs).toBeGreaterThanOrEqual(30_000);
    expect(staticResult.maxBufferedSeconds).toBeLessThanOrEqual(8.5);
    expect(media.opened).toHaveLength(streams);
    await expect(pane.getByRole("alert")).toHaveCount(0);
    await clickMotion(video);
    await expect(browserTab(page, sid, pid)).toContainText(
      "Persistent fixture moving",
    );
    const motion = await video.evaluate(async (element: HTMLVideoElement) => {
      const started = performance.now();
      const frames: number[] = [];
      let handle = 0;
      let maxBufferedSeconds = 0;
      const frame: VideoFrameRequestCallback = (now) => {
        frames.push(now);
        handle = element.requestVideoFrameCallback(frame);
      };
      handle = element.requestVideoFrameCallback(frame);
      while (performance.now() - started < 10_000) {
        if (element.buffered.length)
          maxBufferedSeconds = Math.max(
            maxBufferedSeconds,
            element.buffered.end(element.buffered.length - 1) -
              element.buffered.start(0),
          );
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      element.cancelVideoFrameCallback(handle);
      const ended = performance.now();
      return {
        measuredMs: ended - started,
        displayedFps: (frames.length * 1000) / (ended - started),
        tailFps: frames.filter((time) => ended - time < 5000).length / 5,
        lastFrameStallMs: ended - (frames.at(-1) ?? started),
        maxBufferedSeconds,
        width: element.videoWidth,
        height: element.videoHeight,
        muted: element.muted,
      };
    });
    await testInfo.attach("persistent-static-motion.json", {
      body: JSON.stringify(
        { staticResult, motion, streams: media.opened.length },
        null,
        2,
      ),
      contentType: "application/json",
    });
    console.log(
      `BROWSER_PERSISTENT_MEASUREMENTS ${JSON.stringify({ staticResult, motion })}`,
    );
    expect(motion.displayedFps).toBeGreaterThanOrEqual(20);
    expect(motion.tailFps).toBeGreaterThanOrEqual(20);
    expect(motion.lastFrameStallMs).toBeLessThan(1000);
    expect(motion.maxBufferedSeconds).toBeLessThanOrEqual(8.5);
    expect({
      width: motion.width,
      height: motion.height,
      muted: motion.muted,
    }).toEqual({ width: 1920, height: 1080, muted: true });
    expect(await video.getAttribute("src")).toBe(initialSource);
    expect(media.opened).toHaveLength(streams);
    expect(media.maxLive()).toBe(1);
  } finally {
    await testInfo.attach("persistent-final-clock.json", {
      body: JSON.stringify(await diagnostics.read(), null, 2),
      contentType: "application/json",
    });
    await page.request.delete(`/api/sessions/${sid}`, { headers });
  }
});

test("tab and Session returns restore muted media but preserve an explicit screenshot choice", async ({
  page,
}) => {
  test.skip(
    process.env.PI_E2E_BROWSER !== "1",
    "Explicit installed Chromium opt-in",
  );
  test.setTimeout(110_000);
  await page.setViewportSize({ width: 1600, height: 1100 });
  const media = observeMedia(page);
  const firstTitle = "persistent-session-one";
  const secondTitle = "persistent-session-two";
  const sid = await newSession(page, firstTitle);
  const otherSid = await newSession(page, secondTitle);
  try {
    await page.goto(`/chat/${sid}`);
    const first = await newBrowser(page, sid);
    const firstVideo = await expectMutedMedia(first.pane);
    const firstSource = await firstVideo.getAttribute("src");
    const second = await newBrowser(page, sid);
    await expectMutedMedia(second.pane);
    await expect(firstVideo).not.toBeVisible();
    await expect.poll(() => firstVideo.getAttribute("src")).toBeNull();
    await browserTab(page, sid, first.pid).click();
    await expectMutedMedia(first.pane);
    expect(await firstVideo.getAttribute("src")).not.toBe(firstSource);

    await page
      .getByTestId("session-item")
      .filter({ hasText: secondTitle })
      .click();
    await expect(page).toHaveURL(new RegExp(`/chat/${otherSid}$`));
    const other = await newBrowser(page, otherSid);
    await expectMutedMedia(other.pane);
    await page
      .getByTestId("session-item")
      .filter({ hasText: firstTitle })
      .click();
    await expect(page).toHaveURL(new RegExp(`/chat/${sid}$`));
    await expectMutedMedia(first.pane);

    await first.pane
      .getByLabel("Browser rendering mode")
      .selectOption("screenshots");
    await expectScreenshots(first.pane, true);
    await browserTab(page, sid, second.pid).click();
    await expectMutedMedia(second.pane);
    await browserTab(page, sid, first.pid).click();
    await expectScreenshots(first.pane, true);
    await page
      .getByTestId("session-item")
      .filter({ hasText: secondTitle })
      .click();
    await expectMutedMedia(other.pane);
    await page
      .getByTestId("session-item")
      .filter({ hasText: firstTitle })
      .click();
    await expectScreenshots(first.pane, true);
    await expect(first.pane.getByLabel("HD browser rendering")).toBeChecked();
    expect(media.maxLive()).toBe(1);
    await expect(first.pane.getByRole("alert")).toHaveCount(0);
  } finally {
    await page.request.delete(`/api/sessions/${sid}`, { headers });
    await page.request.delete(`/api/sessions/${otherSid}`, { headers });
  }
});

test("split browsers hand off one media stream and Markdown focus does not restart the sole browser", async ({
  page,
}) => {
  test.skip(
    process.env.PI_E2E_BROWSER !== "1",
    "Explicit installed Chromium opt-in",
  );
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1800, height: 1200 });
  const media = observeMedia(page);
  const sid = await newSession(page, "persistent-split-ownership");
  try {
    await page.goto(`/chat/${sid}`);
    const first = await newBrowser(page, sid);
    await expectMutedMedia(first.pane);
    const second = await newBrowser(page, sid);
    await expectMutedMedia(second.pane);
    await page.getByLabel("Preview layout").selectOption("columns");
    await expectMutedMedia(second.pane);
    await expectScreenshots(first.pane);
    await expect(first.pane.getByLabel("HD browser rendering")).toBeChecked();
    await first.pane.getByLabel("Browser address").click();
    await expectMutedMedia(first.pane);
    await expectScreenshots(second.pane);
    await second.pane.getByLabel("Browser address").click();
    const secondVideo = await expectMutedMedia(second.pane);
    await expectScreenshots(first.pane);
    expect(media.maxLive()).toBe(1);

    // Replacing the screenshot pane with Markdown leaves the already preferred
    // visible browser intact, even when users move focus between pane contents.
    await first.pane.getByLabel("Browser address").click();
    await expectMutedMedia(first.pane);
    await page
      .getByRole("button", { name: "New Markdown", exact: true })
      .click();
    await page.getByLabel("Markdown logical path").fill("notes/persistent.md");
    await page
      .getByLabel("Initial Markdown content")
      .fill("# Persistent browser notes");
    await page
      .locator("form.create-markdown")
      .getByRole("button", { name: "Create", exact: true })
      .click();
    await expect(page.locator(".markdown-preview:visible")).toContainText(
      "Persistent browser notes",
    );
    await expectMutedMedia(second.pane);
    const source = await secondVideo.getAttribute("src");
    const count = media.opened.length;
    await page.locator(".markdown-preview:visible").click();
    await second.pane.getByLabel("Browser address").click();
    await page.locator(".markdown-preview:visible").click();
    await expectMutedMedia(second.pane);
    expect(await secondVideo.getAttribute("src")).toBe(source);
    expect(media.opened).toHaveLength(count);
    expect(media.maxLive()).toBe(1);
    await expect(second.pane.getByRole("alert")).toHaveCount(0);
  } finally {
    await page.request.delete(`/api/sessions/${sid}`, { headers });
  }
});
