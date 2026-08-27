import { flushPromises, mount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

const wikiApi = vi.hoisted(() => ({
  listWikiPages: vi.fn(),
  searchWikiPages: vi.fn(),
  listWikiPageRevisions: vi.fn(),
  getWikiGraph: vi.fn(),
  listWikiChangeSets: vi.fn(),
  listWikiChangeSetItems: vi.fn(),
  decideWikiChangeSet: vi.fn(),
  listWikiConversations: vi.fn(),
  createWikiConversation: vi.fn(),
  setWikiConversationStatus: vi.fn(),
}))

vi.mock("../../src/api/wiki", () => wikiApi)

import WikiChangesView from "../../src/components/wiki/WikiChangesView.vue"
import WikiConversationsView from "../../src/components/wiki/WikiConversationsView.vue"
import WikiGraphView from "../../src/components/wiki/WikiGraphView.vue"
import WikiPagesView from "../../src/components/wiki/WikiPagesView.vue"
import { useSessionStore } from "../../src/stores/sessionStore"
import { useWikiStore } from "../../src/stores/wikiStore"

const spaceId = "space_000000000000000000000001"
const pageId = "page_000000000000000000000001"
const revisionId = "page_revision_000000000000000000000001"
const changeSetId = "change_set_000000000000000000000001"

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  wikiApi.listWikiPages.mockResolvedValue([])
  wikiApi.getWikiGraph.mockResolvedValue({
    space_id: spaceId,
    graph_revision: 2,
    nodes: [],
    edges: [],
  })
  wikiApi.listWikiChangeSets.mockResolvedValue([])
})

describe("Wiki page, graph and approval views", () => {
  it("renders approved Markdown with raw HTML disabled", async () => {
    const store = useWikiStore()
    store.selectedSpaceId = spaceId
    store.pages = [
      {
        id: pageId,
        space_id: spaceId,
        slug: "guide",
        title: "Guide",
        aliases: [],
        status: "active",
        current_revision_id: revisionId,
        version: 1,
        created_at_ms: 1,
        updated_at_ms: 1,
      },
    ]
    wikiApi.listWikiPageRevisions.mockResolvedValue([
      {
        id: revisionId,
        page_id: pageId,
        version: 1,
        title: "Guide",
        markdown: "# Guide\n<script>alert(1)</script>",
        content_sha256: "a".repeat(64),
        change_set_id: changeSetId,
        author_kind: "agent",
        created_at_ms: 1,
      },
    ])
    const wrapper = mount(WikiPagesView)

    await wrapper.get("[data-testid='wiki-page-row']").trigger("click")
    await flushPromises()

    const rendered = wrapper.get("[data-testid='wiki-page-markdown']")
    expect(rendered.get("h1").text()).toBe("Guide")
    expect(rendered.text()).toContain("<script>alert(1)</script>")
    expect(rendered.find("script").exists()).toBe(false)
  })

  it("distinguishes system derived_from edges in the published graph", () => {
    const store = useWikiStore()
    store.graph = {
      space_id: spaceId,
      graph_revision: 3,
      nodes: [
        { id: pageId, kind: "page", label: "Guide" },
        {
          id: "source_000000000000000000000001",
          kind: "source",
          label: "guide.pdf",
        },
      ],
      edges: [
        {
          id: "derived:page:source",
          from_node_id: pageId,
          to_node_id: "source_000000000000000000000001",
          relation_type: "derived_from",
          system_managed: true,
        },
      ],
    }

    const wrapper = mount(WikiGraphView)

    expect(wrapper.get("[data-testid='wiki-graph-view'] svg").exists()).toBe(true)
    expect(wrapper.get("[data-testid='wiki-graph-edge']").text()).toContain("derived_from")
    expect(wrapper.get("[data-testid='wiki-graph-edge']").text()).toContain("system managed")
  })

  it("requires confirmation and sends one whole-Change-Set approval decision", async () => {
    const store = useWikiStore()
    store.selectedSpaceId = spaceId
    store.changeSets = [
      {
        id: changeSetId,
        space_id: spaceId,
        conversation_id: "conversation_000000000000000000000001",
        source_summary_id: null,
        status: "awaiting_approval",
        base_graph_revision: 1,
        summary: "Update Guide",
        safe_error_code: "",
        created_at_ms: 1,
        decided_at_ms: null,
        published_at_ms: null,
      },
    ]
    wikiApi.listWikiChangeSetItems.mockResolvedValue([
      {
        id: "change_item_000000000000000000000001",
        change_set_id: changeSetId,
        ordinal: 0,
        operation_kind: "page_update",
        target_id: pageId,
        base_version: 1,
        before_sha256: "a".repeat(64),
        payload_json: "{}",
        unified_diff: "--- old\n+++ new\n-Old\n+New\n",
        created_at_ms: 1,
      },
    ])
    wikiApi.decideWikiChangeSet.mockResolvedValue({
      change_set: { ...store.changeSets[0], status: "approved" },
      pages: [],
    })
    wikiApi.listWikiChangeSets.mockResolvedValue([{ ...store.changeSets[0], status: "approved" }])
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true)
    const wrapper = mount(WikiChangesView)

    await wrapper.get("[data-testid='wiki-change-set-row']").trigger("click")
    await flushPromises()
    expect(wrapper.get("[data-testid='wiki-change-set-item']").text()).toContain("+New")
    await wrapper.get("[data-testid='wiki-approve-change-set']").trigger("click")
    await flushPromises()

    expect(confirm).toHaveBeenCalledOnce()
    expect(wikiApi.decideWikiChangeSet).toHaveBeenCalledWith(changeSetId, "approve")
  })

  it("activates an independent Session and embeds chat in Knowledge mode", async () => {
    const sessionId = "session_000000000000000000000001"
    const conversationId = "conversation_000000000000000000000001"
    const store = useWikiStore()
    const sessionStore = useSessionStore()
    store.conversations = [
      {
        id: conversationId,
        space_id: spaceId,
        session_id: sessionId,
        title: "Architecture questions",
        status: "active",
        created_at_ms: 1,
        updated_at_ms: 2,
      },
    ]
    store.selectedConversationId = conversationId
    sessionStore.sessions = [
      {
        id: sessionId,
        title: "Architecture questions",
        created_at: 1,
        updated_at: 2,
        metadata: {},
        active_lane: "main",
      },
    ]
    const wrapper = mount(WikiConversationsView, {
      global: {
        stubs: {
          ChatPanel: {
            props: ["mode"],
            template: '<div data-testid="knowledge-chat-stub">{{ mode }}</div>',
          },
        },
      },
    })

    await wrapper.get("[data-testid='wiki-conversation-row']").trigger("click")

    expect(sessionStore.activeSessionId).toBe(sessionId)
    expect(wrapper.get("[data-testid='knowledge-chat-stub']").text()).toBe("knowledge")
    expect(wrapper.get("[data-testid='wiki-conversations-view']").text()).toContain(
      "no Workspace attachments",
    )
  })
})
