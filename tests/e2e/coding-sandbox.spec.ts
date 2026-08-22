import { expect, test, type Page, type Route } from "@playwright/test";

type SandboxStatus =
  | "ready"
  | "validating"
  | "validation_failed"
  | "validated"
  | "freezing"
  | "awaiting_approval"
  | "publishing"
  | "published"
  | "cancelled";

type SandboxOperation = ReturnType<typeof operationFor>;

function operationFor(sessionId: string, status: SandboxStatus) {
  const validationReady = [
    "validation_failed",
    "validated",
    "freezing",
    "awaiting_approval",
    "publishing",
    "published",
  ].includes(status);
  const failed = status === "validation_failed";
  const frozen = ["awaiting_approval", "publishing", "published"].includes(
    status,
  );
  return {
    schema_version: "pi-agent-managed-sandbox-operation/v1" as const,
    operation_id: "sandbox-browser-acceptance",
    session_id: sessionId,
    status,
    config_revision: 7,
    created_at_ms: 1_787_344_000_000,
    updated_at_ms: 1_787_344_000_100,
    workspace_revision: 1,
    baseline_archive_sha256: "a".repeat(64),
    baseline_manifest_sha256: "b".repeat(64),
    validation: validationReady
      ? {
          evidence_id: "validation-browser-acceptance",
          workspace_revision: 1,
          checks: [
            {
              check_id: "tests",
              argv: ["python3", "-m", "pytest", "-q"],
              cwd: ".",
              timeout_seconds: 120,
              status: failed ? "failed" : "passed",
              exit_code: failed ? 1 : 0,
              duration_ms: 42,
              stdout: failed ? "" : "1 passed",
              stderr: failed ? "assertion failed" : "",
              output_truncated: false,
            },
          ],
          passed: !failed,
          failure_code: failed ? "check_failed" : null,
          duration_ms: 42,
        }
      : null,
    diff: validationReady
      ? {
          entries: [
            {
              path: "src/accepted.py",
              status: "added" as const,
              before_sha256: null,
              after_sha256: "c".repeat(64),
            },
          ],
          patch:
            "--- /dev/null\n+++ b/src/accepted.py\n@@ -0,0 +1 @@\n+print('accepted')\n",
          patch_truncated: false,
        }
      : null,
    artifact_id: frozen ? "artifact-browser-acceptance" : null,
    artifact_sha256: frozen ? "d".repeat(64) : null,
    publish_transaction_id:
      status === "published" ? "publish-browser-acceptance" : null,
    changed_paths: frozen ? ["src/accepted.py"] : [],
    deleted_paths: [],
    error_code: failed ? "check_failed" : null,
    terminal: ["published", "cancelled"].includes(status),
    cancellable: [
      "ready",
      "validating",
      "validation_failed",
      "validated",
    ].includes(status),
    approval_required: status === "awaiting_approval",
  };
}

async function fulfill(
  route: Route,
  body: unknown,
  status = 200,
): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installSandboxApi(
  page: Page,
  sessionId: string,
  options: { failValidation?: boolean } = {},
): Promise<{ starts: () => number; publishes: () => number }> {
  let operation: SandboxOperation | null = null;
  let starts = 0;
  let publishes = 0;
  let sequence = 0;
  const events: unknown[] = [];

  function event(eventType: string, status: SandboxStatus): void {
    sequence += 1;
    events.push({
      schema_version: "pi-agent-managed-sandbox-event/v1",
      operation_id: "sandbox-browser-acceptance",
      session_id: sessionId,
      sequence,
      event_type: eventType,
      recorded_at_ms: 1_787_344_000_000 + sequence,
      payload: { status },
    });
  }

  await page.route("**/api/coding-sandbox/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (method === "GET" && path.endsWith(`/sessions/${sessionId}/operation`)) {
      await fulfill(route, { operation });
      return;
    }
    if (method === "POST" && path.endsWith("/operations")) {
      starts += 1;
      operation = operationFor(sessionId, "ready");
      event("sandbox_operation_ready", "ready");
      await fulfill(route, operation, 202);
      return;
    }
    if (method === "GET" && path.endsWith("/events")) {
      const after = Number(url.searchParams.get("after_sequence") ?? "0");
      const selected = events.filter(
        (candidate) => (candidate as { sequence: number }).sequence > after,
      );
      await fulfill(route, {
        events: selected,
        first_available_sequence: events.length ? 1 : null,
        last_available_sequence: sequence || null,
        has_more: false,
        gap: false,
      });
      return;
    }
    if (method === "GET" && path.endsWith("/diff")) {
      await fulfill(
        route,
        operation?.diff ?? { entries: [], patch: "", patch_truncated: false },
      );
      return;
    }
    if (method === "POST" && path.endsWith("/validate")) {
      const terminalStatus = options.failValidation
        ? "validation_failed"
        : "validated";
      operation = operationFor(sessionId, terminalStatus);
      event("sandbox_validation_finished", terminalStatus);
      await fulfill(route, operationFor(sessionId, "validating"), 202);
      return;
    }
    if (method === "POST" && path.endsWith("/prepare-publish")) {
      operation = operationFor(sessionId, "awaiting_approval");
      event("sandbox_approval_required", "awaiting_approval");
      await fulfill(route, operationFor(sessionId, "freezing"), 202);
      return;
    }
    if (method === "POST" && path.endsWith("/publish")) {
      publishes += 1;
      operation = operationFor(sessionId, "published");
      event("sandbox_publish_finished", "published");
      await fulfill(route, operationFor(sessionId, "publishing"), 202);
      return;
    }
    if (method === "POST" && path.endsWith("/cancel")) {
      operation = operationFor(sessionId, "cancelled");
      event("sandbox_operation_cancelled", "cancelled");
      await fulfill(route, operation);
      return;
    }
    if (method === "GET" && path.includes("/operations/")) {
      await fulfill(route, operation);
      return;
    }
    await fulfill(route, { detail: "unexpected sandbox test route" }, 500);
  });

  return { starts: () => starts, publishes: () => publishes };
}

