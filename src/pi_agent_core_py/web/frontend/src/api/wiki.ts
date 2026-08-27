import type {
  WikiArtifact,
  WikiChangeSet,
  WikiChangeSetDecisionResponse,
  WikiChangeSetItem,
  WikiConversation,
  WikiConversationStatus,
  WikiGraphSnapshot,
  WikiPage,
  WikiPageProposal,
  WikiPageRevision,
  WikiPageSearchResult,
  WikiParseMode,
  WikiParseQueuedResponse,
  WikiParseRevision,
  WikiSource,
  WikiSourceSummaryDraft,
  WikiSourceUploadResponse,
  WikiSpace,
  WikiChangeSetResponse,
} from "../types/wiki"
import { requestBlob, requestJson, uploadForm } from "./client"

const WIKI_API = "/api/wiki"

function segment(value: string): string {
  return encodeURIComponent(value)
}

export function listWikiSpaces(signal?: AbortSignal): Promise<WikiSpace[]> {
  return requestJson(`${WIKI_API}/spaces`, { signal })
}

export function createWikiSpace(name: string, description = ""): Promise<WikiSpace> {
  return requestJson(`${WIKI_API}/spaces`, {
    method: "POST",
    body: { name, description },
  })
}

export function updateWikiSpace(
  spaceId: string,
  patch: { name?: string; description?: string },
): Promise<WikiSpace> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}`, {
    method: "PATCH",
    body: patch,
  })
}

export function setWikiSpaceStatus(
  spaceId: string,
  status: "active" | "archived",
): Promise<WikiSpace> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/status`, {
    method: "PATCH",
    body: { status },
  })
}

export function deleteWikiSpace(spaceId: string): Promise<WikiSpace> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}`, { method: "DELETE" })
}

export function listWikiSources(spaceId: string, signal?: AbortSignal): Promise<WikiSource[]> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/sources`, { signal })
}

export function uploadWikiSource(
  spaceId: string,
  file: File,
  parseMode: Exclude<WikiParseMode, "builtin"> = "auto",
): Promise<WikiSourceUploadResponse> {
  const form = new FormData()
  form.append("file", file, file.name)
  form.append("parse_mode", parseMode)
  return uploadForm(`${WIKI_API}/spaces/${segment(spaceId)}/sources`, form)
}

export function enqueueWikiParse(
  sourceId: string,
  parseMode: Exclude<WikiParseMode, "builtin"> = "auto",
): Promise<WikiParseQueuedResponse> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}/parse`, {
    method: "POST",
    body: { parse_mode: parseMode },
  })
}

export function deleteWikiSource(sourceId: string): Promise<WikiSource> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}`, { method: "DELETE" })
}

export function listWikiArtifacts(
  sourceId: string,
  parseRevisionId?: string | null,
  signal?: AbortSignal,
): Promise<WikiArtifact[]> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}/artifacts`, {
    query: { parse_revision_id: parseRevisionId },
    signal,
  })
}

export function listWikiParseRevisions(
  sourceId: string,
  signal?: AbortSignal,
): Promise<WikiParseRevision[]> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}/parse-revisions`, {
    signal,
  })
}

export function listWikiSourceSummaries(
  sourceId: string,
  signal?: AbortSignal,
): Promise<WikiSourceSummaryDraft[]> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}/summaries`, { signal })
}

export function createWikiSourceSummary(sourceId: string): Promise<WikiSourceSummaryDraft> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}/summaries`, { method: "POST" })
}

export function listWikiPageProposals(
  sourceId: string,
  signal?: AbortSignal,
): Promise<WikiPageProposal[]> {
  return requestJson(`${WIKI_API}/sources/${segment(sourceId)}/page-proposals`, { signal })
}

export function createWikiEntryPageProposal(summaryId: string): Promise<WikiPageProposal> {
  return requestJson(`${WIKI_API}/summaries/${segment(summaryId)}/entry-page-proposal`, {
    method: "POST",
  })
}

export function createWikiTopicPageProposals(summaryId: string): Promise<WikiPageProposal[]> {
  return requestJson(`${WIKI_API}/summaries/${segment(summaryId)}/topic-page-proposals`, {
    method: "POST",
  })
}

export function createWikiSourceChangeSet(summaryId: string): Promise<WikiChangeSetResponse> {
  return requestJson(`${WIKI_API}/summaries/${segment(summaryId)}/change-set`, {
    method: "POST",
  })
}

export function readWikiArtifact(artifactId: string) {
  return requestBlob(`${WIKI_API}/artifacts/${segment(artifactId)}/content`)
}

export function readWikiSource(sourceId: string) {
  return requestBlob(`${WIKI_API}/sources/${segment(sourceId)}/content`)
}

export function listWikiPages(spaceId: string, signal?: AbortSignal): Promise<WikiPage[]> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/pages`, { signal })
}

export function searchWikiPages(
  spaceId: string,
  query: string,
  limit = 20,
  signal?: AbortSignal,
): Promise<WikiPageSearchResult[]> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/search`, {
    query: { q: query, limit },
    signal,
  })
}

export function listWikiPageRevisions(
  pageId: string,
  signal?: AbortSignal,
): Promise<WikiPageRevision[]> {
  return requestJson(`${WIKI_API}/pages/${segment(pageId)}/revisions`, { signal })
}

export function getWikiGraph(spaceId: string, signal?: AbortSignal): Promise<WikiGraphSnapshot> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/graph`, { signal })
}

export function listWikiChangeSets(
  spaceId: string,
  signal?: AbortSignal,
): Promise<WikiChangeSet[]> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/change-sets`, { signal })
}

export function listWikiChangeSetItems(
  changeSetId: string,
  signal?: AbortSignal,
): Promise<WikiChangeSetItem[]> {
  return requestJson(`${WIKI_API}/change-sets/${segment(changeSetId)}/items`, { signal })
}

export function decideWikiChangeSet(
  changeSetId: string,
  decision: "approve" | "reject",
): Promise<WikiChangeSetDecisionResponse> {
  return requestJson(`${WIKI_API}/change-sets/${segment(changeSetId)}/decision`, {
    method: "POST",
    body: { decision },
  })
}

export function listWikiConversations(
  spaceId: string,
  signal?: AbortSignal,
): Promise<WikiConversation[]> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/conversations`, { signal })
}

export function createWikiConversation(
  spaceId: string,
  title = "New Knowledge conversation",
): Promise<WikiConversation> {
  return requestJson(`${WIKI_API}/spaces/${segment(spaceId)}/conversations`, {
    method: "POST",
    body: { title },
  })
}

export function setWikiConversationStatus(
  conversationId: string,
  status: WikiConversationStatus,
): Promise<WikiConversation> {
  return requestJson(`${WIKI_API}/conversations/${segment(conversationId)}/status`, {
    method: "PATCH",
    body: { status },
  })
}
