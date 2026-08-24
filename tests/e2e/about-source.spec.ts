import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("About exposes the AGPL Worker Corresponding Source download", async ({
  page,
}) => {
  await page.goto("/");
  const entry = page.locator("[data-testid='about-source-button']");
  await expect(entry).toBeVisible();
  await entry.click();

  const modal = page.locator("[data-testid='about-modal']");
  await expect(modal).toBeVisible();
  await expect(modal.locator("[data-testid='main-app-license']")).toContainText(
    "MIT",
  );
  const worker = modal.locator("[data-testid='worker-license']");
  await expect(worker).toContainText("AGPL-3.0-only");
  await expect(worker).toContainText("absolutely no warranty");
  await expect(worker).toContainText("runtime not ready");

  const manifestResponse = await page.request.get(
    "/api/about/wiki-parser-worker/source-offer",
  );
  expect(manifestResponse.ok(), await manifestResponse.text()).toBe(true);
  const manifest = await manifestResponse.json();
  expect(manifest.component_id).toBe("wiki-parser-worker");
  expect(manifest.file_count).toBe(11);
  expect(manifest.source_tree_sha256).toMatch(/^[a-f0-9]{64}$/);

  const downloadPromise = page.waitForEvent("download");
  await modal.locator("[data-testid='download-worker-source']").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(
    "wiki-parser-worker-0.0.28-source.tar.gz",
  );
  const downloadedPath = await download.path();
  expect(downloadedPath).not.toBeNull();
  const content = await readFile(downloadedPath!);
  expect([...content.subarray(0, 2)]).toEqual([0x1f, 0x8b]);
  expect(content.length).toBeGreaterThan(1_000);
});
