export type WikiSpaceStatus = "active" | "archived" | "deleting" | "failed"
export type WikiSourceStatus = "uploaded" | "parsing" | "parsed" | "failed" | "deleting"
export type WikiSourceMimeType = "application/pdf" | "text/html"
export type WikiParseMode = "builtin" | "auto" | "fast" | "accurate"
export type WikiArtifactKind =
  "parsed_markdown" | "page_markdown" | "embedded_image" | "table_image" | "manifest"
export type WikiChangeSetStatus =
  "draft" | "awaiting_approval" | "approved" | "rejected" | "stale" | "failed"
export type WikiChangeSetOperationKind =
  "page_create" | "page_update" | "page_delete" | "edge_add" | "edge_delete"
export type WikiConversationStatus = "active" | "archived"
export type WikiGraphRelationType =
  "related_to" | "references" | "extends" | "contradicts" | "part_of" | "derived_from"

export interface WikiSpace {
  id: string
  name: string
  description: string
  status: WikiSpaceStatus
  graph_revision: number
  created_at_ms: number
  updated_at_ms: number
}

export interface WikiSource {
  id: string
  space_id: string
  display_name: string
  mime_type: WikiSourceMimeType
  size_bytes: number
  source_sha256: string
  source_relpath: string
  selected_parse_revision_id: string | null
  selection_version: number
  selected_at_ms: number | null
  status: WikiSourceStatus
  safe_error_code: string
  created_at_ms: number
  updated_at_ms: number
}

export interface WikiArtifact {
  id: string
  source_id: string
  parse_revision_id: string
  kind: WikiArtifactKind
  relpath: string
  mime_type: string
  size_bytes: number
  sha256: string
  width: number | null
  height: number | null
  source_locator_json: string
  created_at_ms: number
}

export interface WikiParseRevision {
  id: string
  source_id: string
  job_id: string
  selected_attempt_id: string
  contract_version: number
  artifact_schema: string
  requested_mode: WikiParseMode
  source_sha256: string
  parser: string
  parser_version: string
  preset: string
  routing_config_revision: string
  routing_config_sha256: string
  parsed_markdown_relpath: string
  parsed_markdown_sha256: string
  manifest_relpath: string
  manifest_sha256: string
  page_count: number
  created_at_ms: number
}

export interface WikiSummaryKeyPoint {
  text: string
  page_numbers: number[]
}

export interface WikiSummaryTopic {
  title: string
  summary: string
  page_numbers: number[]
}

export interface WikiSourceSummaryDraft {
  id: string
  space_id: string
  source_id: string
  parse_revision_id: string
  job_id: string
  selection_version: number
  source_sha256: string
  parsed_markdown_sha256: string
  manifest_sha256: string
  page_count: number
  prompt_revision: string
  provider: string
  model: string
  content: {
    suggested_title: string
    overview: string
    key_points: WikiSummaryKeyPoint[]
    topics: WikiSummaryTopic[]
    caveats: string[]
  }
  content_sha256: string
  created_at_ms: number
}

export interface WikiPageProposal {
  id: string
  space_id: string
  source_id: string
  summary_id: string
  job_id: string
  kind: "entry" | "topic"
  topic_ordinal: number | null
  parent_proposal_id: string | null
  title: string
  slug: string
  aliases: string[]
  markdown: string
  content_sha256: string
  source_locator_json: string
  created_at_ms: number
}

export interface WikiPage {
  id: string
  space_id: string
  slug: string
  title: string
  aliases: string[]
  status: "active" | "deleted"
  current_revision_id: string | null
  version: number
  created_at_ms: number
  updated_at_ms: number
}

export interface WikiPageRevision {
  id: string
  page_id: string
  version: number
  title: string
  markdown: string
  content_sha256: string
  change_set_id: string
  author_kind: "agent" | "user" | "system"
  created_at_ms: number
}

export interface WikiPageSearchResult {
  page_id: string
  space_id: string
  revision_id: string
  version: number
  slug: string
  title: string
  snippet: string
  rank: number
}

export interface WikiGraphNode {
  id: string
  kind: "page" | "source"
  label: string
}

export interface WikiGraphEdge {
  id: string
  from_node_id: string
  to_node_id: string
  relation_type: WikiGraphRelationType
  system_managed: boolean
}

export interface WikiGraphSnapshot {
  space_id: string
  graph_revision: number
  nodes: WikiGraphNode[]
  edges: WikiGraphEdge[]
}

export interface WikiChangeSet {
  id: string
  space_id: string
  conversation_id: string | null
  source_summary_id: string | null
  status: WikiChangeSetStatus
  base_graph_revision: number
  summary: string
  safe_error_code: string
  created_at_ms: number
  decided_at_ms: number | null
  published_at_ms: number | null
}

export interface WikiChangeSetItem {
  id: string
  change_set_id: string
  ordinal: number
  operation_kind: WikiChangeSetOperationKind
  target_id: string
  base_version: number | null
  before_sha256: string
  payload_json: string
  unified_diff: string
  created_at_ms: number
}

export interface WikiChangeSetDecisionResponse {
  change_set: WikiChangeSet
  pages: WikiPage[]
}

export interface WikiChangeSetResponse {
  change_set: WikiChangeSet
  items: WikiChangeSetItem[]
}

export interface WikiConversation {
  id: string
  space_id: string
  session_id: string
  title: string
  status: WikiConversationStatus
  created_at_ms: number
  updated_at_ms: number
}

export interface WikiSourceUploadResponse {
  source: WikiSource
  parse_queued: boolean
}

export interface WikiParseQueuedResponse {
  source_id: string
  queued: boolean
}

export interface WikiArtifactPreview {
  artifactId: string
  kind: "text" | "image" | "unsupported"
  text: string
  objectUrl: string | null
}

export type WikiWorkspaceTab = "sources" | "pages" | "graph" | "changes" | "conversations"