async function createSession(page: Page, title: string): Promise<string> {
  const response = await page.request.post("/api/sessions", {
    data: { title },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).id;
}

async function openSandbox(page: Page, sessionId: string): Promise<void> {
  await page.goto(`/chat/${sessionId}`);
  await expect(page.locator("[data-testid='new-chat-button']")).toBeVisible();
  await page.locator("[data-testid='coding-sandbox-button']").click();
  await expect(
    page.getByRole("heading", { name: "Coding Sandbox" }),
  ).toBeVisible();
}

test("Sandbox refresh restores state without replay and publish requires approval", async ({
  page,
}) => {
  const sessionId = await createSession(page, `sandbox-publish-${Date.now()}`);
  const api = await installSandboxApi(page, sessionId);
  await openSandbox(page, sessionId);

  await page.locator("[data-testid='sandbox-start']").click();
  await expect(page.getByText("ready", { exact: true })).toBeVisible();
  await page.locator("[data-testid='sandbox-validate']").click();
  await expect(page.getByText("validated", { exact: true })).toBeVisible();
  await expect(page.getByText("Passed in 42 ms")).toBeVisible();
  await expect(
    page.getByText("src/accepted.py", { exact: true }),
  ).toBeVisible();
  expect(api.starts()).toBe(1);

  await page.reload();
  await expect(page.locator("[data-testid='new-chat-button']")).toBeVisible();
  await page.locator("[data-testid='coding-sandbox-button']").click();
  await expect(page.getByText("validated", { exact: true })).toBeVisible();
  expect(api.starts()).toBe(1);

  await page.locator("[data-testid='sandbox-prepare-publish']").click();
  await expect(
    page.getByText("awaiting approval", { exact: true }),
  ).toBeVisible();
  const publish = page.locator("[data-testid='sandbox-publish']");
  await expect(publish).toBeDisabled();
  expect(api.publishes()).toBe(0);
  await page.locator(".approval-panel input[type='checkbox']").check();
  await expect(publish).toBeEnabled();
  await publish.click();
  await expect(page.getByText("published", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/Published transaction publish-browser-acceptance/),
  ).toBeVisible();
  expect(api.publishes()).toBe(1);
});

test("failed validation blocks publish and remains cancellable", async ({
  page,
}) => {
  const sessionId = await createSession(page, `sandbox-failure-${Date.now()}`);
  await installSandboxApi(page, sessionId, { failValidation: true });
  await openSandbox(page, sessionId);

  await page.locator("[data-testid='sandbox-start']").click();
  await page.locator("[data-testid='sandbox-validate']").click();
  await expect(
    page.getByText("validation failed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("assertion failed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.locator("[data-testid='sandbox-prepare-publish']"),
  ).toBeDisabled();

  await page.locator("[data-testid='sandbox-cancel']").click();
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
  await expect(page.locator("[data-testid='sandbox-start-new']")).toBeVisible();
});
