import { computed, ref } from "vue"
import { defineStore } from "pinia"

import { ApiError } from "../api/client"
import * as wikiApi from "../api/wiki"
import type {
  WikiArtifact,
  WikiArtifactPreview,
  WikiChangeSet,
  WikiChangeSetItem,
  WikiConversation,
  WikiGraphSnapshot,
  WikiPage,
  WikiPageRevision,
  WikiPageSearchResult,
  WikiParseMode,
  WikiParseRevision,
  WikiSource,
  WikiSpace,
  WikiWorkspaceTab,
} from "../types/wiki"

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.detail
  if (error instanceof Error && error.message) return error.message
  return fallback
}

export const useWikiStore = defineStore("wiki", () => {
  const spaces = ref<WikiSpace[]>([])
  const selectedSpaceId = ref<string | null>(null)
  const activeTab = ref<WikiWorkspaceTab>("sources")
  const sources = ref<WikiSource[]>([])
  const selectedSourceId = ref<string | null>(null)
  const artifacts = ref<WikiArtifact[]>([])
  const parseRevisions = ref<WikiParseRevision[]>([])
  const artifactPreview = ref<WikiArtifactPreview | null>(null)
  const pages = ref<WikiPage[]>([])
  const selectedPageId = ref<string | null>(null)
  const pageRevisions = ref<WikiPageRevision[]>([])
  const pageSearchResults = ref<WikiPageSearchResult[]>([])
  const graph = ref<WikiGraphSnapshot | null>(null)
  const changeSets = ref<WikiChangeSet[]>([])
  const selectedChangeSetId = ref<string | null>(null)
  const changeSetItems = ref<WikiChangeSetItem[]>([])
  const conversations = ref<WikiConversation[]>([])
  const selectedConversationId = ref<string | null>(null)

  const loadingSpaces = ref(false)
  const loadingSources = ref(false)
  const loadingSourceDetails = ref(false)
  const loadingPreview = ref(false)
  const loadingPages = ref(false)
  const searchingPages = ref(false)
  const loadingGraph = ref(false)
  const loadingChanges = ref(false)
  const loadingConversations = ref(false)
  const mutating = ref(false)
  const sourceWorkflowStage = ref<
    "summarizing" | "entry_page" | "topic_pages" | "change_set" | null
  >(null)
  const error = ref<string | null>(null)

  let spaceLoadVersion = 0
  let sourceListVersion = 0
  let sourceDetailVersion = 0
  let spaceContentVersion = 0

  const selectedSpace = computed(
    () => spaces.value.find((space) => space.id === selectedSpaceId.value) ?? null,
  )
  const selectedSource = computed(
    () => sources.value.find((source) => source.id === selectedSourceId.value) ?? null,
  )
  const selectedPage = computed(
    () => pages.value.find((page) => page.id === selectedPageId.value) ?? null,
  )
  const selectedPageRevision = computed(() => {
    const revisionId = selectedPage.value?.current_revision_id
    return pageRevisions.value.find((revision) => revision.id === revisionId) ?? null
  })
  const selectedChangeSet = computed(
    () => changeSets.value.find((changeSet) => changeSet.id === selectedChangeSetId.value) ?? null,
  )
  const selectedConversation = computed(
    () =>
      conversations.value.find(
        (conversation) => conversation.id === selectedConversationId.value,
      ) ?? null,
  )

  function clearPreview(): void {
    if (artifactPreview.value?.objectUrl) {
      URL.revokeObjectURL(artifactPreview.value.objectUrl)
    }
    artifactPreview.value = null
  }

  function clearSourceDetails(): void {
    sourceDetailVersion += 1
    selectedSourceId.value = null
    artifacts.value = []
    parseRevisions.value = []
    clearPreview()
  }

  function clearSpaceViews(): void {
    pages.value = []
    selectedPageId.value = null
    pageRevisions.value = []
    pageSearchResults.value = []
    graph.value = null
    changeSets.value = []
    selectedChangeSetId.value = null
    changeSetItems.value = []
    conversations.value = []
    selectedConversationId.value = null
  }

  async function loadSpaces(preferredSpaceId?: string | null): Promise<void> {
    const version = ++spaceLoadVersion
    loadingSpaces.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiSpaces()
      if (version !== spaceLoadVersion) return
      spaces.value = loaded
      const preferred = preferredSpaceId ?? selectedSpaceId.value
      const next =
        loaded.find((space) => space.id === preferred && space.status === "active") ??
        loaded.find((space) => space.status === "active") ??
        null
      if (next?.id !== selectedSpaceId.value) {
        await selectSpace(next?.id ?? null)
      } else if (next) {
        await loadSources(next.id)
        if (activeTab.value !== "sources") await loadActiveTab(next.id)
      }
    } catch (cause) {
      if (version === spaceLoadVersion) {
        error.value = errorMessage(cause, "Unable to load Wiki Spaces.")
      }
    } finally {
      if (version === spaceLoadVersion) loadingSpaces.value = false
    }
  }

  async function createSpace(name: string, description = ""): Promise<WikiSpace | null> {
    const trimmedName = name.trim()
    if (!trimmedName || mutating.value) return null
    mutating.value = true
    error.value = null
    try {
      const created = await wikiApi.createWikiSpace(trimmedName, description.trim())
      spaces.value = [...spaces.value, created]
      await selectSpace(created.id)
      return created
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to create the Wiki Space.")
      return null
    } finally {
      mutating.value = false
    }
  }

  async function setSpaceStatus(
    spaceId: string,
    status: "active" | "archived",
  ): Promise<boolean> {
    if (mutating.value) return false
    mutating.value = true
    error.value = null
    try {
      const updated = await wikiApi.setWikiSpaceStatus(spaceId, status)
      spaces.value = spaces.value.map((space) => (space.id === updated.id ? updated : space))
      if (status === "active") {
        await selectSpace(updated.id)
      } else if (selectedSpaceId.value === updated.id) {
        await selectSpace(null)
      }
      return true
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to change the Wiki Space status.")
      return false
    } finally {
      mutating.value = false
    }
  }

  async function deleteSpace(spaceId: string): Promise<boolean> {
    if (mutating.value) return false
    mutating.value = true
    error.value = null
    try {
      const deleting = await wikiApi.deleteWikiSpace(spaceId)
      spaces.value = spaces.value.map((space) =>
        space.id === deleting.id ? deleting : space,
      )
      if (selectedSpaceId.value === deleting.id) await selectSpace(null)
      return true
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to delete the Wiki Space.")
      return false
    } finally {
      mutating.value = false
    }
  }

  async function selectSpace(spaceId: string | null): Promise<void> {
    if (spaceId === selectedSpaceId.value && spaceId !== null) {
      await loadSources(spaceId)
      return
    }
    selectedSpaceId.value = spaceId
    spaceContentVersion += 1
    sources.value = []
    clearSourceDetails()
    clearSpaceViews()
    error.value = null
    if (spaceId) {
      await loadSources(spaceId)
      if (activeTab.value !== "sources") await loadActiveTab(spaceId)
    }
  }

  async function loadSources(spaceId = selectedSpaceId.value): Promise<void> {
    if (!spaceId) {
      sources.value = []
      clearSourceDetails()
      return
    }
    const version = ++sourceListVersion
    loadingSources.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiSources(spaceId)
      if (version !== sourceListVersion || selectedSpaceId.value !== spaceId) return
      sources.value = loaded
      const selected = loaded.find((source) => source.id === selectedSourceId.value)
      if (!selected) clearSourceDetails()
    } catch (cause) {
      if (version === sourceListVersion) {
        error.value = errorMessage(cause, "Unable to load Wiki Sources.")
      }
    } finally {
      if (version === sourceListVersion) loadingSources.value = false
    }
  }

  async function uploadSource(
    file: File,
    parseMode: Exclude<WikiParseMode, "builtin">,
  ): Promise<WikiSource | null> {
    const spaceId = selectedSpaceId.value
    if (!spaceId || mutating.value) return null
    mutating.value = true
    error.value = null
    try {
      const response = await wikiApi.uploadWikiSource(spaceId, file, parseMode)
      sources.value = [
        ...sources.value.filter((source) => source.id !== response.source.id),
        response.source,
      ]
      await selectSource(response.source.id)
      return response.source
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to upload the Wiki Source.")
      return null
    } finally {
      mutating.value = false
    }
  }

  async function enqueueParse(
    sourceId: string,
    parseMode: Exclude<WikiParseMode, "builtin">,
  ): Promise<boolean> {
    if (mutating.value) return false
    mutating.value = true
    error.value = null
    try {
      const result = await wikiApi.enqueueWikiParse(sourceId, parseMode)
      await loadSources()
      return result.queued
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to queue Wiki parsing.")
      return false
    } finally {
      mutating.value = false
    }
  }

  async function deleteSource(sourceId: string): Promise<boolean> {
    const source = sources.value.find((candidate) => candidate.id === sourceId)
    if (!source || source.status === "deleting" || mutating.value) return false
    mutating.value = true
    error.value = null
    try {
      const deleting = await wikiApi.deleteWikiSource(sourceId)
      sources.value = sources.value.map((candidate) =>
        candidate.id === deleting.id ? deleting : candidate,
      )
      if (selectedSourceId.value === sourceId) {
        artifacts.value = []
        parseRevisions.value = []
        clearPreview()
      }
      return true
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to delete the Wiki Source.")
      return false
    } finally {
      mutating.value = false
    }
  }

  async function prepareSourceChangeSet(sourceId: string): Promise<boolean> {
    const source = sources.value.find((candidate) => candidate.id === sourceId)
    if (
      !source ||
      source.space_id !== selectedSpaceId.value ||
      source.status !== "parsed" ||
      !source.selected_parse_revision_id ||
      mutating.value
    ) {
      return false
    }
    mutating.value = true
    error.value = null
    try {
      sourceWorkflowStage.value = "summarizing"
      const summaries = await wikiApi.listWikiSourceSummaries(source.id)
      let summary = [...summaries]
        .filter(
          (candidate) =>
            candidate.parse_revision_id === source.selected_parse_revision_id &&
            candidate.selection_version === source.selection_version,
        )
        .sort((left, right) => right.created_at_ms - left.created_at_ms)[0]
      if (!summary) summary = await wikiApi.createWikiSourceSummary(source.id)

      sourceWorkflowStage.value = "entry_page"
      let proposals = await wikiApi.listWikiPageProposals(source.id)
      if (
        !proposals.some(
          (proposal) => proposal.summary_id === summary.id && proposal.kind === "entry",
        )
      ) {
        const entry = await wikiApi.createWikiEntryPageProposal(summary.id)
        proposals = [...proposals, entry]
      }

      sourceWorkflowStage.value = "topic_pages"
      await wikiApi.createWikiTopicPageProposals(summary.id)

      sourceWorkflowStage.value = "change_set"
      const result = await wikiApi.createWikiSourceChangeSet(summary.id)
      activeTab.value = "changes"
      await loadChangeSets(source.space_id)
      if (!changeSets.value.some((changeSet) => changeSet.id === result.change_set.id)) {
        changeSets.value = [...changeSets.value, result.change_set]
      }
      selectedChangeSetId.value = result.change_set.id
      changeSetItems.value = result.items
      return true
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to prepare the Source Wiki Change Set.")
      return false
    } finally {
      sourceWorkflowStage.value = null
      mutating.value = false
    }
  }

  async function selectSource(sourceId: string | null): Promise<void> {
    const version = ++sourceDetailVersion
    selectedSourceId.value = sourceId
    artifacts.value = []
    parseRevisions.value = []
    clearPreview()
    if (!sourceId) return
    if (sources.value.find((source) => source.id === sourceId)?.status === "deleting") return
    loadingSourceDetails.value = true
    error.value = null
    try {
      const [loadedArtifacts, loadedRevisions] = await Promise.all([
        wikiApi.listWikiArtifacts(sourceId),
        wikiApi.listWikiParseRevisions(sourceId),
      ])
      if (version !== sourceDetailVersion || selectedSourceId.value !== sourceId) return
      artifacts.value = loadedArtifacts
      parseRevisions.value = loadedRevisions
    } catch (cause) {
      if (version === sourceDetailVersion) {
        error.value = errorMessage(cause, "Unable to load parsed Wiki artifacts.")
      }
    } finally {
      if (version === sourceDetailVersion) loadingSourceDetails.value = false
    }
  }

  async function previewArtifact(artifact: WikiArtifact): Promise<void> {
    clearPreview()
    loadingPreview.value = true
    error.value = null
    try {
      if (
        artifact.kind !== "parsed_markdown" &&
        artifact.kind !== "page_markdown" &&
        artifact.kind !== "manifest" &&
        artifact.kind !== "embedded_image" &&
        artifact.kind !== "table_image"
      ) {
        artifactPreview.value = {
          artifactId: artifact.id,
          kind: "unsupported",
          text: "Preview is not available for this artifact type.",
          objectUrl: null,
        }
        return
      }
      const { blob } = await wikiApi.readWikiArtifact(artifact.id)
      if (artifact.mime_type.startsWith("image/")) {
        artifactPreview.value = {
          artifactId: artifact.id,
          kind: "image",
          text: "",
          objectUrl: URL.createObjectURL(blob),
        }
      } else {
        artifactPreview.value = {
          artifactId: artifact.id,
          kind: "text",
          text: await blob.text(),
          objectUrl: null,
        }
      }
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to preview the Wiki artifact.")
    } finally {
      loadingPreview.value = false
    }
  }

  async function loadPages(spaceId = selectedSpaceId.value): Promise<void> {
    if (!spaceId) return
    const version = spaceContentVersion
    loadingPages.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiPages(spaceId)
      if (version !== spaceContentVersion || selectedSpaceId.value !== spaceId) return
      pages.value = loaded
      if (selectedPageId.value && !pages.value.some((page) => page.id === selectedPageId.value)) {
        selectedPageId.value = null
        pageRevisions.value = []
      }
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to load Wiki pages.")
    } finally {
      loadingPages.value = false
    }
  }

  async function selectPage(pageId: string | null): Promise<void> {
    const version = spaceContentVersion
    selectedPageId.value = pageId
    pageRevisions.value = []
    if (!pageId) return
    loadingPages.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiPageRevisions(pageId)
      if (
        version !== spaceContentVersion ||
        selectedPageId.value !== pageId ||
        !pages.value.some((page) => page.id === pageId)
      ) {
        return
      }
      pageRevisions.value = loaded
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to load Wiki page revisions.")
    } finally {
      loadingPages.value = false
    }
  }

  async function searchPages(query: string): Promise<void> {
    const spaceId = selectedSpaceId.value
    const normalized = query.trim()
    pageSearchResults.value = []
    if (!spaceId || !normalized) return
    const version = spaceContentVersion
    searchingPages.value = true
    error.value = null
    try {
      const loaded = await wikiApi.searchWikiPages(spaceId, normalized)
      if (version !== spaceContentVersion || selectedSpaceId.value !== spaceId) return
      pageSearchResults.value = loaded
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to search Wiki pages.")
    } finally {
      searchingPages.value = false
    }
  }

  async function loadGraph(spaceId = selectedSpaceId.value): Promise<void> {
    if (!spaceId) return
    const version = spaceContentVersion
    loadingGraph.value = true
    error.value = null
    try {
      const loaded = await wikiApi.getWikiGraph(spaceId)
      if (version !== spaceContentVersion || selectedSpaceId.value !== spaceId) return
      graph.value = loaded
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to load the Wiki graph.")
    } finally {
      loadingGraph.value = false
    }
  }

  async function loadChangeSets(spaceId = selectedSpaceId.value): Promise<void> {
    if (!spaceId) return
    const version = spaceContentVersion
    loadingChanges.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiChangeSets(spaceId)
      if (version !== spaceContentVersion || selectedSpaceId.value !== spaceId) return
      changeSets.value = loaded
      if (
        selectedChangeSetId.value &&
        !changeSets.value.some((changeSet) => changeSet.id === selectedChangeSetId.value)
      ) {
        selectedChangeSetId.value = null
        changeSetItems.value = []
      }
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to load Wiki Change Sets.")
    } finally {
      loadingChanges.value = false
    }
  }

  async function selectChangeSet(changeSetId: string | null): Promise<void> {
    const version = spaceContentVersion
    selectedChangeSetId.value = changeSetId
    changeSetItems.value = []
    if (!changeSetId) return
    loadingChanges.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiChangeSetItems(changeSetId)
      if (
        version !== spaceContentVersion ||
        selectedChangeSetId.value !== changeSetId ||
        !changeSets.value.some((changeSet) => changeSet.id === changeSetId)
      ) {
        return
      }
      changeSetItems.value = loaded
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to load the Change Set diff.")
    } finally {
      loadingChanges.value = false
    }
  }

  async function decideChangeSet(
    changeSetId: string,
    decision: "approve" | "reject",
  ): Promise<boolean> {
    if (mutating.value) return false
    mutating.value = true
    error.value = null
    try {
      await wikiApi.decideWikiChangeSet(changeSetId, decision)
      await Promise.all([loadChangeSets(), loadPages(), loadGraph()])
      if (selectedChangeSetId.value === changeSetId) await selectChangeSet(changeSetId)
      return true
    } catch (cause) {
      const message = errorMessage(cause, `Unable to ${decision} the Change Set.`)
      await loadChangeSets()
      error.value = message
      return false
    } finally {
      mutating.value = false
    }
  }

  async function loadConversations(spaceId = selectedSpaceId.value): Promise<void> {
    if (!spaceId) return
    const version = spaceContentVersion
    loadingConversations.value = true
    error.value = null
    try {
      const loaded = await wikiApi.listWikiConversations(spaceId)
      if (version !== spaceContentVersion || selectedSpaceId.value !== spaceId) return
      conversations.value = loaded
      if (
        selectedConversationId.value &&
        !conversations.value.some(
          (conversation) => conversation.id === selectedConversationId.value,
        )
      ) {
        selectedConversationId.value = null
      }
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to load Knowledge conversations.")
    } finally {
      loadingConversations.value = false
    }
  }

  async function createConversation(title: string): Promise<WikiConversation | null> {
    const spaceId = selectedSpaceId.value
    const normalized = title.trim()
    if (!spaceId || !normalized || mutating.value) return null
    mutating.value = true
    error.value = null
    try {
      const created = await wikiApi.createWikiConversation(spaceId, normalized)
      conversations.value = [created, ...conversations.value]
      selectedConversationId.value = created.id
      return created
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to create the Knowledge conversation.")
      return null
    } finally {
      mutating.value = false
    }
  }

  async function setConversationStatus(
    conversationId: string,
    status: "active" | "archived",
  ): Promise<boolean> {
    if (mutating.value) return false
    mutating.value = true
    error.value = null
    try {
      const updated = await wikiApi.setWikiConversationStatus(conversationId, status)
      const index = conversations.value.findIndex(
        (conversation) => conversation.id === conversationId,
      )
      if (index >= 0) conversations.value[index] = updated
      if (updated.status !== "active" && selectedConversationId.value === updated.id) {
        selectedConversationId.value = null
      }
      return true
    } catch (cause) {
      error.value = errorMessage(cause, "Unable to update the Knowledge conversation.")
      return false
    } finally {
      mutating.value = false
    }
  }

  async function loadActiveTab(spaceId = selectedSpaceId.value): Promise<void> {
    if (!spaceId) return
    if (activeTab.value === "sources") await loadSources(spaceId)
    if (activeTab.value === "pages") await loadPages(spaceId)
    if (activeTab.value === "graph") await loadGraph(spaceId)
    if (activeTab.value === "changes") await loadChangeSets(spaceId)
    if (activeTab.value === "conversations") await loadConversations(spaceId)
  }

  function setActiveTab(tab: WikiWorkspaceTab): void {
    activeTab.value = tab
    void loadActiveTab()
  }

  function reset(): void {
    spaceLoadVersion += 1
    sourceListVersion += 1
    sourceDetailVersion += 1
    spaceContentVersion += 1
    clearPreview()
    spaces.value = []
    selectedSpaceId.value = null
    activeTab.value = "sources"
    sources.value = []
    selectedSourceId.value = null
    artifacts.value = []
    parseRevisions.value = []
    clearSpaceViews()
    loadingSpaces.value = false
    loadingSources.value = false
    loadingSourceDetails.value = false
    loadingPreview.value = false
    loadingPages.value = false
    searchingPages.value = false
    loadingGraph.value = false
    loadingChanges.value = false
    loadingConversations.value = false
    mutating.value = false
    sourceWorkflowStage.value = null
    error.value = null
  }

  return {
    spaces,
    selectedSpaceId,
    selectedSpace,
    activeTab,
    sources,
    selectedSourceId,
    selectedSource,
    artifacts,
    parseRevisions,
    artifactPreview,
    pages,
    selectedPageId,
    selectedPage,
    pageRevisions,
    selectedPageRevision,
    pageSearchResults,
    graph,
    changeSets,
    selectedChangeSetId,
    selectedChangeSet,
    changeSetItems,
    conversations,
    selectedConversationId,
    selectedConversation,
    loadingSpaces,
    loadingSources,
    loadingSourceDetails,
    loadingPreview,
    loadingPages,
    searchingPages,
    loadingGraph,
    loadingChanges,
    loadingConversations,
    mutating,
    sourceWorkflowStage,
    error,
    loadSpaces,
    createSpace,
    setSpaceStatus,
    deleteSpace,
    selectSpace,
    loadSources,
    uploadSource,
    enqueueParse,
    deleteSource,
    prepareSourceChangeSet,
    selectSource,
    previewArtifact,
    clearPreview,
    loadPages,
    selectPage,
    searchPages,
    loadGraph,
    loadChangeSets,
    selectChangeSet,
    decideChangeSet,
    loadConversations,
    createConversation,
    setConversationStatus,
    loadActiveTab,
    setActiveTab,
    reset,
  }
})
