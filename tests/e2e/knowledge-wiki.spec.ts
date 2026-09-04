import { expect, test, type Page, type Route } from "@playwright/test";

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test("LLM Wiki supports the source, approval, page, graph and conversation flow", async ({
  page,
}) => {
  const sessionResponse = await page.request.post("/api/sessions", {
    data: { title: "Knowledge E2E" },
  });
  expect(sessionResponse.ok(), await sessionResponse.text()).toBe(true);
  const sessionId = (await sessionResponse.json()).id as string;

  const spaceId = "space_e2e";
  const sourceId = "source_e2e";
  const parseRevisionId = "parse_revision_e2e";
  const summaryId = "summary_e2e";
  const pageId = "page_e2e";
  const pageRevisionId = "page_revision_e2e";
  const changeSetId = "change_set_e2e";
  let changeStatus = "awaiting_approval";
  let decisions = 0;

  const space = {
    id: spaceId,
    name: "Product Wiki",
    description: "Approved product knowledge",
    status: "active",
    graph_revision: 4,
    created_at_ms: 1,
    updated_at_ms: 2,
  };
  const source = {
    id: sourceId,
    space_id: spaceId,
    display_name: "guide.html",
    mime_type: "text/html",
    size_bytes: 42,
    source_sha256: "a".repeat(64),
    source_relpath: "raw/source_e2e/source.html",
    selected_parse_revision_id: parseRevisionId,
    selection_version: 1,
    selected_at_ms: 2,
    status: "parsed",
    safe_error_code: "",
    created_at_ms: 1,
    updated_at_ms: 2,
  };
  const summary = {
    id: summaryId,
    space_id: spaceId,
    source_id: sourceId,
    parse_revision_id: parseRevisionId,
    job_id: "job_summary_e2e",
    selection_version: 1,
    source_sha256: "a".repeat(64),
    parsed_markdown_sha256: "b".repeat(64),
    manifest_sha256: "c".repeat(64),
    page_count: 1,
    prompt_revision: "wiki-summary-v1",
    provider: "fake",
    model: "fake-model",
    content: {
      suggested_title: "Product Guide",
      overview: "A safe summary.",
      key_points: [{ text: "Approved fact", page_numbers: [1] }],
      topics: [],
      caveats: [],
    },
    content_sha256: "d".repeat(64),
    created_at_ms: 3,
  };
  const entryProposal = {
    id: "proposal_entry_e2e",
    space_id: spaceId,
    source_id: sourceId,
    summary_id: summaryId,
    job_id: "job_entry_e2e",
    kind: "entry",
    topic_ordinal: null,
    parent_proposal_id: null,
    title: "Product Guide",
    slug: "product-guide",
    aliases: [],
    markdown: "# Product Guide",
    content_sha256: "e".repeat(64),
    source_locator_json: "{}",
    created_at_ms: 4,
  };
  const pageItem = {
    id: "change_item_e2e",
    change_set_id: changeSetId,
    ordinal: 0,
    operation_kind: "page_create",
    target_id: pageId,
    base_version: null,
    before_sha256: "",
    payload_json: "{}",
    unified_diff: "--- /dev/null\n+++ product-guide.md\n+# Product Guide\n",
    created_at_ms: 5,
  };
  const changeSet = () => ({
    id: changeSetId,
    space_id: spaceId,
    conversation_id: null,
    source_summary_id: summaryId,
    status: changeStatus,
    base_graph_revision: 3,
    summary: "Create Product Guide",
    safe_error_code: "",
    created_at_ms: 5,
    decided_at_ms: changeStatus === "approved" ? 6 : null,
    published_at_ms: changeStatus === "approved" ? 6 : null,
  });
  const pageRecord = {
    id: pageId,
    space_id: spaceId,
    slug: "product-guide",
    title: "Product Guide",
    aliases: ["Guide"],
    status: "active",
    current_revision_id: pageRevisionId,
    version: 1,
    created_at_ms: 6,
    updated_at_ms: 6,
  };

  await page.route("**/api/wiki/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (method === "GET" && path === "/api/wiki/spaces")
      return json(route, [space]);
    if (path === `/api/wiki/spaces/${spaceId}/sources`)
      return json(route, [source]);
    if (
      method === "GET" &&
      path === `/api/wiki/sources/${sourceId}/artifacts`
    ) {
      return json(route, [
        {
          id: "artifact_e2e",
          source_id: sourceId,
          parse_revision_id: parseRevisionId,
          kind: "parsed_markdown",
          relpath: "parsed.md",
          mime_type: "text/markdown",
          size_bytes: 36,
          sha256: "f".repeat(64),
          width: null,
          height: null,
          source_locator_json: "{}",
          created_at_ms: 2,
        },
      ]);
    }
    if (
      method === "GET" &&
      path === `/api/wiki/sources/${sourceId}/parse-revisions`
    ) {
      return json(route, [
        {
          id: parseRevisionId,
          source_id: sourceId,
          job_id: "job_parse_e2e",
          selected_attempt_id: "attempt_e2e",
          contract_version: 2,
          artifact_schema: "parsed-document-v2",
          requested_mode: "pipeline",
          source_sha256: "a".repeat(64),
          parser: "builtin-html",
          parser_version: "1",
          preset: "html",
          routing_config_revision: "",
          routing_config_sha256: "",
          parsed_markdown_relpath: "parsed.md",
          parsed_markdown_sha256: "b".repeat(64),
          manifest_relpath: "manifest.json",
          manifest_sha256: "c".repeat(64),
          page_count: 1,
          created_at_ms: 2,
        },
      ]);
    }
    if (
      method === "GET" &&
      path === "/api/wiki/artifacts/artifact_e2e/content"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "text/markdown; charset=utf-8",
        body: "# Raw evidence\n<script>never execute</script>",
      });
      return;
    }
    if (
      method === "GET" &&
      path === `/api/wiki/sources/${sourceId}/summaries`
    ) {
      return json(route, [summary]);
    }
    if (
      method === "GET" &&
      path === `/api/wiki/sources/${sourceId}/page-proposals`
    ) {
      return json(route, [entryProposal]);
    }
    if (
      method === "POST" &&
      path === `/api/wiki/summaries/${summaryId}/topic-page-proposals`
    ) {
      return json(route, [], 201);
    }
    if (
      method === "POST" &&
      path === `/api/wiki/summaries/${summaryId}/change-set`
    ) {
      return json(route, { change_set: changeSet(), items: [pageItem] }, 201);
    }
    if (
      method === "GET" &&
      path === `/api/wiki/spaces/${spaceId}/change-sets`
    ) {
      return json(route, [changeSet()]);
    }
    if (
      method === "GET" &&
      path === `/api/wiki/change-sets/${changeSetId}/items`
    ) {
      return json(route, [pageItem]);
    }
    if (
      method === "POST" &&
      path === `/api/wiki/change-sets/${changeSetId}/decision`
    ) {
      decisions += 1;
      changeStatus = "approved";
      return json(route, { change_set: changeSet(), pages: [pageRecord] });
    }
    if (method === "GET" && path === `/api/wiki/spaces/${spaceId}/pages`) {
      return json(route, [pageRecord]);
    }
    if (method === "GET" && path === `/api/wiki/pages/${pageId}/revisions`) {
      return json(route, [
        {
          id: pageRevisionId,
          page_id: pageId,
          version: 1,
          title: "Product Guide",
          markdown:
            "# Product Guide\n<script>never execute</script>\nApproved body.",
          content_sha256: "1".repeat(64),
          change_set_id: changeSetId,
          author_kind: "agent",
          created_at_ms: 6,
        },
      ]);
    }
    if (method === "GET" && path === `/api/wiki/spaces/${spaceId}/search`) {
      return json(route, [
        {
          page_id: pageId,
          space_id: spaceId,
          revision_id: pageRevisionId,
          version: 1,
          slug: "product-guide",
          title: "Product Guide",
          snippet: "Approved body",
          rank: -1,
        },
      ]);
    }
    if (method === "GET" && path === `/api/wiki/spaces/${spaceId}/graph`) {
      return json(route, {
        space_id: spaceId,
        graph_revision: 4,
        nodes: [
          { id: pageId, kind: "page", label: "Product Guide" },
          { id: sourceId, kind: "source", label: "guide.html" },
        ],
        edges: [
          {
            id: "derived_e2e",
            from_node_id: pageId,
            to_node_id: sourceId,
            relation_type: "derived_from",
            system_managed: true,
          },
        ],
      });
    }
    if (
      method === "GET" &&
      path === `/api/wiki/spaces/${spaceId}/conversations`
    ) {
      return json(route, [
        {
          id: "conversation_e2e",
          space_id: spaceId,
          session_id: sessionId,
          title: "Knowledge E2E",
          status: "active",
          created_at_ms: 1,
          updated_at_ms: 2,
        },
      ]);
    }
    return json(
      route,
      { detail: `Unexpected Wiki route: ${method} ${path}` },
      500,
    );
  });

  await page.goto("/knowledge");
  await expect(page).toHaveURL(/\/knowledge$/);
  await expect(page.locator("[data-testid='wiki-workspace']")).toBeVisible();
  await expect(page.locator("[data-testid='wiki-space-row']")).toContainText(
    "Product Wiki",
  );

  await page.locator("[data-testid='wiki-source-row']").click();
  await page.locator("[data-testid='wiki-artifact-row']").click();
  const rawPreview = page.locator("[data-testid='wiki-artifact-preview']");
  await expect(rawPreview).toContainText("<script>never execute</script>");
  await expect(rawPreview.locator("script")).toHaveCount(0);

  await page.locator("[data-testid='wiki-prepare-source-change-set']").click();
  await expect(page.locator("[data-testid='wiki-changes-view']")).toBeVisible();
  await expect(
    page.locator("[data-testid='wiki-change-set-item']"),
  ).toContainText("+# Product Guide");
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("[data-testid='wiki-approve-change-set']").click();
  await expect(page.locator("[data-testid='wiki-changes-view']")).toContainText(
    "approved",
  );
  expect(decisions).toBe(1);

  await page.locator("[data-testid='wiki-tab-pages']").click();
  await page.locator("[data-testid='wiki-page-row']").click();
  const approvedPage = page.locator("[data-testid='wiki-page-markdown']");
  await expect(approvedPage).toContainText("<script>never execute</script>");
  await expect(approvedPage.locator("script")).toHaveCount(0);
  await page
    .locator("[data-testid='wiki-page-search-input']")
    .fill("approved body");
  await page.locator("[data-testid='wiki-page-search-button']").click();
  await expect(
    page.locator("[data-testid='wiki-page-search-result']"),
  ).toContainText("Approved body");

  await page.locator("[data-testid='wiki-tab-graph']").click();
  await expect(page.locator("[data-testid='wiki-graph-edge']")).toContainText(
    "derived_from",
  );
  await expect(page.locator("[data-testid='wiki-graph-edge']")).toContainText(
    "system managed",
  );

  await page.locator("[data-testid='wiki-tab-conversations']").click();
  await page.locator("[data-testid='wiki-conversation-row']").click();
  await expect(
    page.locator("[data-testid='chat-input-field']"),
  ).toHaveAttribute(
    "placeholder",
    "Ask this Wiki or propose an approved change…",
  );
  await expect(
    page.locator("[data-testid='wiki-conversations-view']"),
  ).toContainText("no Workspace attachments");

  await page.locator("[data-testid='wiki-back-to-chat']").click();
  await expect(page).toHaveURL(new RegExp(`/chat/${sessionId}$`));
});
