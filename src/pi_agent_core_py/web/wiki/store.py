"""SQLite canonical store for page-centric LLM Wiki schema v8."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import aiosqlite

from .errors import WikiMirrorError, WikiSchemaError, WikiStoreError
from .files import WikiFileStore
from .fts import compile_wiki_fts_query
from .legacy import retire_legacy_knowledge
from .models import (
    WIKI_SCHEMA_VERSION,
    WikiArtifact,
    WikiArtifactKind,
    WikiChangeSet,
    WikiChangeSetItem,
    WikiChangeSetOperationKind,
    WikiConversation,
    WikiConversationStatus,
    WikiEdge,
    WikiGraphEdge,
    WikiGraphNode,
    WikiGraphSnapshot,
    WikiJob,
    WikiJobKind,
    WikiJobStatus,
    WikiLegacyBackupReceipt,
    WikiMirrorRepairReport,
    WikiPage,
    WikiPageProposal,
    WikiPageRevision,
    WikiPageSearchResult,
    WikiParseAttempt,
    WikiParseAttemptState,
    WikiParseMode,
    WikiParseRevision,
    WikiRelationType,
    WikiSelectedParsePointer,
    WikiSource,
    WikiSourceMimeType,
    WikiSourceStatus,
    WikiSourceSummaryContent,
    WikiSourceSummaryDraft,
    WikiSpace,
    WikiSpaceStatus,
    validate_artifact_id,
    validate_change_set_id,
    validate_conversation_id,
    validate_edge_id,
    validate_job_id,
    validate_page_id,
    validate_page_proposal_id,
    validate_page_revision_id,
    validate_parse_revision_id,
    validate_source_id,
    validate_space_id,
    validate_summary_id,
)

WIKI_APPLICATION_ID = 1_464_421_193  # ASCII "WIKI"
_SCHEMA_META_KEY = "schema_version"
LegacyPolicy = Literal["preserve", "retire"]

_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS wiki_schema_meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS wiki_spaces (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        description    TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'active',
        graph_revision INTEGER NOT NULL DEFAULT 0,
        created_at_ms  INTEGER NOT NULL,
        updated_at_ms  INTEGER NOT NULL,
        CHECK (length(name) BETWEEN 1 AND 120),
        CHECK (length(description) <= 4000),
        CHECK (status IN ('active', 'archived', 'deleting', 'failed')),
        CHECK (graph_revision >= 0),
        CHECK (updated_at_ms >= created_at_ms)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_sources (
        id                       TEXT PRIMARY KEY,
        space_id                 TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        display_name             TEXT NOT NULL,
        mime_type                TEXT NOT NULL,
        size_bytes               INTEGER NOT NULL,
        source_sha256            TEXT NOT NULL,
        source_relpath           TEXT NOT NULL,
        selected_parse_revision_id TEXT,
        selection_version        INTEGER NOT NULL DEFAULT 0,
        selected_at_ms            INTEGER,
        status                   TEXT NOT NULL DEFAULT 'uploaded',
        safe_error_code          TEXT NOT NULL DEFAULT '',
        created_at_ms            INTEGER NOT NULL,
        updated_at_ms            INTEGER NOT NULL,
        CHECK (display_name <> ''),
        CHECK (mime_type IN ('application/pdf', 'text/html')),
        CHECK (size_bytes > 0),
        CHECK (length(source_sha256) = 64 AND source_sha256 = lower(source_sha256)),
        CHECK (source_relpath <> ''),
        CHECK (selection_version >= 0),
        CHECK ((selected_parse_revision_id IS NULL AND selection_version = 0) OR
               (selected_parse_revision_id IS NOT NULL AND selection_version >= 1)),
        CHECK ((selected_parse_revision_id IS NULL AND selected_at_ms IS NULL) OR
               (selected_parse_revision_id IS NOT NULL AND selected_at_ms IS NOT NULL)),
        CHECK (status <> 'parsed' OR selected_parse_revision_id IS NOT NULL),
        CHECK (status IN ('uploaded', 'parsing', 'parsed', 'failed', 'deleting')),
        CHECK (updated_at_ms >= created_at_ms),
        UNIQUE (space_id, source_sha256),
        FOREIGN KEY (id, selected_parse_revision_id)
            REFERENCES wiki_parse_revisions(source_id, id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_artifacts (
        id                  TEXT PRIMARY KEY,
        source_id           TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE CASCADE,
        parse_revision_id   TEXT NOT NULL REFERENCES wiki_parse_revisions(id)
                                      ON DELETE CASCADE,
        kind                TEXT NOT NULL,
        relpath             TEXT NOT NULL,
        mime_type           TEXT NOT NULL,
        size_bytes          INTEGER NOT NULL,
        sha256              TEXT NOT NULL,
        width               INTEGER,
        height              INTEGER,
        source_locator_json TEXT NOT NULL DEFAULT '{}',
        created_at_ms       INTEGER NOT NULL,
        CHECK (kind IN ('parsed_markdown', 'page_markdown', 'embedded_image',
                        'table_image', 'manifest')),
        CHECK (relpath <> ''),
        CHECK (mime_type <> ''),
        CHECK (size_bytes >= 0),
        CHECK (kind = 'page_markdown' OR size_bytes > 0),
        CHECK (length(sha256) = 64 AND sha256 = lower(sha256)),
        CHECK (width IS NULL OR width > 0),
        CHECK (height IS NULL OR height > 0),
        CHECK (json_valid(source_locator_json)),
        UNIQUE (parse_revision_id, relpath),
        FOREIGN KEY (source_id, parse_revision_id)
            REFERENCES wiki_parse_revisions(source_id, id) ON DELETE CASCADE
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_conversations (
        id            TEXT PRIMARY KEY,
        space_id      TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        session_id    TEXT NOT NULL UNIQUE,
        title         TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'active',
        created_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL,
        CHECK (title <> ''),
        CHECK (status IN ('active', 'archived')),
        CHECK (updated_at_ms >= created_at_ms)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_change_sets (
        id                  TEXT PRIMARY KEY,
        space_id            TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        conversation_id     TEXT REFERENCES wiki_conversations(id) ON DELETE SET NULL,
        source_summary_id   TEXT UNIQUE REFERENCES wiki_source_summaries(id)
                                      ON DELETE RESTRICT,
        status              TEXT NOT NULL DEFAULT 'draft',
        base_graph_revision INTEGER NOT NULL,
        summary             TEXT NOT NULL DEFAULT '',
        safe_error_code     TEXT NOT NULL DEFAULT '',
        created_at_ms       INTEGER NOT NULL,
        decided_at_ms       INTEGER,
        published_at_ms     INTEGER,
        CHECK (status IN ('draft', 'awaiting_approval', 'approved', 'rejected',
                          'stale', 'failed')),
        CHECK (base_graph_revision >= 0),
        CHECK (decided_at_ms IS NULL OR decided_at_ms >= created_at_ms),
        CHECK (published_at_ms IS NULL OR published_at_ms >= created_at_ms)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_pages (
        id                  TEXT PRIMARY KEY,
        space_id            TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        slug                TEXT NOT NULL,
        title               TEXT NOT NULL,
        aliases_json        TEXT NOT NULL DEFAULT '[]',
        status              TEXT NOT NULL DEFAULT 'active',
        current_revision_id TEXT REFERENCES wiki_page_revisions(id) ON DELETE SET NULL
                                   DEFERRABLE INITIALLY DEFERRED,
        version             INTEGER NOT NULL DEFAULT 0,
        created_at_ms       INTEGER NOT NULL,
        updated_at_ms       INTEGER NOT NULL,
        CHECK (slug <> ''),
        CHECK (title <> ''),
        CHECK (json_valid(aliases_json) AND json_type(aliases_json) = 'array'),
        CHECK (status IN ('active', 'deleted')),
        CHECK (version >= 0),
        CHECK (updated_at_ms >= created_at_ms),
        UNIQUE (space_id, slug)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_page_revisions (
        id             TEXT PRIMARY KEY,
        page_id        TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        version        INTEGER NOT NULL,
        title          TEXT NOT NULL,
        markdown       TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        change_set_id  TEXT NOT NULL REFERENCES wiki_change_sets(id) ON DELETE RESTRICT,
        author_kind    TEXT NOT NULL,
        created_at_ms  INTEGER NOT NULL,
        CHECK (version >= 1),
        CHECK (title <> ''),
        CHECK (markdown <> ''),
        CHECK (length(content_sha256) = 64 AND content_sha256 = lower(content_sha256)),
        CHECK (author_kind IN ('agent', 'user', 'system')),
        UNIQUE (page_id, version)
    ) STRICT;
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS wiki_pages_fts USING fts5(
        page_id UNINDEXED,
        space_id UNINDEXED,
        title,
        aliases,
        content,
        tokenize = 'unicode61 remove_diacritics 2'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_page_sources (
        page_id             TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        source_id           TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE RESTRICT,
        source_locator_json TEXT NOT NULL DEFAULT '{}',
        created_at_ms       INTEGER NOT NULL,
        CHECK (json_valid(source_locator_json)),
        PRIMARY KEY (page_id, source_id)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_edges (
        id             TEXT PRIMARY KEY,
        space_id       TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        from_page_id   TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        to_page_id     TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        relation_type  TEXT NOT NULL,
        change_set_id  TEXT NOT NULL REFERENCES wiki_change_sets(id) ON DELETE RESTRICT,
        created_at_ms  INTEGER NOT NULL,
        CHECK (from_page_id <> to_page_id),
        CHECK (relation_type IN ('related_to', 'references', 'extends',
                                 'contradicts', 'part_of')),
        UNIQUE (space_id, from_page_id, to_page_id, relation_type)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_change_set_items (
        id               TEXT PRIMARY KEY,
        change_set_id    TEXT NOT NULL REFERENCES wiki_change_sets(id) ON DELETE CASCADE,
        ordinal          INTEGER NOT NULL,
        operation_kind   TEXT NOT NULL,
        target_id        TEXT NOT NULL DEFAULT '',
        base_version     INTEGER,
        before_sha256    TEXT NOT NULL DEFAULT '',
        payload_json     TEXT NOT NULL,
        unified_diff     TEXT NOT NULL DEFAULT '',
        created_at_ms    INTEGER NOT NULL,
        CHECK (ordinal >= 0),
        CHECK (operation_kind IN ('page_create', 'page_update', 'page_delete',
                                  'edge_add', 'edge_delete')),
        CHECK (base_version IS NULL OR base_version >= 0),
        CHECK (before_sha256 = '' OR length(before_sha256) = 64),
        CHECK (json_valid(payload_json)),
        UNIQUE (change_set_id, ordinal)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_jobs (
        id              TEXT PRIMARY KEY,
        space_id        TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        source_id       TEXT REFERENCES wiki_sources(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'queued',
        attempt         INTEGER NOT NULL DEFAULT 1,
        requested_mode  TEXT NOT NULL DEFAULT 'builtin',
        base_selection_version INTEGER NOT NULL DEFAULT 0,
        base_selected_parse_revision_id TEXT,
        safe_error_code TEXT NOT NULL DEFAULT '',
        created_at_ms   INTEGER NOT NULL,
        started_at_ms   INTEGER,
        finished_at_ms  INTEGER,
        CHECK (kind IN ('parse', 'summarize_source', 'synthesize_entry_page',
                        'synthesize_topic_pages', 'rebuild_search',
                        'rebuild_graph_projection')),
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        CHECK (attempt >= 1),
        CHECK (requested_mode IN ('builtin', 'pipeline', 'gpu-medium', 'gpu-high')),
        CHECK (base_selection_version >= 0),
        CHECK ((base_selected_parse_revision_id IS NULL AND base_selection_version = 0) OR
               (base_selected_parse_revision_id IS NOT NULL AND
                base_selection_version >= 1)),
        CHECK (started_at_ms IS NULL OR started_at_ms >= created_at_ms),
        CHECK (finished_at_ms IS NULL OR finished_at_ms >= created_at_ms),
        UNIQUE (source_id, id),
        FOREIGN KEY (source_id, base_selected_parse_revision_id)
            REFERENCES wiki_parse_revisions(source_id, id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_parse_attempts (
        id                       TEXT PRIMARY KEY,
        provider_attempt_id      TEXT NOT NULL,
        source_id                TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE CASCADE,
        job_id                   TEXT NOT NULL REFERENCES wiki_jobs(id) ON DELETE CASCADE,
        ordinal                  INTEGER NOT NULL,
        requested_mode           TEXT NOT NULL,
        source_sha256            TEXT NOT NULL,
        parser                   TEXT NOT NULL,
        parser_version           TEXT NOT NULL,
        preset                   TEXT NOT NULL,
        routing_config_revision  TEXT NOT NULL DEFAULT '',
        routing_config_sha256    TEXT NOT NULL DEFAULT '',
        route_reasons_json       TEXT NOT NULL,
        state                    TEXT NOT NULL,
        safe_error_code          TEXT NOT NULL DEFAULT '',
        quality_report_json      TEXT NOT NULL DEFAULT '{}',
        output_size_bytes        INTEGER,
        output_sha256            TEXT,
        fallback_from_attempt_id TEXT,
        started_at_ms            INTEGER,
        finished_at_ms           INTEGER,
        CHECK (ordinal >= 1),
        CHECK (requested_mode IN ('builtin', 'pipeline', 'gpu-medium', 'gpu-high')),
        CHECK (length(source_sha256) = 64 AND source_sha256 = lower(source_sha256)),
        CHECK (provider_attempt_id <> '' AND parser <> '' AND parser_version <> ''),
        CHECK (preset <> ''),
        CHECK ((routing_config_revision = '' AND routing_config_sha256 = '') OR
               (routing_config_revision <> '' AND length(routing_config_sha256) = 64)),
        CHECK (json_valid(route_reasons_json) AND json_type(route_reasons_json) = 'array'),
        CHECK (state IN ('queued', 'running', 'succeeded', 'failed',
                         'quality_rejected', 'cancelled')),
        CHECK (json_valid(quality_report_json) AND json_type(quality_report_json) = 'object'),
        CHECK ((output_size_bytes IS NULL AND output_sha256 IS NULL) OR
               (output_size_bytes > 0 AND length(output_sha256) = 64)),
        CHECK (started_at_ms IS NULL OR started_at_ms >= 0),
        CHECK (finished_at_ms IS NULL OR finished_at_ms >= started_at_ms),
        CHECK ((state = 'queued' AND started_at_ms IS NULL) OR
               (state <> 'queued' AND started_at_ms IS NOT NULL)),
        CHECK ((state IN ('succeeded', 'failed', 'quality_rejected', 'cancelled')) =
               (finished_at_ms IS NOT NULL)),
        CHECK ((state = 'succeeded' AND output_sha256 IS NOT NULL AND safe_error_code = '') OR
               (state IN ('failed', 'quality_rejected', 'cancelled') AND
                output_sha256 IS NULL AND safe_error_code <> '') OR
               (state IN ('queued', 'running') AND output_sha256 IS NULL AND
                safe_error_code = '')),
        UNIQUE (job_id, ordinal),
        UNIQUE (job_id, provider_attempt_id),
        UNIQUE (job_id, id),
        FOREIGN KEY (source_id, job_id)
            REFERENCES wiki_jobs(source_id, id) ON DELETE CASCADE,
        FOREIGN KEY (job_id, fallback_from_attempt_id)
            REFERENCES wiki_parse_attempts(job_id, id) ON DELETE RESTRICT
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_parse_revisions (
        id                         TEXT PRIMARY KEY,
        source_id                  TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE CASCADE,
        job_id                     TEXT NOT NULL UNIQUE REFERENCES wiki_jobs(id) ON DELETE RESTRICT,
        selected_attempt_id        TEXT NOT NULL REFERENCES wiki_parse_attempts(id)
                                             ON DELETE RESTRICT,
        contract_version           INTEGER NOT NULL,
        artifact_schema            TEXT NOT NULL,
        requested_mode             TEXT NOT NULL,
        source_sha256              TEXT NOT NULL,
        parser                     TEXT NOT NULL,
        parser_version             TEXT NOT NULL,
        preset                     TEXT NOT NULL,
        routing_config_revision    TEXT NOT NULL DEFAULT '',
        routing_config_sha256      TEXT NOT NULL DEFAULT '',
        parsed_markdown_relpath    TEXT NOT NULL,
        parsed_markdown_sha256     TEXT NOT NULL,
        manifest_relpath           TEXT NOT NULL,
        manifest_sha256            TEXT NOT NULL,
        page_count                 INTEGER NOT NULL,
        created_at_ms              INTEGER NOT NULL,
        CHECK (contract_version >= 1),
        CHECK (artifact_schema <> ''),
        CHECK (requested_mode IN ('builtin', 'pipeline', 'gpu-medium', 'gpu-high')),
        CHECK (length(source_sha256) = 64 AND source_sha256 = lower(source_sha256)),
        CHECK (parser <> '' AND parser_version <> '' AND preset <> ''),
        CHECK ((routing_config_revision = '' AND routing_config_sha256 = '') OR
               (routing_config_revision <> '' AND length(routing_config_sha256) = 64)),
        CHECK (parsed_markdown_relpath <> '' AND length(parsed_markdown_sha256) = 64),
        CHECK (manifest_relpath <> '' AND length(manifest_sha256) = 64),
        CHECK (page_count >= 1),
        UNIQUE (source_id, id),
        FOREIGN KEY (source_id, job_id)
            REFERENCES wiki_jobs(source_id, id) ON DELETE RESTRICT,
        FOREIGN KEY (job_id, selected_attempt_id)
            REFERENCES wiki_parse_attempts(job_id, id) ON DELETE RESTRICT
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_source_summaries (
        id                       TEXT PRIMARY KEY,
        space_id                 TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        source_id                TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE CASCADE,
        parse_revision_id        TEXT NOT NULL REFERENCES wiki_parse_revisions(id)
                                          ON DELETE RESTRICT,
        job_id                   TEXT NOT NULL UNIQUE REFERENCES wiki_jobs(id) ON DELETE RESTRICT,
        selection_version        INTEGER NOT NULL,
        source_sha256            TEXT NOT NULL,
        parsed_markdown_sha256   TEXT NOT NULL,
        manifest_sha256          TEXT NOT NULL,
        page_count               INTEGER NOT NULL,
        prompt_revision          TEXT NOT NULL,
        provider                 TEXT NOT NULL,
        model                    TEXT NOT NULL,
        content_json             TEXT NOT NULL,
        content_sha256           TEXT NOT NULL,
        created_at_ms            INTEGER NOT NULL,
        CHECK (selection_version >= 1),
        CHECK (length(source_sha256) = 64 AND source_sha256 = lower(source_sha256)),
        CHECK (length(parsed_markdown_sha256) = 64 AND
               parsed_markdown_sha256 = lower(parsed_markdown_sha256)),
        CHECK (length(manifest_sha256) = 64 AND manifest_sha256 = lower(manifest_sha256)),
        CHECK (page_count >= 1),
        CHECK (prompt_revision <> '' AND provider <> '' AND model <> ''),
        CHECK (json_valid(content_json) AND json_type(content_json) = 'object'),
        CHECK (length(content_sha256) = 64 AND content_sha256 = lower(content_sha256)),
        UNIQUE (source_id, id),
        FOREIGN KEY (source_id, parse_revision_id)
            REFERENCES wiki_parse_revisions(source_id, id) ON DELETE RESTRICT,
        FOREIGN KEY (source_id, job_id)
            REFERENCES wiki_jobs(source_id, id) ON DELETE RESTRICT
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_page_proposals (
        id                  TEXT PRIMARY KEY,
        space_id            TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        source_id           TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE CASCADE,
        summary_id          TEXT NOT NULL REFERENCES wiki_source_summaries(id)
                                      ON DELETE RESTRICT,
        job_id              TEXT NOT NULL REFERENCES wiki_jobs(id) ON DELETE RESTRICT,
        kind                TEXT NOT NULL,
        topic_ordinal       INTEGER,
        parent_proposal_id  TEXT,
        title               TEXT NOT NULL,
        slug                TEXT NOT NULL,
        aliases_json        TEXT NOT NULL DEFAULT '[]',
        markdown            TEXT NOT NULL,
        content_sha256      TEXT NOT NULL,
        source_locator_json TEXT NOT NULL,
        created_at_ms       INTEGER NOT NULL,
        CHECK (kind IN ('entry', 'topic')),
        CHECK ((kind = 'entry' AND topic_ordinal IS NULL AND parent_proposal_id IS NULL) OR
               (kind = 'topic' AND topic_ordinal >= 0 AND parent_proposal_id IS NOT NULL)),
        CHECK (title <> '' AND slug <> '' AND markdown <> ''),
        CHECK (json_valid(aliases_json) AND json_type(aliases_json) = 'array'),
        CHECK (length(content_sha256) = 64 AND content_sha256 = lower(content_sha256)),
        CHECK (json_valid(source_locator_json) AND
               json_type(source_locator_json) = 'object'),
        UNIQUE (summary_id, kind, topic_ordinal),
        UNIQUE (summary_id, id),
        FOREIGN KEY (source_id, summary_id)
            REFERENCES wiki_source_summaries(source_id, id) ON DELETE RESTRICT,
        FOREIGN KEY (source_id, job_id)
            REFERENCES wiki_jobs(source_id, id) ON DELETE RESTRICT,
        FOREIGN KEY (summary_id, parent_proposal_id)
            REFERENCES wiki_page_proposals(summary_id, id) ON DELETE RESTRICT
    ) STRICT;
    """,
    "CREATE INDEX IF NOT EXISTS idx_wiki_sources_space ON wiki_sources(space_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_sources_status ON wiki_sources(status);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_artifacts_source ON wiki_artifacts(source_id);",
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_artifacts_revision "
        "ON wiki_artifacts(parse_revision_id);"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_parse_attempts_source_job "
        "ON wiki_parse_attempts(source_id, job_id, ordinal);"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_parse_revisions_source "
        "ON wiki_parse_revisions(source_id, created_at_ms, id);"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_source_summaries_source "
        "ON wiki_source_summaries(source_id, created_at_ms, id);"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_page_proposals_entry "
        "ON wiki_page_proposals(summary_id) WHERE kind = 'entry';"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_page_proposals_source "
        "ON wiki_page_proposals(source_id, created_at_ms, id);"
    ),
    "CREATE INDEX IF NOT EXISTS idx_wiki_pages_space_status ON wiki_pages(space_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page ON wiki_page_revisions(page_id, version);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_page_sources_source ON wiki_page_sources(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_edges_space ON wiki_edges(space_id);",
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_changes_space_status "
        "ON wiki_change_sets(space_id, status);"
    ),
    "CREATE INDEX IF NOT EXISTS idx_wiki_conversations_space ON wiki_conversations(space_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_jobs_space_status ON wiki_jobs(space_id, status);",
)

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "wiki_spaces": frozenset(
        {
            "id",
            "name",
            "description",
            "status",
            "graph_revision",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_sources": frozenset(
        {
            "id",
            "space_id",
            "display_name",
            "mime_type",
            "size_bytes",
            "source_sha256",
            "source_relpath",
            "selected_parse_revision_id",
            "selection_version",
            "selected_at_ms",
            "status",
            "safe_error_code",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_artifacts": frozenset(
        {
            "id",
            "source_id",
            "parse_revision_id",
            "kind",
            "relpath",
            "mime_type",
            "size_bytes",
            "sha256",
            "width",
            "height",
            "source_locator_json",
            "created_at_ms",
        }
    ),
    "wiki_pages": frozenset(
        {
            "id",
            "space_id",
            "slug",
            "title",
            "aliases_json",
            "status",
            "current_revision_id",
            "version",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_page_revisions": frozenset(
        {
            "id",
            "page_id",
            "version",
            "title",
            "markdown",
            "content_sha256",
            "change_set_id",
            "author_kind",
            "created_at_ms",
        }
    ),
    "wiki_pages_fts": frozenset({"page_id", "space_id", "title", "aliases", "content"}),
    "wiki_page_sources": frozenset(
        {"page_id", "source_id", "source_locator_json", "created_at_ms"}
    ),
    "wiki_edges": frozenset(
        {
            "id",
            "space_id",
            "from_page_id",
            "to_page_id",
            "relation_type",
            "change_set_id",
            "created_at_ms",
        }
    ),
    "wiki_change_sets": frozenset(
        {
            "id",
            "space_id",
            "conversation_id",
            "source_summary_id",
            "status",
            "base_graph_revision",
            "summary",
            "safe_error_code",
            "created_at_ms",
            "decided_at_ms",
            "published_at_ms",
        }
    ),
    "wiki_change_set_items": frozenset(
        {
            "id",
            "change_set_id",
            "ordinal",
            "operation_kind",
            "target_id",
            "base_version",
            "before_sha256",
            "payload_json",
            "unified_diff",
            "created_at_ms",
        }
    ),
    "wiki_conversations": frozenset(
        {
            "id",
            "space_id",
            "session_id",
            "title",
            "status",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_jobs": frozenset(
        {
            "id",
            "space_id",
            "source_id",
            "kind",
            "status",
            "attempt",
            "requested_mode",
            "base_selection_version",
            "base_selected_parse_revision_id",
            "safe_error_code",
            "created_at_ms",
            "started_at_ms",
            "finished_at_ms",
        }
    ),
    "wiki_parse_attempts": frozenset(
        {
            "id",
            "provider_attempt_id",
            "source_id",
            "job_id",
            "ordinal",
            "requested_mode",
            "source_sha256",
            "parser",
            "parser_version",
            "preset",
            "routing_config_revision",
            "routing_config_sha256",
            "route_reasons_json",
            "state",
            "safe_error_code",
            "quality_report_json",
            "output_size_bytes",
            "output_sha256",
            "fallback_from_attempt_id",
            "started_at_ms",
            "finished_at_ms",
        }
    ),
    "wiki_parse_revisions": frozenset(
        {
            "id",
            "source_id",
            "job_id",
            "selected_attempt_id",
            "contract_version",
            "artifact_schema",
            "requested_mode",
            "source_sha256",
            "parser",
            "parser_version",
            "preset",
            "routing_config_revision",
            "routing_config_sha256",
            "parsed_markdown_relpath",
            "parsed_markdown_sha256",
            "manifest_relpath",
            "manifest_sha256",
            "page_count",
            "created_at_ms",
        }
    ),
    "wiki_source_summaries": frozenset(
        {
            "id",
            "space_id",
            "source_id",
            "parse_revision_id",
            "job_id",
            "selection_version",
            "source_sha256",
            "parsed_markdown_sha256",
            "manifest_sha256",
            "page_count",
            "prompt_revision",
            "provider",
            "model",
            "content_json",
            "content_sha256",
            "created_at_ms",
        }
    ),
    "wiki_page_proposals": frozenset(
        {
            "id",
            "space_id",
            "source_id",
            "summary_id",
            "job_id",
            "kind",
            "topic_ordinal",
            "parent_proposal_id",
            "title",
            "slug",
            "aliases_json",
            "markdown",
            "content_sha256",
            "source_locator_json",
            "created_at_ms",
        }
    ),
}

_STATUS_TRANSITIONS: dict[WikiSpaceStatus, frozenset[WikiSpaceStatus]] = {
    "active": frozenset({"archived", "deleting", "failed"}),
    "archived": frozenset({"active", "deleting", "failed"}),
    "failed": frozenset({"active", "deleting"}),
    "deleting": frozenset(),
}

_SOURCE_STATUS_TRANSITIONS: dict[WikiSourceStatus, frozenset[WikiSourceStatus]] = {
    "uploaded": frozenset({"parsing", "deleting"}),
    "parsing": frozenset({"parsed", "failed", "deleting"}),
    "parsed": frozenset({"parsing", "deleting"}),
    "failed": frozenset({"parsing", "deleting"}),
    "deleting": frozenset(),
}

_JOB_STATUS_TRANSITIONS: dict[WikiJobStatus, frozenset[WikiJobStatus]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def _row_to_space(row: aiosqlite.Row) -> WikiSpace:
    return WikiSpace(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        graph_revision=row["graph_revision"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_source(row: aiosqlite.Row) -> WikiSource:
    return WikiSource(
        id=row["id"],
        space_id=row["space_id"],
        display_name=row["display_name"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        source_sha256=row["source_sha256"],
        source_relpath=row["source_relpath"],
        selected_parse_revision_id=row["selected_parse_revision_id"],
        selection_version=row["selection_version"],
        selected_at_ms=row["selected_at_ms"],
        status=row["status"],
        safe_error_code=row["safe_error_code"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_artifact(row: aiosqlite.Row) -> WikiArtifact:
    return WikiArtifact(
        id=row["id"],
        source_id=row["source_id"],
        parse_revision_id=row["parse_revision_id"],
        kind=row["kind"],
        relpath=row["relpath"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        width=row["width"],
        height=row["height"],
        source_locator_json=row["source_locator_json"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_job(row: aiosqlite.Row) -> WikiJob:
    return WikiJob(
        id=row["id"],
        space_id=row["space_id"],
        source_id=row["source_id"],
        kind=row["kind"],
        status=row["status"],
        attempt=row["attempt"],
        requested_mode=row["requested_mode"],
        base_selection_version=row["base_selection_version"],
        base_selected_parse_revision_id=row["base_selected_parse_revision_id"],
        safe_error_code=row["safe_error_code"],
        created_at_ms=row["created_at_ms"],
        started_at_ms=row["started_at_ms"],
        finished_at_ms=row["finished_at_ms"],
    )


def _row_to_parse_attempt(row: aiosqlite.Row) -> WikiParseAttempt:
    return WikiParseAttempt(
        id=row["id"],
        provider_attempt_id=row["provider_attempt_id"],
        source_id=row["source_id"],
        job_id=row["job_id"],
        ordinal=row["ordinal"],
        requested_mode=row["requested_mode"],
        source_sha256=row["source_sha256"],
        parser=row["parser"],
        parser_version=row["parser_version"],
        preset=row["preset"],
        routing_config_revision=row["routing_config_revision"],
        routing_config_sha256=row["routing_config_sha256"],
        route_reasons_json=row["route_reasons_json"],
        state=row["state"],
        safe_error_code=row["safe_error_code"],
        quality_report_json=row["quality_report_json"],
        output_size_bytes=row["output_size_bytes"],
        output_sha256=row["output_sha256"],
        fallback_from_attempt_id=row["fallback_from_attempt_id"],
        started_at_ms=row["started_at_ms"],
        finished_at_ms=row["finished_at_ms"],
    )


def _row_to_parse_revision(row: aiosqlite.Row) -> WikiParseRevision:
    return WikiParseRevision(
        id=row["id"],
        source_id=row["source_id"],
        job_id=row["job_id"],
        selected_attempt_id=row["selected_attempt_id"],
        contract_version=row["contract_version"],
        artifact_schema=row["artifact_schema"],
        requested_mode=row["requested_mode"],
        source_sha256=row["source_sha256"],
        parser=row["parser"],
        parser_version=row["parser_version"],
        preset=row["preset"],
        routing_config_revision=row["routing_config_revision"],
        routing_config_sha256=row["routing_config_sha256"],
        parsed_markdown_relpath=row["parsed_markdown_relpath"],
        parsed_markdown_sha256=row["parsed_markdown_sha256"],
        manifest_relpath=row["manifest_relpath"],
        manifest_sha256=row["manifest_sha256"],
        page_count=row["page_count"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_source_summary(row: aiosqlite.Row) -> WikiSourceSummaryDraft:
    return WikiSourceSummaryDraft(
        id=row["id"],
        space_id=row["space_id"],
        source_id=row["source_id"],
        parse_revision_id=row["parse_revision_id"],
        job_id=row["job_id"],
        selection_version=row["selection_version"],
        source_sha256=row["source_sha256"],
        parsed_markdown_sha256=row["parsed_markdown_sha256"],
        manifest_sha256=row["manifest_sha256"],
        page_count=row["page_count"],
        prompt_revision=row["prompt_revision"],
        provider=row["provider"],
        model=row["model"],
        content=WikiSourceSummaryContent.model_validate_json(row["content_json"]),
        content_sha256=row["content_sha256"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_page_proposal(row: aiosqlite.Row) -> WikiPageProposal:
    return WikiPageProposal(
        id=row["id"],
        space_id=row["space_id"],
        source_id=row["source_id"],
        summary_id=row["summary_id"],
        job_id=row["job_id"],
        kind=row["kind"],
        topic_ordinal=row["topic_ordinal"],
        parent_proposal_id=row["parent_proposal_id"],
        title=row["title"],
        slug=row["slug"],
        aliases=tuple(json.loads(row["aliases_json"])),
        markdown=row["markdown"],
        content_sha256=row["content_sha256"],
        source_locator_json=row["source_locator_json"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_page(row: aiosqlite.Row) -> WikiPage:
    return WikiPage(
        id=row["id"],
        space_id=row["space_id"],
        slug=row["slug"],
        title=row["title"],
        aliases=tuple(json.loads(row["aliases_json"])),
        status=row["status"],
        current_revision_id=row["current_revision_id"],
        version=row["version"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_page_revision(row: aiosqlite.Row) -> WikiPageRevision:
    return WikiPageRevision(
        id=row["id"],
        page_id=row["page_id"],
        version=row["version"],
        title=row["title"],
        markdown=row["markdown"],
        content_sha256=row["content_sha256"],
        change_set_id=row["change_set_id"],
        author_kind=row["author_kind"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_edge(row: aiosqlite.Row) -> WikiEdge:
    return WikiEdge(
        id=row["id"],
        space_id=row["space_id"],
        from_page_id=row["from_page_id"],
        to_page_id=row["to_page_id"],
        relation_type=row["relation_type"],
        change_set_id=row["change_set_id"],
        created_at_ms=row["created_at_ms"],
    )


def _row_to_conversation(row: aiosqlite.Row) -> WikiConversation:
    return WikiConversation(
        id=row["id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        title=row["title"],
        status=row["status"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_change_set(row: aiosqlite.Row) -> WikiChangeSet:
    return WikiChangeSet(
        id=row["id"],
        space_id=row["space_id"],
        conversation_id=row["conversation_id"],
        source_summary_id=row["source_summary_id"],
        status=row["status"],
        base_graph_revision=row["base_graph_revision"],
        summary=row["summary"],
        safe_error_code=row["safe_error_code"],
        created_at_ms=row["created_at_ms"],
        decided_at_ms=row["decided_at_ms"],
        published_at_ms=row["published_at_ms"],
    )


def _row_to_change_set_item(row: aiosqlite.Row) -> WikiChangeSetItem:
    return WikiChangeSetItem(
        id=row["id"],
        change_set_id=row["change_set_id"],
        ordinal=row["ordinal"],
        operation_kind=row["operation_kind"],
        target_id=row["target_id"],
        base_version=row["base_version"],
        before_sha256=row["before_sha256"],
        payload_json=row["payload_json"],
        unified_diff=row["unified_diff"],
        created_at_ms=row["created_at_ms"],
    )


def _render_page_create_diff(slug: str, markdown: str) -> str:
    lines = markdown.splitlines()
    header = (
        "--- /dev/null",
        f"+++ pages/{slug}.md",
        f"@@ -0,0 +1,{len(lines)} @@",
    )
    return "\n".join((*header, *(f"+{line}" for line in lines))) + "\n"


def _render_edge_add_diff(
    from_slug: str,
    to_slug: str,
    relation_type: str,
) -> str:
    return f"+ edge pages/{from_slug}.md --{relation_type}--> pages/{to_slug}.md\n"


def _render_page_update_diff(slug: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"pages/{slug}.md",
        tofile=f"pages/{slug}.md",
        lineterm="",
    )
    rendered = "\n".join(lines)
    return f"{rendered}\n" if rendered else ""


async def _enable_wal(
    connection: aiosqlite.Connection, *, timeout_seconds: float = 5.0,
) -> None:
    """Retry only startup lock contention; never continue in a fallback mode."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            # A competing opener can hold a schema/read lock during this mode
            # transition. SQLite may report BUSY without invoking busy_timeout.
            async with await connection.execute("PRAGMA journal_mode=WAL") as cursor:
                row = await cursor.fetchone()
            if row is None or str(row[0]).lower() != "wal":
                raise WikiStoreError("invalid_configuration")
            return
        except aiosqlite.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if (
                code is None or code & 0xFF != sqlite3.SQLITE_BUSY
                or time.monotonic() >= deadline
            ):
                raise
            await asyncio.sleep(0.05)


class WikiStore:
    """Own ``wiki.db`` and the rebuildable account-scoped Wiki mirror."""

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        file_store: WikiFileStore,
        owns_connection: bool,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        source_id_factory: Callable[[], str] | None = None,
        artifact_id_factory: Callable[[], str] | None = None,
        job_id_factory: Callable[[], str] | None = None,
        parse_attempt_id_factory: Callable[[], str] | None = None,
        parse_revision_id_factory: Callable[[], str] | None = None,
        summary_id_factory: Callable[[], str] | None = None,
        page_proposal_id_factory: Callable[[], str] | None = None,
        change_set_id_factory: Callable[[], str] | None = None,
        change_set_item_id_factory: Callable[[], str] | None = None,
        page_id_factory: Callable[[], str] | None = None,
        page_revision_id_factory: Callable[[], str] | None = None,
        edge_id_factory: Callable[[], str] | None = None,
        conversation_id_factory: Callable[[], str] | None = None,
        legacy_backup_receipt: WikiLegacyBackupReceipt | None = None,
    ) -> None:
        self._db: aiosqlite.Connection | None = connection
        self._file_store = file_store
        self._owns_connection = owns_connection
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._id_factory = id_factory or (lambda: f"space_{secrets.token_hex(12)}")
        self._source_id_factory = source_id_factory or (lambda: f"source_{secrets.token_hex(12)}")
        self._artifact_id_factory = artifact_id_factory or (
            lambda: f"artifact_{secrets.token_hex(12)}"
        )
        self._job_id_factory = job_id_factory or (lambda: f"job_{secrets.token_hex(12)}")
        self._parse_attempt_id_factory = parse_attempt_id_factory or (
            lambda: f"parse_attempt_{secrets.token_hex(12)}"
        )
        self._parse_revision_id_factory = parse_revision_id_factory or (
            lambda: f"parse_revision_{secrets.token_hex(12)}"
        )
        self._summary_id_factory = summary_id_factory or (
            lambda: f"summary_{secrets.token_hex(12)}"
        )
        self._page_proposal_id_factory = page_proposal_id_factory or (
            lambda: f"page_proposal_{secrets.token_hex(12)}"
        )
        self._change_set_id_factory = change_set_id_factory or (
            lambda: f"change_set_{secrets.token_hex(12)}"
        )
        self._change_set_item_id_factory = change_set_item_id_factory or (
            lambda: f"change_item_{secrets.token_hex(12)}"
        )
        self._page_id_factory = page_id_factory or (lambda: f"page_{secrets.token_hex(12)}")
        self._page_revision_id_factory = page_revision_id_factory or (
            lambda: f"page_revision_{secrets.token_hex(12)}"
        )
        self._edge_id_factory = edge_id_factory or (lambda: f"edge_{secrets.token_hex(12)}")
        self._conversation_id_factory = conversation_id_factory or (
            lambda: f"conversation_{secrets.token_hex(12)}"
        )
        self._legacy_backup_receipt = legacy_backup_receipt
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._startup_repair_report = WikiMirrorRepairReport()

    @classmethod
    async def open(
        cls,
        root: str | Path,
        *,
        legacy_policy: LegacyPolicy = "preserve",
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        source_id_factory: Callable[[], str] | None = None,
        artifact_id_factory: Callable[[], str] | None = None,
        job_id_factory: Callable[[], str] | None = None,
        parse_attempt_id_factory: Callable[[], str] | None = None,
        parse_revision_id_factory: Callable[[], str] | None = None,
        summary_id_factory: Callable[[], str] | None = None,
        page_proposal_id_factory: Callable[[], str] | None = None,
        change_set_id_factory: Callable[[], str] | None = None,
        change_set_item_id_factory: Callable[[], str] | None = None,
        page_id_factory: Callable[[], str] | None = None,
        page_revision_id_factory: Callable[[], str] | None = None,
        edge_id_factory: Callable[[], str] | None = None,
        conversation_id_factory: Callable[[], str] | None = None,
        backup_id_factory: Callable[[], str] | None = None,
    ) -> WikiStore:
        if legacy_policy not in {"preserve", "retire"}:
            raise WikiStoreError("invalid_configuration")
        file_store = WikiFileStore(root)
        try:
            await asyncio.to_thread(file_store.ensure_root)
            await asyncio.to_thread(file_store.validate_database_paths)
        except WikiStoreError:
            raise
        except OSError as exc:
            raise WikiStoreError("invalid_configuration") from exc
        backup_receipt = None
        if legacy_policy == "retire":
            backup_receipt = await asyncio.to_thread(
                retire_legacy_knowledge,
                file_store.root,
                clock_ms=clock_ms,
                id_factory=backup_id_factory,
            )
        connection = await aiosqlite.connect(
            str(file_store.database_path),
            isolation_level=None,
        )
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA trusted_schema=OFF")
            await connection.execute("PRAGMA busy_timeout=5000")
            await connection.execute("PRAGMA synchronous=FULL")
            store = cls(
                connection=connection,
                file_store=file_store,
                owns_connection=True,
                clock_ms=clock_ms,
                id_factory=id_factory,
                source_id_factory=source_id_factory,
                artifact_id_factory=artifact_id_factory,
                job_id_factory=job_id_factory,
                parse_attempt_id_factory=parse_attempt_id_factory,
                parse_revision_id_factory=parse_revision_id_factory,
                summary_id_factory=summary_id_factory,
                page_proposal_id_factory=page_proposal_id_factory,
                change_set_id_factory=change_set_id_factory,
                change_set_item_id_factory=change_set_item_id_factory,
                page_id_factory=page_id_factory,
                page_revision_id_factory=page_revision_id_factory,
                edge_id_factory=edge_id_factory,
                conversation_id_factory=conversation_id_factory,
                legacy_backup_receipt=backup_receipt,
            )
            await store._initialize_schema()
            await _enable_wal(connection)
            store._startup_repair_report = await store.repair_space_mirrors()
            await store.repair_selected_parse_pointers()
            await store.repair_page_mirrors()
            await store.repair_page_search_index()
        except BaseException:
            await connection.close()
            raise
        return store

    @classmethod
    async def for_testing(
        cls,
        connection: aiosqlite.Connection,
        *,
        root: str | Path,
        owns_connection: bool = False,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        source_id_factory: Callable[[], str] | None = None,
        artifact_id_factory: Callable[[], str] | None = None,
        job_id_factory: Callable[[], str] | None = None,
        parse_attempt_id_factory: Callable[[], str] | None = None,
        parse_revision_id_factory: Callable[[], str] | None = None,
        summary_id_factory: Callable[[], str] | None = None,
        page_proposal_id_factory: Callable[[], str] | None = None,
        change_set_id_factory: Callable[[], str] | None = None,
        change_set_item_id_factory: Callable[[], str] | None = None,
        page_id_factory: Callable[[], str] | None = None,
        page_revision_id_factory: Callable[[], str] | None = None,
        edge_id_factory: Callable[[], str] | None = None,
        conversation_id_factory: Callable[[], str] | None = None,
    ) -> WikiStore:
        if getattr(connection, "isolation_level", "") is not None:
            raise WikiStoreError("invalid_configuration")
        file_store = WikiFileStore(root)
        await asyncio.to_thread(file_store.ensure_root)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        store = cls(
            connection=connection,
            file_store=file_store,
            owns_connection=owns_connection,
            clock_ms=clock_ms,
            id_factory=id_factory,
            source_id_factory=source_id_factory,
            artifact_id_factory=artifact_id_factory,
            job_id_factory=job_id_factory,
            parse_attempt_id_factory=parse_attempt_id_factory,
            parse_revision_id_factory=parse_revision_id_factory,
            summary_id_factory=summary_id_factory,
            page_proposal_id_factory=page_proposal_id_factory,
            change_set_id_factory=change_set_id_factory,
            change_set_item_id_factory=change_set_item_id_factory,
            page_id_factory=page_id_factory,
            page_revision_id_factory=page_revision_id_factory,
            edge_id_factory=edge_id_factory,
            conversation_id_factory=conversation_id_factory,
        )
        await store._initialize_schema()
        store._startup_repair_report = await store.repair_space_mirrors()
        await store.repair_selected_parse_pointers()
        await store.repair_page_mirrors()
        await store.repair_page_search_index()
        return store

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def file_store(self) -> WikiFileStore:
        return self._file_store

    @property
    def legacy_backup_receipt(self) -> WikiLegacyBackupReceipt | None:
        return self._legacy_backup_receipt

    @property
    def startup_repair_report(self) -> WikiMirrorRepairReport:
        return self._startup_repair_report

    async def close(self) -> None:
        connection = self._db
        self._db = None
        self._closed = True
        if self._owns_connection and connection is not None:
            await connection.close()

    def _require_db(self) -> aiosqlite.Connection:
        if self._closed or self._db is None:
            raise WikiStoreError("invalid_configuration")
        return self._db

    async def get_schema_version(self) -> int | None:
        db = self._require_db()
        async with db.execute(
            "SELECT value FROM wiki_schema_meta WHERE key = ?",
            (_SCHEMA_META_KEY,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else int(row["value"])

    async def create_space(self, *, name: str, description: str = "") -> WikiSpace:
        now = self._clock_ms()
        try:
            space = WikiSpace(
                id=self._id_factory(),
                name=name,
                description=description,
                created_at_ms=now,
                updated_at_ms=now,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_space") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """
                    INSERT INTO wiki_spaces (
                        id, name, description, status, graph_revision,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        space.id,
                        space.name,
                        space.description,
                        space.status,
                        space.graph_revision,
                        space.created_at_ms,
                        space.updated_at_ms,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("space_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        try:
            await asyncio.to_thread(self._file_store.create_or_repair_space_layout, space)
        except Exception as exc:
            raise WikiMirrorError("mirror_failed") from exc
        return space

    async def get_space(self, space_id: str) -> WikiSpace:
        try:
            validate_space_id(space_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_spaces WHERE id = ?",
            (space_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("space_not_found")
        return _row_to_space(row)

    async def list_spaces(
        self,
        *,
        statuses: Sequence[WikiSpaceStatus] | None = None,
    ) -> tuple[WikiSpace, ...]:
        db = self._require_db()
        if statuses is None:
            query = "SELECT * FROM wiki_spaces ORDER BY created_at_ms, id"
            parameters: tuple[object, ...] = ()
        else:
            if not statuses or any(status not in _STATUS_TRANSITIONS for status in statuses):
                raise WikiStoreError("invalid_space")
            unique = tuple(dict.fromkeys(statuses))
            placeholders = ",".join("?" for _ in unique)
            query = (
                f"SELECT * FROM wiki_spaces WHERE status IN ({placeholders}) "
                "ORDER BY created_at_ms, id"
            )
            parameters = tuple(unique)
        async with db.execute(query, parameters) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_space(row) for row in rows)

    async def update_space(
        self,
        space_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSpace:
        if name is None and description is None:
            raise WikiStoreError("invalid_space")
        current = await self.get_space(space_id)
        try:
            candidate = current.model_copy(
                update={
                    "name": current.name if name is None else name,
                    "description": (current.description if description is None else description),
                }
            )
            candidate = WikiSpace.model_validate(candidate.model_dump())
        except ValueError as exc:
            raise WikiStoreError("invalid_space") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("space_not_found")
                locked = _row_to_space(row)
                if locked.status != "active":
                    raise WikiStoreError("space_read_only")
                if (
                    expected_updated_at_ms is not None
                    and locked.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("space_conflict")
                updated_at = max(self._clock_ms(), locked.updated_at_ms + 1)
                await db.execute(
                    """
                    UPDATE wiki_spaces
                    SET name = ?, description = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (candidate.name, candidate.description, updated_at, space_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        updated = await self.get_space(space_id)
        await self._sync_mirror_or_raise(updated)
        return updated

    async def set_space_status(
        self,
        space_id: str,
        status: WikiSpaceStatus,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSpace:
        if status not in _STATUS_TRANSITIONS:
            raise WikiStoreError("invalid_space")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("space_not_found")
                current = _row_to_space(row)
                if (
                    expected_updated_at_ms is not None
                    and current.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("space_conflict")
                if status == current.status:
                    await db.execute("COMMIT")
                    return current
                if status not in _STATUS_TRANSITIONS[current.status]:
                    raise WikiStoreError("invalid_status_transition")
                if status == "archived":
                    checks = (
                        "SELECT 1 FROM wiki_jobs WHERE space_id = ? "
                        "AND status IN ('queued', 'running') LIMIT 1",
                        "SELECT 1 FROM wiki_conversations WHERE space_id = ? "
                        "AND status = 'active' LIMIT 1",
                        "SELECT 1 FROM wiki_change_sets WHERE space_id = ? "
                        "AND status IN ('draft', 'awaiting_approval') LIMIT 1",
                    )
                    for query in checks:
                        async with db.execute(query, (space_id,)) as cursor:
                            if await cursor.fetchone() is not None:
                                raise WikiStoreError("space_in_use")
                updated_at = max(self._clock_ms(), current.updated_at_ms + 1)
                await db.execute(
                    "UPDATE wiki_spaces SET status = ?, updated_at_ms = ? WHERE id = ?",
                    (status, updated_at, space_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        updated = await self.get_space(space_id)
        await self._sync_mirror_or_raise(updated)
        return updated

    async def delete_space(
        self,
        space_id: str,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSpace:
        """Enter ``deleting`` only after all user-visible work is retired."""
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("space_not_found")
                current = _row_to_space(row)
                if (
                    expected_updated_at_ms is not None
                    and current.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("space_conflict")
                if current.status == "deleting":
                    await db.execute("COMMIT")
                    return current
                checks = (
                    (
                        "SELECT 1 FROM wiki_sources "
                        "WHERE space_id = ? AND status <> 'deleting' LIMIT 1"
                    ),
                    (
                        "SELECT 1 FROM wiki_pages "
                        "WHERE space_id = ? AND status = 'active' LIMIT 1"
                    ),
                    (
                        "SELECT 1 FROM wiki_conversations "
                        "WHERE space_id = ? AND status = 'active' LIMIT 1"
                    ),
                    (
                        "SELECT 1 FROM wiki_jobs WHERE space_id = ? "
                        "AND status IN ('queued', 'running') LIMIT 1"
                    ),
                    (
                        "SELECT 1 FROM wiki_change_sets WHERE space_id = ? "
                        "AND status IN ('draft', 'awaiting_approval') LIMIT 1"
                    ),
                )
                for query in checks:
                    async with db.execute(query, (space_id,)) as cursor:
                        if await cursor.fetchone() is not None:
                            raise WikiStoreError("space_in_use")
                if "deleting" not in _STATUS_TRANSITIONS[current.status]:
                    raise WikiStoreError("invalid_status_transition")
                updated_at = max(self._clock_ms(), current.updated_at_ms + 1)
                await db.execute(
                    "UPDATE wiki_spaces SET status = 'deleting', updated_at_ms = ? WHERE id = ?",
                    (updated_at, space_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        updated = await self.get_space(space_id)
        await self._sync_mirror_or_raise(updated)
        return updated

    async def upload_source(
        self,
        space_id: str,
        *,
        display_name: str,
        mime_type: WikiSourceMimeType,
        content: bytes,
        max_bytes: int = 250 * 1024 * 1024,
    ) -> WikiSource:
        """Persist one no-clobber raw source and its canonical row."""
        if not content or len(content) > max_bytes:
            raise WikiStoreError("file_too_large")
        space = await self.get_space(space_id)
        if space.status != "active":
            raise WikiStoreError("space_read_only")
        now = self._clock_ms()
        source_id = self._source_id_factory()
        digest = hashlib.sha256(content).hexdigest()
        try:
            source = WikiSource(
                id=source_id,
                space_id=space_id,
                display_name=display_name,
                mime_type=mime_type,
                size_bytes=len(content),
                source_sha256=digest,
                source_relpath=self._file_store.source_relative_path(
                    source_id,
                    display_name,
                    mime_type,
                ),
                created_at_ms=now,
                updated_at_ms=now,
            )
        except (KeyError, ValueError) as exc:
            raise WikiStoreError("invalid_source") from exc
        wrote_file = False
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT id FROM wiki_sources WHERE space_id = ? AND source_sha256 = ?",
                    (space_id, digest),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise WikiStoreError("source_conflict")
                await asyncio.to_thread(
                    self._file_store.write_owned_file_atomic,
                    space_id,
                    source.source_relpath,
                    content,
                    overwrite=False,
                    max_bytes=max_bytes,
                )
                wrote_file = True
                async with db.execute(
                    "SELECT status FROM wiki_spaces WHERE id = ?",
                    (space_id,),
                ) as cursor:
                    space_row = await cursor.fetchone()
                if space_row is None:
                    raise WikiStoreError("space_not_found")
                if space_row["status"] != "active":
                    raise WikiStoreError("space_read_only")
                await db.execute(
                    """
                    INSERT INTO wiki_sources (
                        id, space_id, display_name, mime_type, size_bytes,
                        source_sha256, source_relpath, status,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.id,
                        source.space_id,
                        source.display_name,
                        source.mime_type,
                        source.size_bytes,
                        source.source_sha256,
                        source.source_relpath,
                        source.status,
                        source.created_at_ms,
                        source.updated_at_ms,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                await self._rollback_source_file(source, wrote_file, max_bytes)
                raise WikiStoreError("source_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                await self._rollback_source_file(source, wrote_file, max_bytes)
                raise
        return source

    async def get_source(self, source_id: str, *, space_id: str | None = None) -> WikiSource:
        try:
            validate_source_id(source_id)
            if space_id is not None:
                validate_space_id(space_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        query = "SELECT * FROM wiki_sources WHERE id = ?"
        parameters: tuple[object, ...] = (source_id,)
        if space_id is not None:
            query += " AND space_id = ?"
            parameters = (source_id, space_id)
        async with db.execute(query, parameters) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("source_not_found")
        return _row_to_source(row)

    async def list_sources(self, space_id: str) -> tuple[WikiSource, ...]:
        try:
            validate_space_id(space_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_sources WHERE space_id = ? ORDER BY created_at_ms, id",
            (space_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_source(row) for row in rows)

    async def list_sources_by_status(
        self,
        statuses: Sequence[WikiSourceStatus],
    ) -> tuple[WikiSource, ...]:
        if not statuses or any(status not in _SOURCE_STATUS_TRANSITIONS for status in statuses):
            raise WikiStoreError("invalid_source")
        unique = tuple(dict.fromkeys(statuses))
        placeholders = ",".join("?" for _ in unique)
        db = self._require_db()
        async with db.execute(
            f"SELECT * FROM wiki_sources WHERE status IN ({placeholders}) "
            "ORDER BY created_at_ms, id",
            tuple(unique),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_source(row) for row in rows)

    async def recover_interrupted_parses(self) -> tuple[tuple[str, WikiParseMode], ...]:
        """Fail stale work and return each source's latest requested mode."""
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    """
                    SELECT source_id, requested_mode, attempt FROM wiki_jobs
                    WHERE kind = 'parse' AND status IN ('queued', 'running')
                          AND source_id IS NOT NULL
                    ORDER BY source_id, attempt DESC, id DESC
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
                latest_modes: dict[str, WikiParseMode] = {}
                for row in rows:
                    source_id = str(row["source_id"])
                    if source_id not in latest_modes:
                        latest_modes[source_id] = cast(WikiParseMode, row["requested_mode"])
                requests = tuple(latest_modes.items())
                source_ids = tuple(latest_modes)
                now = self._clock_ms()
                await db.execute(
                    """
                    UPDATE wiki_jobs
                    SET status = 'failed', safe_error_code = 'provider_unavailable',
                        finished_at_ms = MAX(created_at_ms, ?)
                    WHERE kind = 'parse' AND status IN ('queued', 'running')
                    """,
                    (now,),
                )
                if source_ids:
                    placeholders = ",".join("?" for _ in source_ids)
                    await db.execute(
                        f"UPDATE wiki_sources SET status = 'failed', "
                        "safe_error_code = 'provider_unavailable', "
                        "updated_at_ms = MAX(updated_at_ms + 1, ?) "
                        f"WHERE status = 'parsing' AND id IN ({placeholders})",
                        (now, *source_ids),
                    )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return requests

    async def set_source_status(
        self,
        source_id: str,
        status: WikiSourceStatus,
        *,
        safe_error_code: str = "",
        expected_updated_at_ms: int | None = None,
    ) -> WikiSource:
        if status not in _SOURCE_STATUS_TRANSITIONS:
            raise WikiStoreError("invalid_source")
        if status == "parsed":
            raise WikiStoreError("invalid_source")
        if (status == "failed") != bool(safe_error_code):
            raise WikiStoreError("invalid_source")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (source_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("source_not_found")
                current = _row_to_source(row)
                if (
                    expected_updated_at_ms is not None
                    and current.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("source_conflict")
                if status == current.status:
                    await db.execute("COMMIT")
                    return current
                if status not in _SOURCE_STATUS_TRANSITIONS[current.status]:
                    raise WikiStoreError("invalid_status_transition")
                updated_at = max(self._clock_ms(), current.updated_at_ms + 1)
                await db.execute(
                    """
                    UPDATE wiki_sources
                    SET status = ?, safe_error_code = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (status, safe_error_code, updated_at, source_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return await self.get_source(source_id)

    async def request_source_deletion(
        self,
        source_id: str,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSource:
        """Make Raw bytes inaccessible and schedule retention cleanup.

        Source metadata remains as immutable audit evidence. A Source that is
        still projected by an active page, has active work, or feeds a pending
        Change Set cannot enter deletion.
        """
        try:
            validate_source_id(source_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (source_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("source_not_found")
                current = _row_to_source(row)
                if (
                    expected_updated_at_ms is not None
                    and current.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("source_conflict")
                if current.status == "deleting":
                    await db.execute("COMMIT")
                    return current
                async with db.execute(
                    """
                    SELECT 1
                    FROM wiki_page_sources AS ps
                    JOIN wiki_pages AS p ON p.id = ps.page_id
                    WHERE ps.source_id = ? AND p.status = 'active'
                    LIMIT 1
                    """,
                    (source_id,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise WikiStoreError("source_in_use")
                async with db.execute(
                    """
                    SELECT 1 FROM wiki_jobs
                    WHERE source_id = ? AND status IN ('queued', 'running')
                    LIMIT 1
                    """,
                    (source_id,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise WikiStoreError("source_conflict")
                async with db.execute(
                    """
                    SELECT 1
                    FROM wiki_change_sets AS c
                    JOIN wiki_source_summaries AS s ON s.id = c.source_summary_id
                    WHERE s.source_id = ? AND c.status IN ('draft', 'awaiting_approval')
                    LIMIT 1
                    """,
                    (source_id,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise WikiStoreError("source_conflict")
                updated_at = max(self._clock_ms(), current.updated_at_ms + 1)
                await db.execute(
                    """
                    UPDATE wiki_sources
                    SET status = 'deleting', safe_error_code = '', updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (updated_at, source_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return await self.get_source(source_id)

    async def purge_due_source_files(
        self,
        *,
        retention_ms: int,
        limit: int = 100,
        now_ms: int | None = None,
    ) -> tuple[str, ...]:
        """Physically remove due Raw trees while retaining canonical rows."""
        if retention_ms < 0 or not 1 <= limit <= 1000:
            raise WikiStoreError("invalid_configuration")
        cutoff = (self._clock_ms() if now_ms is None else now_ms) - retention_ms
        async with self._write_lock:
            db = self._require_db()
            async with db.execute(
                """
                SELECT * FROM wiki_sources
                WHERE status = 'deleting' AND updated_at_ms <= ?
                ORDER BY updated_at_ms, id
                LIMIT ?
                """,
                (cutoff, limit),
            ) as cursor:
                rows = await cursor.fetchall()
            removed: list[str] = []
            for row in rows:
                source = _row_to_source(row)
                async with db.execute(
                    """
                    SELECT 1
                    FROM wiki_page_sources AS ps
                    JOIN wiki_pages AS p ON p.id = ps.page_id
                    WHERE ps.source_id = ? AND p.status = 'active'
                    LIMIT 1
                    """,
                    (source.id,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        continue
                did_remove = await asyncio.to_thread(
                    self._file_store.purge_source_bundle,
                    source,
                )
                if did_remove:
                    removed.append(source.id)
        return tuple(removed)

    async def create_job(
        self,
        space_id: str,
        *,
        kind: WikiJobKind,
        source_id: str | None = None,
        attempt: int = 1,
        requested_mode: WikiParseMode = "builtin",
    ) -> WikiJob:
        now = self._clock_ms()
        source: WikiSource | None = None
        if source_id is not None:
            source = await self.get_source(source_id, space_id=space_id)
            if source.status not in {"uploaded", "failed", "parsed"}:
                raise WikiStoreError("job_conflict")
        try:
            job = WikiJob(
                id=self._job_id_factory(),
                space_id=space_id,
                source_id=source_id,
                kind=kind,
                attempt=attempt,
                requested_mode=requested_mode,
                base_selection_version=(0 if source is None else source.selection_version),
                base_selected_parse_revision_id=(
                    None if source is None else source.selected_parse_revision_id
                ),
                created_at_ms=now,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_job") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._assert_space_active(db, space_id)
                await db.execute(
                    """
                    INSERT INTO wiki_jobs (
                        id, space_id, source_id, kind, status, attempt,
                        requested_mode, base_selection_version,
                        base_selected_parse_revision_id, safe_error_code, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        job.space_id,
                        job.source_id,
                        job.kind,
                        job.status,
                        job.attempt,
                        job.requested_mode,
                        job.base_selection_version,
                        job.base_selected_parse_revision_id,
                        job.safe_error_code,
                        job.created_at_ms,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("job_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return job

    async def begin_parse_job(
        self,
        source_id: str,
        *,
        requested_mode: WikiParseMode = "builtin",
    ) -> tuple[WikiSource, WikiJob]:
        """Atomically claim an uploaded/failed source and enqueue exactly one parse."""
        try:
            validate_source_id(source_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (source_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("source_not_found")
                source = _row_to_source(row)
                await self._assert_space_active(db, source.space_id)
                if source.status not in {"uploaded", "failed", "parsed"}:
                    raise WikiStoreError("source_conflict")
                now = max(self._clock_ms(), source.updated_at_ms + 1)
                async with db.execute(
                    "SELECT COALESCE(MAX(attempt), 0) FROM wiki_jobs WHERE source_id = ?",
                    (source.id,),
                ) as cursor:
                    attempt_row = await cursor.fetchone()
                attempt = 1 if attempt_row is None else int(attempt_row[0]) + 1
                try:
                    job = WikiJob(
                        id=self._job_id_factory(),
                        space_id=source.space_id,
                        source_id=source.id,
                        kind="parse",
                        attempt=attempt,
                        requested_mode=requested_mode,
                        base_selection_version=source.selection_version,
                        base_selected_parse_revision_id=source.selected_parse_revision_id,
                        created_at_ms=now,
                    )
                    parsing = WikiSource.model_validate(
                        source.model_copy(
                            update={
                                "status": "parsing",
                                "safe_error_code": "",
                                "updated_at_ms": now,
                            }
                        ).model_dump()
                    )
                except ValueError as exc:
                    raise WikiStoreError("invalid_job") from exc
                await db.execute(
                    """
                    INSERT INTO wiki_jobs (
                        id, space_id, source_id, kind, status, attempt,
                        requested_mode, base_selection_version,
                        base_selected_parse_revision_id, safe_error_code, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        job.id,
                        job.space_id,
                        job.source_id,
                        job.kind,
                        job.status,
                        job.attempt,
                        job.requested_mode,
                        job.base_selection_version,
                        job.base_selected_parse_revision_id,
                        job.created_at_ms,
                    ),
                )
                await db.execute(
                    """
                    UPDATE wiki_sources
                    SET status = 'parsing', safe_error_code = '', updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (parsing.updated_at_ms, source.id),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("job_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return parsing, job

    async def get_job(self, job_id: str) -> WikiJob:
        try:
            validate_job_id(job_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute("SELECT * FROM wiki_jobs WHERE id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("job_not_found")
        return _row_to_job(row)

    async def list_parse_jobs(self, source_id: str) -> tuple[WikiJob, ...]:
        await self.get_source(source_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_jobs
            WHERE source_id = ? AND kind = 'parse'
            ORDER BY attempt, created_at_ms, id
            """,
            (source_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_job(row) for row in rows)

    async def set_job_status(
        self,
        job_id: str,
        status: WikiJobStatus,
        *,
        safe_error_code: str = "",
    ) -> WikiJob:
        if status not in _JOB_STATUS_TRANSITIONS:
            raise WikiStoreError("invalid_job")
        if (status in {"failed", "cancelled"}) != bool(safe_error_code):
            raise WikiStoreError("invalid_job")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_jobs WHERE id = ?",
                    (job_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("job_not_found")
                current = _row_to_job(row)
                if status == current.status:
                    await db.execute("COMMIT")
                    return current
                if status not in _JOB_STATUS_TRANSITIONS[current.status]:
                    raise WikiStoreError("invalid_status_transition")
                now = max(self._clock_ms(), current.created_at_ms)
                started_at = now if status == "running" else current.started_at_ms
                finished_at = now if status in {"succeeded", "failed", "cancelled"} else None
                await db.execute(
                    """
                    UPDATE wiki_jobs
                    SET status = ?, safe_error_code = ?, started_at_ms = ?,
                        finished_at_ms = ?
                    WHERE id = ?
                    """,
                    (status, safe_error_code, started_at, finished_at, job_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return await self.get_job(job_id)

    def new_parse_revision_id(self) -> str:
        try:
            return validate_parse_revision_id(self._parse_revision_id_factory())
        except ValueError as exc:
            raise WikiStoreError("invalid_artifact") from exc

    def new_parse_attempt(
        self,
        source: WikiSource,
        job: WikiJob,
        *,
        provider_attempt_id: str,
        ordinal: int,
        parser: str,
        parser_version: str,
        preset: str,
        route_reasons: Sequence[str],
        state: WikiParseAttemptState,
        safe_error_code: str = "",
        quality_report_json: str = "{}",
        output_size_bytes: int | None = None,
        output_sha256: str | None = None,
        fallback_from_attempt_id: str | None = None,
        routing_config_revision: str = "",
        routing_config_sha256: str = "",
        started_at_ms: int | None = None,
        finished_at_ms: int | None = None,
    ) -> WikiParseAttempt:
        if job.source_id != source.id:
            raise WikiStoreError("invalid_job")
        try:
            return WikiParseAttempt(
                id=self._parse_attempt_id_factory(),
                provider_attempt_id=provider_attempt_id,
                source_id=source.id,
                job_id=job.id,
                ordinal=ordinal,
                requested_mode=job.requested_mode,
                source_sha256=source.source_sha256,
                parser=parser,
                parser_version=parser_version,
                preset=preset,
                routing_config_revision=routing_config_revision,
                routing_config_sha256=routing_config_sha256,
                route_reasons_json=json.dumps(
                    list(route_reasons),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                state=state,
                safe_error_code=safe_error_code,
                quality_report_json=quality_report_json,
                output_size_bytes=output_size_bytes,
                output_sha256=output_sha256,
                fallback_from_attempt_id=fallback_from_attempt_id,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_artifact") from exc

    def new_parse_revision(
        self,
        source: WikiSource,
        job: WikiJob,
        selected_attempt: WikiParseAttempt,
        *,
        revision_id: str,
        contract_version: int,
        artifact_schema: str,
        parsed_markdown_relpath: str,
        parsed_markdown_sha256: str,
        manifest_relpath: str,
        manifest_sha256: str,
        page_count: int,
    ) -> WikiParseRevision:
        if (
            selected_attempt.source_id != source.id
            or selected_attempt.job_id != job.id
            or selected_attempt.state != "succeeded"
        ):
            raise WikiStoreError("invalid_artifact")
        try:
            return WikiParseRevision(
                id=revision_id,
                source_id=source.id,
                job_id=job.id,
                selected_attempt_id=selected_attempt.id,
                contract_version=contract_version,
                artifact_schema=artifact_schema,
                requested_mode=job.requested_mode,
                source_sha256=source.source_sha256,
                parser=selected_attempt.parser,
                parser_version=selected_attempt.parser_version,
                preset=selected_attempt.preset,
                routing_config_revision=selected_attempt.routing_config_revision,
                routing_config_sha256=selected_attempt.routing_config_sha256,
                parsed_markdown_relpath=parsed_markdown_relpath,
                parsed_markdown_sha256=parsed_markdown_sha256,
                manifest_relpath=manifest_relpath,
                manifest_sha256=manifest_sha256,
                page_count=page_count,
                created_at_ms=max(self._clock_ms(), selected_attempt.finished_at_ms or 0),
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_artifact") from exc

    def new_artifact(
        self,
        source_id: str,
        *,
        parse_revision_id: str,
        kind: WikiArtifactKind,
        relpath: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        source_locator_json: str = "{}",
    ) -> WikiArtifact:
        try:
            return WikiArtifact(
                id=self._artifact_id_factory(),
                source_id=source_id,
                parse_revision_id=parse_revision_id,
                kind=kind,
                relpath=relpath,
                mime_type=mime_type,
                size_bytes=size_bytes,
                sha256=sha256,
                source_locator_json=source_locator_json,
                created_at_ms=self._clock_ms(),
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_artifact") from exc

    async def complete_source_parse(
        self,
        source_id: str,
        *,
        job_id: str,
        attempts: Sequence[WikiParseAttempt],
        revision: WikiParseRevision,
        artifacts: Sequence[WikiArtifact],
    ) -> WikiSource:
        source = await self.get_source(source_id)
        job = await self.get_job(job_id)
        if (
            source.status != "parsing"
            or job.status != "running"
            or job.source_id != source.id
            or not attempts
            or not artifacts
        ):
            raise WikiStoreError("source_conflict")
        self._validate_parse_commit(source, job, attempts, revision, artifacts)
        for artifact in artifacts:
            content = await asyncio.to_thread(
                self._file_store.read_owned_file,
                source.space_id,
                artifact.relpath,
                max_bytes=artifact.size_bytes,
            )
            if (
                len(content) != artifact.size_bytes
                or hashlib.sha256(content).hexdigest() != artifact.sha256
            ):
                raise WikiStoreError("invalid_artifact")
        selected_at = max(self._clock_ms(), source.updated_at_ms + 1)
        try:
            parsed = WikiSource.model_validate(
                source.model_copy(
                    update={
                        "selected_parse_revision_id": revision.id,
                        "selection_version": source.selection_version + 1,
                        "selected_at_ms": selected_at,
                        "status": "parsed",
                        "safe_error_code": "",
                        "updated_at_ms": selected_at,
                    }
                ).model_dump()
            )
            pointer = self._selected_pointer(parsed, revision)
        except ValueError as exc:
            raise WikiStoreError("invalid_source") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                await self._assert_parse_commit_cas(db, source, job)
                for attempt in attempts:
                    await self._insert_parse_attempt(db, attempt)
                await self._insert_parse_revision(db, revision)
                for artifact in artifacts:
                    await self._insert_artifact(db, artifact)
                await db.execute(
                    """
                    UPDATE wiki_sources
                    SET selected_parse_revision_id = ?, selection_version = ?,
                        selected_at_ms = ?, status = 'parsed', safe_error_code = '',
                        updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (
                        revision.id,
                        parsed.selection_version,
                        selected_at,
                        selected_at,
                        source.id,
                    ),
                )
                await db.execute(
                    """
                    UPDATE wiki_jobs
                    SET status = 'succeeded', safe_error_code = '', finished_at_ms = ?
                    WHERE id = ?
                    """,
                    (max(selected_at, job.started_at_ms or 0), job.id),
                )
                await asyncio.to_thread(
                    self._file_store.write_selected_parse_pointer,
                    source.space_id,
                    parsed,
                    revision,
                    pointer,
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                await self._repair_selected_pointer_quietly(source)
                raise WikiStoreError("source_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                await self._repair_selected_pointer_quietly(source)
                raise
        return parsed

    async def list_artifacts(
        self,
        source_id: str,
        *,
        parse_revision_id: str | None = None,
    ) -> tuple[WikiArtifact, ...]:
        source = await self.get_source(source_id)
        selected = parse_revision_id or source.selected_parse_revision_id
        if selected is None:
            return ()
        revision = await self.get_parse_revision(selected)
        if revision.source_id != source.id:
            raise WikiStoreError("source_conflict")
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_artifacts
            WHERE source_id = ? AND parse_revision_id = ?
            ORDER BY relpath, id
            """,
            (source_id, selected),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_artifact(row) for row in rows)

    def new_source_summary(
        self,
        source: WikiSource,
        revision: WikiParseRevision,
        job: WikiJob,
        *,
        prompt_revision: str,
        provider: str,
        model: str,
        content: WikiSourceSummaryContent,
    ) -> WikiSourceSummaryDraft:
        """Build an immutable draft pinned to the job's selected Raw revision."""
        if (
            source.status != "parsed"
            or source.selected_parse_revision_id != revision.id
            or source.selection_version < 1
            or revision.source_id != source.id
            or revision.source_sha256 != source.source_sha256
            or job.kind != "summarize_source"
            or job.status != "running"
            or job.source_id != source.id
            or job.base_selection_version != source.selection_version
            or job.base_selected_parse_revision_id != revision.id
        ):
            raise WikiStoreError("source_conflict")
        content_json = json.dumps(
            content.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            return WikiSourceSummaryDraft(
                id=self._summary_id_factory(),
                space_id=source.space_id,
                source_id=source.id,
                parse_revision_id=revision.id,
                job_id=job.id,
                selection_version=source.selection_version,
                source_sha256=source.source_sha256,
                parsed_markdown_sha256=revision.parsed_markdown_sha256,
                manifest_sha256=revision.manifest_sha256,
                page_count=revision.page_count,
                prompt_revision=prompt_revision,
                provider=provider,
                model=model,
                content=content,
                content_sha256=hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
                created_at_ms=max(self._clock_ms(), job.started_at_ms or 0),
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_summary") from exc

    async def complete_source_summary(
        self,
        summary: WikiSourceSummaryDraft,
    ) -> WikiSourceSummaryDraft:
        """Publish only the draft row; never mutate Raw artifacts or Wiki pages."""
        try:
            validate_summary_id(summary.id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        content_json = json.dumps(
            summary.content.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if hashlib.sha256(content_json.encode("utf-8")).hexdigest() != summary.content_sha256:
            raise WikiStoreError("invalid_summary")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (summary.source_id,),
                ) as cursor:
                    source_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_jobs WHERE id = ?",
                    (summary.job_id,),
                ) as cursor:
                    job_row = await cursor.fetchone()
                if source_row is None or job_row is None:
                    raise WikiStoreError("source_conflict")
                source = _row_to_source(source_row)
                job = _row_to_job(job_row)
                if (
                    source.space_id != summary.space_id
                    or source.status != "parsed"
                    or source.source_sha256 != summary.source_sha256
                    or source.selection_version != summary.selection_version
                    or source.selected_parse_revision_id != summary.parse_revision_id
                    or job.kind != "summarize_source"
                    or job.status != "running"
                    or job.source_id != source.id
                    or job.base_selection_version != summary.selection_version
                    or job.base_selected_parse_revision_id != summary.parse_revision_id
                ):
                    raise WikiStoreError("source_conflict")
                await db.execute(
                    """
                    INSERT INTO wiki_source_summaries (
                        id, space_id, source_id, parse_revision_id, job_id,
                        selection_version, source_sha256, parsed_markdown_sha256,
                        manifest_sha256, page_count, prompt_revision, provider,
                        model, content_json, content_sha256, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary.id,
                        summary.space_id,
                        summary.source_id,
                        summary.parse_revision_id,
                        summary.job_id,
                        summary.selection_version,
                        summary.source_sha256,
                        summary.parsed_markdown_sha256,
                        summary.manifest_sha256,
                        summary.page_count,
                        summary.prompt_revision,
                        summary.provider,
                        summary.model,
                        content_json,
                        summary.content_sha256,
                        summary.created_at_ms,
                    ),
                )
                finished_at = max(self._clock_ms(), job.started_at_ms or 0)
                await db.execute(
                    """
                    UPDATE wiki_jobs
                    SET status = 'succeeded', safe_error_code = '', finished_at_ms = ?
                    WHERE id = ?
                    """,
                    (finished_at, job.id),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("source_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return await self.get_source_summary(summary.id)

    async def get_source_summary(self, summary_id: str) -> WikiSourceSummaryDraft:
        try:
            validate_summary_id(summary_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_source_summaries WHERE id = ?",
            (summary_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("summary_not_found")
        return _row_to_source_summary(row)

    async def list_source_summaries(
        self,
        source_id: str,
    ) -> tuple[WikiSourceSummaryDraft, ...]:
        await self.get_source(source_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_source_summaries
            WHERE source_id = ? ORDER BY created_at_ms, id
            """,
            (source_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_source_summary(row) for row in rows)

    def new_entry_page_proposal(
        self,
        summary: WikiSourceSummaryDraft,
        job: WikiJob,
        *,
        title: str,
        slug: str,
        aliases: tuple[str, ...],
        markdown: str,
        source_locator_json: str,
    ) -> WikiPageProposal:
        if (
            job.kind != "synthesize_entry_page"
            or job.status != "running"
            or job.source_id != summary.source_id
            or job.space_id != summary.space_id
            or job.base_selection_version != summary.selection_version
            or job.base_selected_parse_revision_id != summary.parse_revision_id
        ):
            raise WikiStoreError("source_conflict")
        try:
            return WikiPageProposal(
                id=self._page_proposal_id_factory(),
                space_id=summary.space_id,
                source_id=summary.source_id,
                summary_id=summary.id,
                job_id=job.id,
                kind="entry",
                title=title,
                slug=slug,
                aliases=aliases,
                markdown=markdown,
                content_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                source_locator_json=source_locator_json,
                created_at_ms=max(self._clock_ms(), job.started_at_ms or 0),
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_page_proposal") from exc

    async def complete_entry_page_proposal(
        self,
        summary: WikiSourceSummaryDraft,
        proposal: WikiPageProposal,
    ) -> WikiPageProposal:
        if (
            proposal.kind != "entry"
            or proposal.summary_id != summary.id
            or proposal.source_id != summary.source_id
            or proposal.space_id != summary.space_id
            or hashlib.sha256(proposal.markdown.encode("utf-8")).hexdigest()
            != proposal.content_sha256
        ):
            raise WikiStoreError("invalid_page_proposal")
        aliases_json = json.dumps(
            list(proposal.aliases),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (summary.source_id,),
                ) as cursor:
                    source_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_source_summaries WHERE id = ?",
                    (summary.id,),
                ) as cursor:
                    summary_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_jobs WHERE id = ?",
                    (proposal.job_id,),
                ) as cursor:
                    job_row = await cursor.fetchone()
                if source_row is None or summary_row is None or job_row is None:
                    raise WikiStoreError("source_conflict")
                source = _row_to_source(source_row)
                persisted_summary = _row_to_source_summary(summary_row)
                job = _row_to_job(job_row)
                if (
                    persisted_summary != summary
                    or source.status != "parsed"
                    or source.space_id != summary.space_id
                    or source.source_sha256 != summary.source_sha256
                    or source.selection_version != summary.selection_version
                    or source.selected_parse_revision_id != summary.parse_revision_id
                    or job.kind != "synthesize_entry_page"
                    or job.status != "running"
                    or job.source_id != summary.source_id
                    or job.base_selection_version != summary.selection_version
                    or job.base_selected_parse_revision_id != summary.parse_revision_id
                ):
                    raise WikiStoreError("source_conflict")
                await db.execute(
                    """
                    INSERT INTO wiki_page_proposals (
                        id, space_id, source_id, summary_id, job_id, kind,
                        topic_ordinal, parent_proposal_id, title, slug, aliases_json,
                        markdown, content_sha256, source_locator_json, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'entry', NULL, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.id,
                        proposal.space_id,
                        proposal.source_id,
                        proposal.summary_id,
                        proposal.job_id,
                        proposal.title,
                        proposal.slug,
                        aliases_json,
                        proposal.markdown,
                        proposal.content_sha256,
                        proposal.source_locator_json,
                        proposal.created_at_ms,
                    ),
                )
                finished_at = max(self._clock_ms(), job.started_at_ms or 0)
                await db.execute(
                    """
                    UPDATE wiki_jobs
                    SET status = 'succeeded', safe_error_code = '', finished_at_ms = ?
                    WHERE id = ?
                    """,
                    (finished_at, job.id),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("page_proposal_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return await self.get_page_proposal(proposal.id)

    def new_topic_page_proposal(
        self,
        summary: WikiSourceSummaryDraft,
        entry: WikiPageProposal,
        job: WikiJob,
        *,
        topic_ordinal: int,
        title: str,
        slug: str,
        markdown: str,
        source_locator_json: str,
    ) -> WikiPageProposal:
        if (
            entry.kind != "entry"
            or entry.summary_id != summary.id
            or job.kind != "synthesize_topic_pages"
            or job.status != "running"
            or job.source_id != summary.source_id
            or job.space_id != summary.space_id
            or job.base_selection_version != summary.selection_version
            or job.base_selected_parse_revision_id != summary.parse_revision_id
        ):
            raise WikiStoreError("source_conflict")
        try:
            return WikiPageProposal(
                id=self._page_proposal_id_factory(),
                space_id=summary.space_id,
                source_id=summary.source_id,
                summary_id=summary.id,
                job_id=job.id,
                kind="topic",
                topic_ordinal=topic_ordinal,
                parent_proposal_id=entry.id,
                title=title,
                slug=slug,
                markdown=markdown,
                content_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                source_locator_json=source_locator_json,
                created_at_ms=max(self._clock_ms(), job.started_at_ms or 0),
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_page_proposal") from exc

    async def complete_topic_page_proposals(
        self,
        summary: WikiSourceSummaryDraft,
        entry: WikiPageProposal,
        proposals: Sequence[WikiPageProposal],
    ) -> tuple[WikiPageProposal, ...]:
        if not proposals or len(proposals) != len(summary.content.topics):
            raise WikiStoreError("invalid_page_proposal")
        if [item.topic_ordinal for item in proposals] != list(range(len(proposals))):
            raise WikiStoreError("invalid_page_proposal")
        job_id = proposals[0].job_id
        if any(
            item.kind != "topic"
            or item.summary_id != summary.id
            or item.source_id != summary.source_id
            or item.space_id != summary.space_id
            or item.parent_proposal_id != entry.id
            or item.job_id != job_id
            or hashlib.sha256(item.markdown.encode("utf-8")).hexdigest() != item.content_sha256
            for item in proposals
        ):
            raise WikiStoreError("invalid_page_proposal")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (summary.source_id,),
                ) as cursor:
                    source_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_source_summaries WHERE id = ?",
                    (summary.id,),
                ) as cursor:
                    summary_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_page_proposals WHERE id = ?",
                    (entry.id,),
                ) as cursor:
                    entry_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_jobs WHERE id = ?",
                    (job_id,),
                ) as cursor:
                    job_row = await cursor.fetchone()
                if (
                    source_row is None
                    or summary_row is None
                    or entry_row is None
                    or job_row is None
                ):
                    raise WikiStoreError("source_conflict")
                source = _row_to_source(source_row)
                persisted_summary = _row_to_source_summary(summary_row)
                persisted_entry = _row_to_page_proposal(entry_row)
                job = _row_to_job(job_row)
                if (
                    persisted_summary != summary
                    or persisted_entry != entry
                    or source.status != "parsed"
                    or source.space_id != summary.space_id
                    or source.source_sha256 != summary.source_sha256
                    or source.selection_version != summary.selection_version
                    or source.selected_parse_revision_id != summary.parse_revision_id
                    or job.kind != "synthesize_topic_pages"
                    or job.status != "running"
                    or job.source_id != summary.source_id
                    or job.base_selection_version != summary.selection_version
                    or job.base_selected_parse_revision_id != summary.parse_revision_id
                ):
                    raise WikiStoreError("source_conflict")
                for proposal in proposals:
                    await db.execute(
                        """
                        INSERT INTO wiki_page_proposals (
                            id, space_id, source_id, summary_id, job_id, kind,
                            topic_ordinal, parent_proposal_id, title, slug,
                            aliases_json, markdown, content_sha256,
                            source_locator_json, created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, 'topic', ?, ?, ?, ?, '[]', ?, ?, ?, ?)
                        """,
                        (
                            proposal.id,
                            proposal.space_id,
                            proposal.source_id,
                            proposal.summary_id,
                            proposal.job_id,
                            proposal.topic_ordinal,
                            proposal.parent_proposal_id,
                            proposal.title,
                            proposal.slug,
                            proposal.markdown,
                            proposal.content_sha256,
                            proposal.source_locator_json,
                            proposal.created_at_ms,
                        ),
                    )
                finished_at = max(self._clock_ms(), job.started_at_ms or 0)
                await db.execute(
                    """
                    UPDATE wiki_jobs
                    SET status = 'succeeded', safe_error_code = '', finished_at_ms = ?
                    WHERE id = ?
                    """,
                    (finished_at, job.id),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("page_proposal_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return await self.list_topic_page_proposals(summary.id)

    async def get_page_proposal(self, proposal_id: str) -> WikiPageProposal:
        try:
            validate_page_proposal_id(proposal_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_page_proposals WHERE id = ?",
            (proposal_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("page_proposal_not_found")
        return _row_to_page_proposal(row)

    async def find_entry_page_proposal(
        self,
        summary_id: str,
    ) -> WikiPageProposal | None:
        try:
            validate_summary_id(summary_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_page_proposals
            WHERE summary_id = ? AND kind = 'entry'
            """,
            (summary_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _row_to_page_proposal(row)

    async def list_topic_page_proposals(
        self,
        summary_id: str,
    ) -> tuple[WikiPageProposal, ...]:
        try:
            validate_summary_id(summary_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_page_proposals
            WHERE summary_id = ? AND kind = 'topic'
            ORDER BY topic_ordinal, id
            """,
            (summary_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_page_proposal(row) for row in rows)

    async def list_page_proposals(
        self,
        source_id: str,
    ) -> tuple[WikiPageProposal, ...]:
        await self.get_source(source_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_page_proposals
            WHERE source_id = ?
            ORDER BY CASE kind WHEN 'entry' THEN 0 ELSE 1 END,
                     topic_ordinal, created_at_ms, id
            """,
            (source_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_page_proposal(row) for row in rows)

    async def create_conversation(
        self,
        space_id: str,
        *,
        session_id: str,
        title: str,
    ) -> WikiConversation:
        """Bind a server-created durable Session to exactly one Wiki Space."""
        space = await self.get_space(space_id)
        now = self._clock_ms()
        try:
            conversation = WikiConversation(
                id=self._conversation_id_factory(),
                space_id=space.id,
                session_id=session_id,
                title=title.strip(),
                status="active",
                created_at_ms=now,
                updated_at_ms=now,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_conversation") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT status FROM wiki_spaces WHERE id = ?",
                    (space.id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("space_not_found")
                if row["status"] != "active":
                    raise WikiStoreError("space_read_only")
                await db.execute(
                    """
                    INSERT INTO wiki_conversations (
                        id, space_id, session_id, title, status,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        conversation.id,
                        conversation.space_id,
                        conversation.session_id,
                        conversation.title,
                        conversation.created_at_ms,
                        conversation.updated_at_ms,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("conversation_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return conversation

    async def get_conversation(self, conversation_id: str) -> WikiConversation:
        try:
            validate_conversation_id(conversation_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_conversations WHERE id = ?",
            (conversation_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("conversation_not_found")
        return _row_to_conversation(row)

    async def find_conversation_by_session(
        self,
        session_id: str,
    ) -> WikiConversation | None:
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 128
            or session_id != session_id.strip()
            or any(character in session_id for character in ("\x00", "\r", "\n"))
        ):
            raise WikiStoreError("invalid_identifier")
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_conversations WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _row_to_conversation(row)

    async def list_conversations(
        self,
        space_id: str,
        *,
        statuses: Sequence[WikiConversationStatus] = ("active", "archived"),
    ) -> tuple[WikiConversation, ...]:
        await self.get_space(space_id)
        if not statuses or any(status not in {"active", "archived"} for status in statuses):
            raise WikiStoreError("invalid_conversation")
        unique_statuses = tuple(dict.fromkeys(statuses))
        placeholders = ",".join("?" for _ in unique_statuses)
        db = self._require_db()
        async with db.execute(
            f"""
            SELECT * FROM wiki_conversations
            WHERE space_id = ? AND status IN ({placeholders})
            ORDER BY updated_at_ms DESC, id
            """,  # noqa: S608 - placeholders are generated, never user supplied
            (space_id, *unique_statuses),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_conversation(row) for row in rows)

    async def set_conversation_status(
        self,
        conversation_id: str,
        status: WikiConversationStatus,
    ) -> WikiConversation:
        conversation = await self.get_conversation(conversation_id)
        if status not in {"active", "archived"}:
            raise WikiStoreError("invalid_conversation")
        if conversation.status == status:
            return conversation
        now = max(self._clock_ms(), conversation.updated_at_ms + 1)
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                UPDATE wiki_conversations SET status = ?, updated_at_ms = ?
                WHERE id = ? AND status = ? AND updated_at_ms = ?
                """,
                (
                    status,
                    now,
                    conversation.id,
                    conversation.status,
                    conversation.updated_at_ms,
                ),
            )
            await db.commit()
        if cursor.rowcount != 1:
            raise WikiStoreError("conversation_conflict")
        return await self.get_conversation(conversation.id)

    async def on_session_deleted(self, session_id: str) -> None:
        conversation = await self.find_conversation_by_session(session_id)
        if conversation is not None and conversation.status == "active":
            await self.set_conversation_status(conversation.id, "archived")

    async def create_page_proposal_change_set(
        self,
        summary: WikiSourceSummaryDraft,
        proposals: Sequence[WikiPageProposal],
        item_specs: Sequence[tuple[WikiChangeSetOperationKind, str, str]],
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        """Atomically freeze ordered page-create payloads and user-visible diffs."""
        existing = await self.find_change_set_for_summary(summary.id)
        if existing is not None:
            return existing, await self.list_change_set_items(existing.id)
        if (
            not proposals
            or len(item_specs) != len(proposals) * 2 - 1
            or proposals[0].kind != "entry"
            or proposals[0].topic_ordinal is not None
            or any(
                proposal.summary_id != summary.id
                or proposal.source_id != summary.source_id
                or proposal.space_id != summary.space_id
                for proposal in proposals
            )
            or [item.topic_ordinal for item in proposals[1:]] != list(range(len(proposals) - 1))
        ):
            raise WikiStoreError("invalid_change_set")
        page_specs = item_specs[: len(proposals)]
        edge_specs = item_specs[len(proposals) :]
        for proposal, (operation_kind, payload_json, unified_diff) in zip(
            proposals,
            page_specs,
            strict=True,
        ):
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if (
                not isinstance(payload, dict)
                or operation_kind != "page_create"
                or payload.get("schema") != "llm-wiki-page-create/v1"
                or payload.get("proposal_id") != proposal.id
                or payload.get("source_id") != proposal.source_id
                or payload.get("summary_id") != proposal.summary_id
                or payload.get("slug") != proposal.slug
                or payload.get("title") != proposal.title
                or payload.get("markdown") != proposal.markdown
                or payload.get("content_sha256") != proposal.content_sha256
                or not unified_diff
            ):
                raise WikiStoreError("invalid_change_set")
        for topic, (operation_kind, payload_json, unified_diff) in zip(
            proposals[1:],
            edge_specs,
            strict=True,
        ):
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if (
                operation_kind != "edge_add"
                or not isinstance(payload, dict)
                or payload.get("schema") != "llm-wiki-edge-add/v1"
                or payload.get("from_proposal_id") != topic.id
                or payload.get("to_proposal_id") != proposals[0].id
                or payload.get("relation_type") != "part_of"
                or not unified_diff
            ):
                raise WikiStoreError("invalid_change_set")
        space = await self.get_space(summary.space_id)
        now = self._clock_ms()
        try:
            change_set = WikiChangeSet(
                id=self._change_set_id_factory(),
                space_id=summary.space_id,
                source_summary_id=summary.id,
                status="awaiting_approval",
                base_graph_revision=space.graph_revision,
                summary=(
                    f"Create one source entry page and {len(proposals) - 1} "
                    f"topic page(s), plus {len(edge_specs)} relation(s), "
                    f"from {summary.id}."
                ),
                created_at_ms=now,
            )
            items = tuple(
                WikiChangeSetItem(
                    id=self._change_set_item_id_factory(),
                    change_set_id=change_set.id,
                    ordinal=ordinal,
                    operation_kind=operation_kind,
                    payload_json=payload_json,
                    unified_diff=unified_diff,
                    created_at_ms=now,
                )
                for ordinal, (operation_kind, payload_json, unified_diff) in enumerate(item_specs)
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_change_set") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_change_sets WHERE source_summary_id = ?",
                    (summary.id,),
                ) as cursor:
                    existing_row = await cursor.fetchone()
                if existing_row is not None:
                    await db.execute("COMMIT")
                    persisted = _row_to_change_set(existing_row)
                    return persisted, await self.list_change_set_items(persisted.id)
                async with db.execute(
                    "SELECT * FROM wiki_sources WHERE id = ?",
                    (summary.source_id,),
                ) as cursor:
                    source_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_source_summaries WHERE id = ?",
                    (summary.id,),
                ) as cursor:
                    summary_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (summary.space_id,),
                ) as cursor:
                    space_row = await cursor.fetchone()
                if source_row is None or summary_row is None or space_row is None:
                    raise WikiStoreError("source_conflict")
                source = _row_to_source(source_row)
                persisted_summary = _row_to_source_summary(summary_row)
                persisted_space = _row_to_space(space_row)
                if (
                    persisted_summary != summary
                    or persisted_space != space
                    or persisted_space.status != "active"
                    or source.status != "parsed"
                    or source.source_sha256 != summary.source_sha256
                    or source.selection_version != summary.selection_version
                    or source.selected_parse_revision_id != summary.parse_revision_id
                ):
                    raise WikiStoreError("source_conflict")
                persisted_proposals: list[WikiPageProposal] = []
                for proposal in proposals:
                    async with db.execute(
                        "SELECT * FROM wiki_page_proposals WHERE id = ?",
                        (proposal.id,),
                    ) as cursor:
                        proposal_row = await cursor.fetchone()
                    if proposal_row is None:
                        raise WikiStoreError("page_proposal_conflict")
                    persisted_proposals.append(_row_to_page_proposal(proposal_row))
                    async with db.execute(
                        "SELECT 1 FROM wiki_pages WHERE space_id = ? AND slug = ?",
                        (summary.space_id, proposal.slug),
                    ) as cursor:
                        if await cursor.fetchone() is not None:
                            raise WikiStoreError("page_proposal_conflict")
                if tuple(persisted_proposals) != tuple(proposals):
                    raise WikiStoreError("page_proposal_conflict")
                await db.execute(
                    """
                    INSERT INTO wiki_change_sets (
                        id, space_id, conversation_id, source_summary_id, status,
                        base_graph_revision, summary, safe_error_code, created_at_ms,
                        decided_at_ms, published_at_ms
                    ) VALUES (?, ?, NULL, ?, 'awaiting_approval', ?, ?, '', ?, NULL, NULL)
                    """,
                    (
                        change_set.id,
                        change_set.space_id,
                        change_set.source_summary_id,
                        change_set.base_graph_revision,
                        change_set.summary,
                        change_set.created_at_ms,
                    ),
                )
                for item in items:
                    await db.execute(
                        """
                        INSERT INTO wiki_change_set_items (
                            id, change_set_id, ordinal, operation_kind, target_id,
                            base_version, before_sha256, payload_json, unified_diff,
                            created_at_ms
                        ) VALUES (?, ?, ?, ?, '', NULL, '', ?, ?, ?)
                        """,
                        (
                            item.id,
                            item.change_set_id,
                            item.ordinal,
                            item.operation_kind,
                            item.payload_json,
                            item.unified_diff,
                            item.created_at_ms,
                        ),
                    )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("change_set_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return change_set, items

    async def create_conversation_page_update_change_set(
        self,
        conversation: WikiConversation,
        page: WikiPage,
        revision: WikiPageRevision,
        *,
        title: str,
        aliases: tuple[str, ...],
        markdown: str,
        payload_json: str,
        unified_diff: str,
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        """Freeze one Knowledge Agent page patch without publishing it."""
        if (
            conversation.status != "active"
            or page.status != "active"
            or page.space_id != conversation.space_id
            or page.current_revision_id != revision.id
            or revision.page_id != page.id
            or not unified_diff
        ):
            raise WikiStoreError("invalid_change_set")
        try:
            payload = json.loads(payload_json)
            canonical_payload = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            content_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            proposed_page = WikiPage(
                id=page.id,
                space_id=page.space_id,
                slug=page.slug,
                title=title,
                aliases=aliases,
                status=page.status,
                current_revision_id=page.current_revision_id,
                version=page.version,
                created_at_ms=page.created_at_ms,
                updated_at_ms=page.updated_at_ms,
            )
            proposed_revision = WikiPageRevision(
                id=f"page_revision_{'0' * 24}",
                page_id=page.id,
                version=page.version + 1,
                title=title,
                markdown=markdown,
                content_sha256=content_sha256,
                change_set_id=f"change_set_{'0' * 24}",
                author_kind="agent",
                created_at_ms=max(self._clock_ms(), page.updated_at_ms),
            )
        except (TypeError, ValueError) as exc:
            raise WikiStoreError("invalid_change_set") from exc
        if (
            canonical_payload != payload_json
            or proposed_page.title != title
            or proposed_page.aliases != aliases
            or payload.get("schema") != "llm-wiki-page-update/v1"
            or payload.get("page_id") != page.id
            or payload.get("base_revision_id") != revision.id
            or payload.get("title") != title
            or tuple(payload.get("aliases", ())) != aliases
            or payload.get("markdown") != markdown
            or payload.get("content_sha256") != proposed_revision.content_sha256
            or unified_diff != _render_page_update_diff(page.slug, revision.markdown, markdown)
        ):
            raise WikiStoreError("invalid_change_set")
        space = await self.get_space(conversation.space_id)
        now = max(self._clock_ms(), page.updated_at_ms)
        try:
            change_set = WikiChangeSet(
                id=self._change_set_id_factory(),
                space_id=space.id,
                conversation_id=conversation.id,
                status="awaiting_approval",
                base_graph_revision=space.graph_revision,
                summary=f"Update Wiki page {page.title}.",
                created_at_ms=now,
            )
            item = WikiChangeSetItem(
                id=self._change_set_item_id_factory(),
                change_set_id=change_set.id,
                ordinal=0,
                operation_kind="page_update",
                target_id=page.id,
                base_version=page.version,
                before_sha256=revision.content_sha256,
                payload_json=payload_json,
                unified_diff=unified_diff,
                created_at_ms=now,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_change_set") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    """
                    SELECT c.* FROM wiki_change_sets AS c
                    JOIN wiki_change_set_items AS i ON i.change_set_id = c.id
                    WHERE c.conversation_id = ? AND c.status = 'awaiting_approval'
                      AND i.ordinal = 0 AND i.operation_kind = 'page_update'
                      AND i.target_id = ? AND i.base_version = ?
                      AND i.before_sha256 = ? AND i.payload_json = ?
                    ORDER BY c.created_at_ms, c.id LIMIT 1
                    """,
                    (
                        conversation.id,
                        page.id,
                        page.version,
                        revision.content_sha256,
                        payload_json,
                    ),
                ) as cursor:
                    existing_row = await cursor.fetchone()
                if existing_row is not None:
                    await db.execute("COMMIT")
                    existing = _row_to_change_set(existing_row)
                    return existing, await self.list_change_set_items(existing.id)
                async with db.execute(
                    "SELECT * FROM wiki_conversations WHERE id = ?",
                    (conversation.id,),
                ) as cursor:
                    conversation_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space.id,),
                ) as cursor:
                    space_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_pages WHERE id = ?",
                    (page.id,),
                ) as cursor:
                    page_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_page_revisions WHERE id = ?",
                    (revision.id,),
                ) as cursor:
                    revision_row = await cursor.fetchone()
                if (
                    conversation_row is None
                    or space_row is None
                    or page_row is None
                    or revision_row is None
                    or _row_to_conversation(conversation_row) != conversation
                    or _row_to_space(space_row) != space
                    or _row_to_page(page_row) != page
                    or _row_to_page_revision(revision_row) != revision
                    or space.status != "active"
                ):
                    raise WikiStoreError("change_set_conflict")
                await db.execute(
                    """
                    INSERT INTO wiki_change_sets (
                        id, space_id, conversation_id, source_summary_id, status,
                        base_graph_revision, summary, safe_error_code, created_at_ms,
                        decided_at_ms, published_at_ms
                    ) VALUES (?, ?, ?, NULL, 'awaiting_approval', ?, ?, '', ?, NULL, NULL)
                    """,
                    (
                        change_set.id,
                        change_set.space_id,
                        change_set.conversation_id,
                        change_set.base_graph_revision,
                        change_set.summary,
                        change_set.created_at_ms,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO wiki_change_set_items (
                        id, change_set_id, ordinal, operation_kind, target_id,
                        base_version, before_sha256, payload_json, unified_diff,
                        created_at_ms
                    ) VALUES (?, ?, 0, 'page_update', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        item.change_set_id,
                        item.target_id,
                        item.base_version,
                        item.before_sha256,
                        item.payload_json,
                        item.unified_diff,
                        item.created_at_ms,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("change_set_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return change_set, (item,)

    async def create_conversation_change_set(
        self,
        conversation: WikiConversation,
        *,
        summary: str,
        item_specs: Sequence[
            tuple[
                WikiChangeSetOperationKind,
                str,
                int | None,
                str,
                str,
                str,
            ]
        ],
    ) -> tuple[WikiChangeSet, tuple[WikiChangeSetItem, ...]]:
        """Freeze validated Knowledge Agent create/delete/edge operations."""
        if conversation.status != "active" or not item_specs or len(item_specs) > 100:
            raise WikiStoreError("invalid_change_set")
        fingerprint_payload = json.dumps(
            [list(spec) for spec in item_specs],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
        rendered_summary = f"{summary.strip()} [proposal {fingerprint[:12]}]"
        space = await self.get_space(conversation.space_id)
        now = max(self._clock_ms(), conversation.updated_at_ms)
        try:
            change_set = WikiChangeSet(
                id=self._change_set_id_factory(),
                space_id=space.id,
                conversation_id=conversation.id,
                status="awaiting_approval",
                base_graph_revision=space.graph_revision,
                summary=rendered_summary,
                created_at_ms=now,
            )
            items = tuple(
                WikiChangeSetItem(
                    id=self._change_set_item_id_factory(),
                    change_set_id=change_set.id,
                    ordinal=ordinal,
                    operation_kind=operation_kind,
                    target_id=target_id,
                    base_version=base_version,
                    before_sha256=before_sha256,
                    payload_json=payload_json,
                    unified_diff=unified_diff,
                    created_at_ms=now,
                )
                for ordinal, (
                    operation_kind,
                    target_id,
                    base_version,
                    before_sha256,
                    payload_json,
                    unified_diff,
                ) in enumerate(item_specs)
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_change_set") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    """
                    SELECT * FROM wiki_change_sets
                    WHERE conversation_id = ? AND status = 'awaiting_approval'
                      AND base_graph_revision = ? AND summary = ?
                    ORDER BY created_at_ms, id
                    """,
                    (conversation.id, space.graph_revision, rendered_summary),
                ) as cursor:
                    candidate_rows = await cursor.fetchall()
                for candidate_row in candidate_rows:
                    candidate = _row_to_change_set(candidate_row)
                    async with db.execute(
                        """
                        SELECT * FROM wiki_change_set_items
                        WHERE change_set_id = ? ORDER BY ordinal, id
                        """,
                        (candidate.id,),
                    ) as cursor:
                        candidate_item_rows = await cursor.fetchall()
                    candidate_items = tuple(
                        _row_to_change_set_item(row) for row in candidate_item_rows
                    )
                    candidate_specs = tuple(
                        (
                            item.operation_kind,
                            item.target_id,
                            item.base_version,
                            item.before_sha256,
                            item.payload_json,
                            item.unified_diff,
                        )
                        for item in candidate_items
                    )
                    if candidate_specs == tuple(item_specs):
                        await db.execute("COMMIT")
                        return candidate, candidate_items
                async with db.execute(
                    "SELECT * FROM wiki_conversations WHERE id = ?",
                    (conversation.id,),
                ) as cursor:
                    conversation_row = await cursor.fetchone()
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space.id,),
                ) as cursor:
                    space_row = await cursor.fetchone()
                if (
                    conversation_row is None
                    or space_row is None
                    or _row_to_conversation(conversation_row) != conversation
                    or _row_to_space(space_row) != space
                    or conversation.status != "active"
                    or space.status != "active"
                ):
                    raise WikiStoreError("change_set_conflict")
                await db.execute(
                    """
                    INSERT INTO wiki_change_sets (
                        id, space_id, conversation_id, source_summary_id, status,
                        base_graph_revision, summary, safe_error_code, created_at_ms,
                        decided_at_ms, published_at_ms
                    ) VALUES (?, ?, ?, NULL, 'awaiting_approval', ?, ?, '', ?, NULL, NULL)
                    """,
                    (
                        change_set.id,
                        change_set.space_id,
                        change_set.conversation_id,
                        change_set.base_graph_revision,
                        change_set.summary,
                        change_set.created_at_ms,
                    ),
                )
                for item in items:
                    await db.execute(
                        """
                        INSERT INTO wiki_change_set_items (
                            id, change_set_id, ordinal, operation_kind, target_id,
                            base_version, before_sha256, payload_json, unified_diff,
                            created_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.id,
                            item.change_set_id,
                            item.ordinal,
                            item.operation_kind,
                            item.target_id,
                            item.base_version,
                            item.before_sha256,
                            item.payload_json,
                            item.unified_diff,
                            item.created_at_ms,
                        ),
                    )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("change_set_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return change_set, items

    async def get_change_set(self, change_set_id: str) -> WikiChangeSet:
        try:
            validate_change_set_id(change_set_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_change_sets WHERE id = ?",
            (change_set_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("change_set_not_found")
        return _row_to_change_set(row)

    async def decide_change_set(
        self,
        change_set_id: str,
        *,
        approve: bool,
    ) -> tuple[WikiChangeSet, tuple[WikiPage, ...]]:
        """Reject or atomically publish every page-create item in one transaction."""
        try:
            validate_change_set_id(change_set_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        published_pages: tuple[WikiPage, ...] = ()
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_change_sets WHERE id = ?",
                    (change_set_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("change_set_not_found")
                change_set = _row_to_change_set(row)
                if change_set.status == "approved":
                    if not approve:
                        raise WikiStoreError("change_set_conflict")
                    await db.execute("COMMIT")
                    published_pages = await self._pages_for_change_set(change_set.id)
                elif change_set.status == "rejected":
                    if approve:
                        raise WikiStoreError("change_set_conflict")
                    await db.execute("COMMIT")
                elif change_set.status != "awaiting_approval":
                    raise WikiStoreError("change_set_conflict")
                elif not approve:
                    decided_at = max(self._clock_ms(), change_set.created_at_ms)
                    await db.execute(
                        """
                        UPDATE wiki_change_sets
                        SET status = 'rejected', decided_at_ms = ?
                        WHERE id = ?
                        """,
                        (decided_at, change_set.id),
                    )
                    await db.execute("COMMIT")
                else:
                    outcome = await self._approve_change_set_transaction(
                        db,
                        change_set,
                    )
                    if outcome is None:
                        await db.execute("COMMIT")
                    else:
                        published_pages = outcome
                        await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("change_set_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        decided = await self.get_change_set(change_set_id)
        if decided.status == "approved":
            if not published_pages:
                published_pages = await self._pages_for_change_set(decided.id)
            await self._sync_page_mirrors(published_pages)
            await self._sync_deleted_page_mirrors(decided.id)
            await self._sync_mirror_or_raise(await self.get_space(decided.space_id))
        return decided, published_pages

    async def _approve_change_set_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        if change_set.source_summary_id is not None and change_set.conversation_id is None:
            return await self._approve_source_change_set_transaction(db, change_set)
        if change_set.conversation_id is not None and change_set.source_summary_id is None:
            return await self._approve_conversation_change_set_transaction(
                db,
                change_set,
            )
        raise WikiStoreError("invalid_change_set")

    async def _approve_source_change_set_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        if change_set.source_summary_id is None:
            raise WikiStoreError("invalid_change_set")
        async with db.execute(
            "SELECT * FROM wiki_spaces WHERE id = ?",
            (change_set.space_id,),
        ) as cursor:
            space_row = await cursor.fetchone()
        async with db.execute(
            "SELECT * FROM wiki_source_summaries WHERE id = ?",
            (change_set.source_summary_id,),
        ) as cursor:
            summary_row = await cursor.fetchone()
        if space_row is None or summary_row is None:
            await self._mark_change_set_stale(db, change_set)
            return None
        space = _row_to_space(space_row)
        summary = _row_to_source_summary(summary_row)
        async with db.execute(
            "SELECT * FROM wiki_sources WHERE id = ?",
            (summary.source_id,),
        ) as cursor:
            source_row = await cursor.fetchone()
        if source_row is None:
            await self._mark_change_set_stale(db, change_set)
            return None
        source = _row_to_source(source_row)
        if (
            space.status != "active"
            or space.graph_revision != change_set.base_graph_revision
            or source.status != "parsed"
            or source.space_id != change_set.space_id
            or source.source_sha256 != summary.source_sha256
            or source.selection_version != summary.selection_version
            or source.selected_parse_revision_id != summary.parse_revision_id
        ):
            await self._mark_change_set_stale(db, change_set)
            return None
        async with db.execute(
            """
            SELECT * FROM wiki_change_set_items
            WHERE change_set_id = ? ORDER BY ordinal, id
            """,
            (change_set.id,),
        ) as cursor:
            item_rows = await cursor.fetchall()
        items = tuple(_row_to_change_set_item(row) for row in item_rows)
        if not items or [item.ordinal for item in items] != list(range(len(items))):
            raise WikiStoreError("invalid_change_set")
        page_items = tuple(item for item in items if item.operation_kind == "page_create")
        edge_items = tuple(item for item in items if item.operation_kind == "edge_add")
        if len(page_items) + len(edge_items) != len(items) or not page_items:
            raise WikiStoreError("invalid_change_set")
        now = max(self._clock_ms(), change_set.created_at_ms)
        pages: list[WikiPage] = []
        revisions: list[WikiPageRevision] = []
        locators: list[str] = []
        proposal_to_page: dict[str, WikiPage] = {}
        proposal_by_id: dict[str, WikiPageProposal] = {}
        for item in page_items:
            try:
                payload = json.loads(item.payload_json)
                canonical_payload = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                proposal_id = payload["proposal_id"]
                aliases = tuple(payload["aliases"])
                locator_json = json.dumps(
                    payload["source_locator"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if canonical_payload != item.payload_json:
                raise WikiStoreError("invalid_change_set")
            async with db.execute(
                "SELECT * FROM wiki_page_proposals WHERE id = ?",
                (proposal_id,),
            ) as cursor:
                proposal_row = await cursor.fetchone()
            if proposal_row is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            proposal = _row_to_page_proposal(proposal_row)
            if (
                proposal.summary_id != summary.id
                or proposal.source_id != summary.source_id
                or payload.get("schema") != "llm-wiki-page-create/v1"
                or payload.get("source_id") != proposal.source_id
                or payload.get("summary_id") != proposal.summary_id
                or payload.get("slug") != proposal.slug
                or payload.get("title") != proposal.title
                or aliases != proposal.aliases
                or payload.get("markdown") != proposal.markdown
                or payload.get("content_sha256") != proposal.content_sha256
                or payload.get("source_locator") != json.loads(proposal.source_locator_json)
                or item.unified_diff != _render_page_create_diff(proposal.slug, proposal.markdown)
            ):
                raise WikiStoreError("invalid_change_set")
            async with db.execute(
                "SELECT 1 FROM wiki_pages WHERE space_id = ? AND slug = ?",
                (space.id, proposal.slug),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    await self._mark_change_set_stale(db, change_set)
                    return None
            try:
                page = WikiPage(
                    id=self._page_id_factory(),
                    space_id=space.id,
                    slug=proposal.slug,
                    title=proposal.title,
                    aliases=proposal.aliases,
                    status="active",
                    current_revision_id=self._page_revision_id_factory(),
                    version=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                revision = WikiPageRevision(
                    id=page.current_revision_id,
                    page_id=page.id,
                    version=1,
                    title=page.title,
                    markdown=proposal.markdown,
                    content_sha256=proposal.content_sha256,
                    change_set_id=change_set.id,
                    author_kind="agent",
                    created_at_ms=now,
                )
            except ValueError as exc:
                raise WikiStoreError("invalid_change_set") from exc
            pages.append(page)
            revisions.append(revision)
            locators.append(locator_json)
            proposal_to_page[proposal.id] = page
            proposal_by_id[proposal.id] = proposal
        edges: list[WikiEdge] = []
        edge_item_pairs: list[tuple[WikiChangeSetItem, WikiEdge]] = []
        async with db.execute(
            """
            SELECT from_page_id, to_page_id FROM wiki_edges
            WHERE space_id = ? AND relation_type = 'part_of'
            """,
            (space.id,),
        ) as cursor:
            part_of_rows = await cursor.fetchall()
        part_of_adjacency: dict[str, set[str]] = {}
        pending_edge_keys: set[tuple[str, str, WikiRelationType]] = set()
        for row in part_of_rows:
            part_of_adjacency.setdefault(row["from_page_id"], set()).add(row["to_page_id"])
        for item in edge_items:
            try:
                payload = json.loads(item.payload_json)
                canonical_payload = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                from_proposal_id = payload["from_proposal_id"]
                to_proposal_id = payload["to_proposal_id"]
                relation_type = cast("WikiRelationType", payload["relation_type"])
                from_page = proposal_to_page[from_proposal_id]
                to_page = proposal_to_page[to_proposal_id]
                from_proposal = proposal_by_id[from_proposal_id]
                to_proposal = proposal_by_id[to_proposal_id]
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if (
                canonical_payload != item.payload_json
                or payload.get("schema") != "llm-wiki-edge-add/v1"
                or relation_type
                not in {
                    "related_to",
                    "references",
                    "extends",
                    "contradicts",
                    "part_of",
                }
                or item.unified_diff
                != _render_edge_add_diff(
                    from_proposal.slug,
                    to_proposal.slug,
                    relation_type,
                )
            ):
                raise WikiStoreError("invalid_change_set")
            from_page_id = from_page.id
            to_page_id = to_page.id
            if relation_type == "related_to" and from_page_id > to_page_id:
                from_page_id, to_page_id = to_page_id, from_page_id
            if from_page_id == to_page_id:
                raise WikiStoreError("invalid_change_set")
            edge_key = (from_page_id, to_page_id, relation_type)
            if edge_key in pending_edge_keys:
                raise WikiStoreError("invalid_change_set")
            pending_edge_keys.add(edge_key)
            async with db.execute(
                """
                SELECT 1 FROM wiki_edges
                WHERE space_id = ? AND from_page_id = ? AND to_page_id = ?
                  AND relation_type = ?
                """,
                (space.id, from_page_id, to_page_id, relation_type),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    await self._mark_change_set_stale(db, change_set)
                    return None
            if relation_type == "part_of":
                pending = [to_page_id]
                visited: set[str] = set()
                while pending:
                    node = pending.pop()
                    if node == from_page_id:
                        raise WikiStoreError("invalid_change_set")
                    if node in visited:
                        continue
                    visited.add(node)
                    pending.extend(part_of_adjacency.get(node, ()))
                part_of_adjacency.setdefault(from_page_id, set()).add(to_page_id)
            try:
                edge = WikiEdge(
                    id=self._edge_id_factory(),
                    space_id=space.id,
                    from_page_id=from_page_id,
                    to_page_id=to_page_id,
                    relation_type=relation_type,
                    change_set_id=change_set.id,
                    created_at_ms=now,
                )
            except ValueError as exc:
                raise WikiStoreError("invalid_change_set") from exc
            edges.append(edge)
            edge_item_pairs.append((item, edge))
        for item, page, revision, locator_json in zip(
            page_items,
            pages,
            revisions,
            locators,
            strict=True,
        ):
            aliases_json = json.dumps(
                list(page.aliases),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await db.execute(
                """
                INSERT INTO wiki_pages (
                    id, space_id, slug, title, aliases_json, status,
                    current_revision_id, version, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)
                """,
                (
                    page.id,
                    page.space_id,
                    page.slug,
                    page.title,
                    aliases_json,
                    page.current_revision_id,
                    page.created_at_ms,
                    page.updated_at_ms,
                ),
            )
            await db.execute(
                """
                INSERT INTO wiki_page_revisions (
                    id, page_id, version, title, markdown, content_sha256,
                    change_set_id, author_kind, created_at_ms
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision.id,
                    revision.page_id,
                    revision.title,
                    revision.markdown,
                    revision.content_sha256,
                    revision.change_set_id,
                    revision.author_kind,
                    revision.created_at_ms,
                ),
            )
            await db.execute(
                """
                INSERT INTO wiki_page_sources (
                    page_id, source_id, source_locator_json, created_at_ms
                ) VALUES (?, ?, ?, ?)
                """,
                (page.id, summary.source_id, locator_json, now),
            )
            await db.execute(
                "UPDATE wiki_change_set_items SET target_id = ? WHERE id = ?",
                (page.id, item.id),
            )
            await db.execute(
                """
                INSERT INTO wiki_pages_fts (
                    page_id, space_id, title, aliases, content
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    page.id,
                    page.space_id,
                    page.title,
                    aliases_json,
                    revision.markdown,
                ),
            )
        for item, edge in edge_item_pairs:
            await db.execute(
                """
                INSERT INTO wiki_edges (
                    id, space_id, from_page_id, to_page_id, relation_type,
                    change_set_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.id,
                    edge.space_id,
                    edge.from_page_id,
                    edge.to_page_id,
                    edge.relation_type,
                    edge.change_set_id,
                    edge.created_at_ms,
                ),
            )
            await db.execute(
                "UPDATE wiki_change_set_items SET target_id = ? WHERE id = ?",
                (edge.id, item.id),
            )
        updated_at = max(now, space.updated_at_ms + 1)
        await db.execute(
            """
            UPDATE wiki_spaces
            SET graph_revision = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (space.graph_revision + 1, updated_at, space.id),
        )
        await db.execute(
            """
            UPDATE wiki_change_sets
            SET status = 'approved', decided_at_ms = ?, published_at_ms = ?
            WHERE id = ?
            """,
            (now, now, change_set.id),
        )
        return tuple(pages)

    async def _approve_conversation_change_set_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        async with db.execute(
            """
            SELECT operation_kind FROM wiki_change_set_items
            WHERE change_set_id = ? ORDER BY ordinal, id
            """,
            (change_set.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        operation_kinds = tuple(row["operation_kind"] for row in rows)
        if operation_kinds and all(kind == "page_update" for kind in operation_kinds):
            return await self._approve_conversation_page_update_transaction(
                db,
                change_set,
            )
        if operation_kinds and all(kind == "page_create" for kind in operation_kinds):
            return await self._approve_conversation_page_create_transaction(
                db,
                change_set,
            )
        if operation_kinds and all(kind == "page_delete" for kind in operation_kinds):
            return await self._approve_conversation_page_delete_transaction(
                db,
                change_set,
            )
        if operation_kinds and all(kind in {"edge_add", "edge_delete"} for kind in operation_kinds):
            return await self._approve_conversation_edge_transaction(
                db,
                change_set,
            )
        raise WikiStoreError("invalid_change_set")

    async def _approve_conversation_page_update_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        if change_set.conversation_id is None:
            raise WikiStoreError("invalid_change_set")
        async with db.execute(
            "SELECT * FROM wiki_spaces WHERE id = ?",
            (change_set.space_id,),
        ) as cursor:
            space_row = await cursor.fetchone()
        async with db.execute(
            "SELECT * FROM wiki_conversations WHERE id = ?",
            (change_set.conversation_id,),
        ) as cursor:
            conversation_row = await cursor.fetchone()
        if space_row is None or conversation_row is None:
            await self._mark_change_set_stale(db, change_set)
            return None
        space = _row_to_space(space_row)
        conversation = _row_to_conversation(conversation_row)
        if (
            space.status != "active"
            or space.graph_revision != change_set.base_graph_revision
            or conversation.space_id != space.id
            or conversation.status != "active"
        ):
            await self._mark_change_set_stale(db, change_set)
            return None
        async with db.execute(
            """
            SELECT * FROM wiki_change_set_items
            WHERE change_set_id = ? ORDER BY ordinal, id
            """,
            (change_set.id,),
        ) as cursor:
            rows = await cursor.fetchall()
        items = tuple(_row_to_change_set_item(row) for row in rows)
        if (
            not items
            or [item.ordinal for item in items] != list(range(len(items)))
            or any(item.operation_kind != "page_update" for item in items)
            or len({item.target_id for item in items}) != len(items)
        ):
            raise WikiStoreError("invalid_change_set")
        now = max(self._clock_ms(), change_set.created_at_ms)
        updates: list[tuple[WikiPage, WikiPageRevision, WikiPage]] = []
        for item in items:
            try:
                payload = json.loads(item.payload_json)
                expected_keys = {
                    "schema",
                    "page_id",
                    "base_revision_id",
                    "title",
                    "aliases",
                    "markdown",
                    "content_sha256",
                }
                if (
                    not isinstance(payload, dict)
                    or set(payload) != expected_keys
                    or not isinstance(payload.get("aliases"), list)
                    or any(not isinstance(alias, str) for alias in payload.get("aliases", ()))
                ):
                    raise ValueError("invalid page update payload")
                canonical_payload = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                aliases = tuple(payload["aliases"])
                markdown = payload["markdown"]
                title = payload["title"]
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if (
                canonical_payload != item.payload_json
                or payload.get("schema") != "llm-wiki-page-update/v1"
                or payload.get("page_id") != item.target_id
                or not isinstance(markdown, str)
                or not isinstance(title, str)
                or payload.get("content_sha256")
                != hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            ):
                raise WikiStoreError("invalid_change_set")
            async with db.execute(
                "SELECT * FROM wiki_pages WHERE id = ?",
                (item.target_id,),
            ) as cursor:
                page_row = await cursor.fetchone()
            if page_row is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            page = _row_to_page(page_row)
            if page.current_revision_id is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            async with db.execute(
                "SELECT * FROM wiki_page_revisions WHERE id = ?",
                (page.current_revision_id,),
            ) as cursor:
                revision_row = await cursor.fetchone()
            if revision_row is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            before_revision = _row_to_page_revision(revision_row)
            if (
                page.space_id != space.id
                or page.status != "active"
                or page.version != item.base_version
                or before_revision.content_sha256 != item.before_sha256
                or payload.get("base_revision_id") != before_revision.id
            ):
                await self._mark_change_set_stale(db, change_set)
                return None
            if item.unified_diff != _render_page_update_diff(
                page.slug,
                before_revision.markdown,
                markdown,
            ):
                raise WikiStoreError("invalid_change_set")
            updated_at = max(now, page.updated_at_ms + 1)
            try:
                revision = WikiPageRevision(
                    id=self._page_revision_id_factory(),
                    page_id=page.id,
                    version=page.version + 1,
                    title=title,
                    markdown=markdown,
                    content_sha256=payload["content_sha256"],
                    change_set_id=change_set.id,
                    author_kind="agent",
                    created_at_ms=updated_at,
                )
                updated_page = WikiPage(
                    id=page.id,
                    space_id=page.space_id,
                    slug=page.slug,
                    title=title,
                    aliases=aliases,
                    status="active",
                    current_revision_id=revision.id,
                    version=revision.version,
                    created_at_ms=page.created_at_ms,
                    updated_at_ms=updated_at,
                )
            except ValueError as exc:
                raise WikiStoreError("invalid_change_set") from exc
            updates.append((page, revision, updated_page))
        for _item, (before_page, revision, updated_page) in zip(
            items,
            updates,
            strict=True,
        ):
            aliases_json = json.dumps(
                list(updated_page.aliases),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await db.execute(
                """
                INSERT INTO wiki_page_revisions (
                    id, page_id, version, title, markdown, content_sha256,
                    change_set_id, author_kind, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'agent', ?)
                """,
                (
                    revision.id,
                    revision.page_id,
                    revision.version,
                    revision.title,
                    revision.markdown,
                    revision.content_sha256,
                    revision.change_set_id,
                    revision.created_at_ms,
                ),
            )
            cursor = await db.execute(
                """
                UPDATE wiki_pages
                SET title = ?, aliases_json = ?, current_revision_id = ?,
                    version = ?, updated_at_ms = ?
                WHERE id = ? AND space_id = ? AND status = 'active'
                  AND version = ? AND current_revision_id = ?
                """,
                (
                    updated_page.title,
                    aliases_json,
                    updated_page.current_revision_id,
                    updated_page.version,
                    updated_page.updated_at_ms,
                    updated_page.id,
                    updated_page.space_id,
                    before_page.version,
                    before_page.current_revision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WikiStoreError("change_set_conflict")
            await db.execute(
                "DELETE FROM wiki_pages_fts WHERE page_id = ?",
                (updated_page.id,),
            )
            await db.execute(
                """
                INSERT INTO wiki_pages_fts (
                    page_id, space_id, title, aliases, content
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    updated_page.id,
                    updated_page.space_id,
                    updated_page.title,
                    aliases_json,
                    revision.markdown,
                ),
            )
        updated_at = max(
            (updated_page.updated_at_ms for _, _, updated_page in updates),
            default=now,
        )
        await db.execute(
            """
            UPDATE wiki_spaces SET graph_revision = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (space.graph_revision + 1, updated_at, space.id),
        )
        await db.execute(
            """
            UPDATE wiki_change_sets
            SET status = 'approved', decided_at_ms = ?, published_at_ms = ?
            WHERE id = ?
            """,
            (now, now, change_set.id),
        )
        return tuple(updated_page for _, _, updated_page in updates)

    async def _active_conversation_change_context(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiSpace, WikiConversation] | None:
        if change_set.conversation_id is None:
            raise WikiStoreError("invalid_change_set")
        async with db.execute(
            "SELECT * FROM wiki_spaces WHERE id = ?",
            (change_set.space_id,),
        ) as cursor:
            space_row = await cursor.fetchone()
        async with db.execute(
            "SELECT * FROM wiki_conversations WHERE id = ?",
            (change_set.conversation_id,),
        ) as cursor:
            conversation_row = await cursor.fetchone()
        if space_row is None or conversation_row is None:
            await self._mark_change_set_stale(db, change_set)
            return None
        space = _row_to_space(space_row)
        conversation = _row_to_conversation(conversation_row)
        if (
            space.status != "active"
            or space.graph_revision != change_set.base_graph_revision
            or conversation.space_id != space.id
            or conversation.status != "active"
        ):
            await self._mark_change_set_stale(db, change_set)
            return None
        return space, conversation

    async def _ordered_change_set_items(
        self,
        db: aiosqlite.Connection,
        change_set_id: str,
    ) -> tuple[WikiChangeSetItem, ...]:
        async with db.execute(
            """
            SELECT * FROM wiki_change_set_items
            WHERE change_set_id = ? ORDER BY ordinal, id
            """,
            (change_set_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        items = tuple(_row_to_change_set_item(row) for row in rows)
        if not items or [item.ordinal for item in items] != list(range(len(items))):
            raise WikiStoreError("invalid_change_set")
        return items

    async def _publish_conversation_change_set(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
        space: WikiSpace,
        *,
        now: int,
        updated_at_ms: int,
    ) -> None:
        await db.execute(
            """
            UPDATE wiki_spaces SET graph_revision = ?, updated_at_ms = ?
            WHERE id = ?
            """,
            (
                space.graph_revision + 1,
                max(updated_at_ms, space.updated_at_ms + 1),
                space.id,
            ),
        )
        await db.execute(
            """
            UPDATE wiki_change_sets
            SET status = 'approved', decided_at_ms = ?, published_at_ms = ?
            WHERE id = ?
            """,
            (now, now, change_set.id),
        )

    async def _approve_conversation_page_create_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        context = await self._active_conversation_change_context(db, change_set)
        if context is None:
            return None
        space, _conversation = context
        items = await self._ordered_change_set_items(db, change_set.id)
        if any(item.operation_kind != "page_create" for item in items):
            raise WikiStoreError("invalid_change_set")
        now = max(self._clock_ms(), change_set.created_at_ms)
        pages: list[WikiPage] = []
        revisions: list[WikiPageRevision] = []
        slugs: set[str] = set()
        expected_keys = {
            "schema",
            "slug",
            "title",
            "aliases",
            "markdown",
            "content_sha256",
        }
        for item in items:
            try:
                payload = json.loads(item.payload_json)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != expected_keys
                    or not isinstance(payload["aliases"], list)
                    or any(not isinstance(alias, str) for alias in payload["aliases"])
                ):
                    raise ValueError("invalid page create payload")
                canonical_payload = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                markdown = payload["markdown"]
                title = payload["title"]
                slug = payload["slug"]
                aliases = tuple(payload["aliases"])
                if not all(isinstance(value, str) for value in (markdown, title, slug)):
                    raise ValueError("invalid page create text")
                page = WikiPage(
                    id=self._page_id_factory(),
                    space_id=space.id,
                    slug=slug,
                    title=title,
                    aliases=aliases,
                    status="active",
                    current_revision_id=self._page_revision_id_factory(),
                    version=1,
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                revision = WikiPageRevision(
                    id=page.current_revision_id or "",
                    page_id=page.id,
                    version=1,
                    title=page.title,
                    markdown=markdown,
                    content_sha256=payload["content_sha256"],
                    change_set_id=change_set.id,
                    author_kind="agent",
                    created_at_ms=now,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if (
                canonical_payload != item.payload_json
                or payload.get("schema") != "llm-wiki-page-create-agent/v1"
                or revision.content_sha256
                != hashlib.sha256(revision.markdown.encode("utf-8")).hexdigest()
                or item.unified_diff != _render_page_create_diff(page.slug, revision.markdown)
                or page.slug in slugs
            ):
                raise WikiStoreError("invalid_change_set")
            slugs.add(page.slug)
            async with db.execute(
                "SELECT 1 FROM wiki_pages WHERE space_id = ? AND slug = ?",
                (space.id, page.slug),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    await self._mark_change_set_stale(db, change_set)
                    return None
            pages.append(page)
            revisions.append(revision)
        for item, page, revision in zip(items, pages, revisions, strict=True):
            aliases_json = json.dumps(
                list(page.aliases),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await db.execute(
                """
                INSERT INTO wiki_pages (
                    id, space_id, slug, title, aliases_json, status,
                    current_revision_id, version, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)
                """,
                (
                    page.id,
                    page.space_id,
                    page.slug,
                    page.title,
                    aliases_json,
                    page.current_revision_id,
                    page.created_at_ms,
                    page.updated_at_ms,
                ),
            )
            await db.execute(
                """
                INSERT INTO wiki_page_revisions (
                    id, page_id, version, title, markdown, content_sha256,
                    change_set_id, author_kind, created_at_ms
                ) VALUES (?, ?, 1, ?, ?, ?, ?, 'agent', ?)
                """,
                (
                    revision.id,
                    revision.page_id,
                    revision.title,
                    revision.markdown,
                    revision.content_sha256,
                    revision.change_set_id,
                    revision.created_at_ms,
                ),
            )
            await db.execute(
                """
                INSERT INTO wiki_pages_fts (page_id, space_id, title, aliases, content)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    page.id,
                    page.space_id,
                    page.title,
                    aliases_json,
                    revision.markdown,
                ),
            )
            await db.execute(
                "UPDATE wiki_change_set_items SET target_id = ? WHERE id = ?",
                (page.id, item.id),
            )
        await self._publish_conversation_change_set(
            db,
            change_set,
            space,
            now=now,
            updated_at_ms=now,
        )
        return tuple(pages)

    async def _approve_conversation_page_delete_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        context = await self._active_conversation_change_context(db, change_set)
        if context is None:
            return None
        space, _conversation = context
        items = await self._ordered_change_set_items(db, change_set.id)
        if any(item.operation_kind != "page_delete" for item in items) or len(
            {item.target_id for item in items}
        ) != len(items):
            raise WikiStoreError("invalid_change_set")
        now = max(self._clock_ms(), change_set.created_at_ms)
        pages: list[WikiPage] = []
        expected_keys = {"schema", "page_id", "base_revision_id", "slug"}
        for item in items:
            try:
                payload = json.loads(item.payload_json)
                canonical_payload = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if not isinstance(payload, dict) or set(payload) != expected_keys:
                raise WikiStoreError("invalid_change_set")
            async with db.execute(
                "SELECT * FROM wiki_pages WHERE id = ?",
                (item.target_id,),
            ) as cursor:
                page_row = await cursor.fetchone()
            if page_row is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            page = _row_to_page(page_row)
            if page.current_revision_id is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            async with db.execute(
                "SELECT * FROM wiki_page_revisions WHERE id = ?",
                (page.current_revision_id,),
            ) as cursor:
                revision_row = await cursor.fetchone()
            if revision_row is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            revision = _row_to_page_revision(revision_row)
            if (
                canonical_payload != item.payload_json
                or payload.get("schema") != "llm-wiki-page-delete/v1"
                or payload.get("page_id") != page.id
                or payload.get("base_revision_id") != revision.id
                or payload.get("slug") != page.slug
                or page.space_id != space.id
                or page.status != "active"
                or page.version != item.base_version
                or revision.content_sha256 != item.before_sha256
                or item.unified_diff != self._render_page_delete_diff(page.slug, revision.markdown)
            ):
                if (
                    page.space_id != space.id
                    or page.status != "active"
                    or page.version != item.base_version
                    or revision.content_sha256 != item.before_sha256
                ):
                    await self._mark_change_set_stale(db, change_set)
                    return None
                raise WikiStoreError("invalid_change_set")
            pages.append(page)
        latest_update = now
        for page in pages:
            updated_at = max(now, page.updated_at_ms + 1)
            latest_update = max(latest_update, updated_at)
            cursor = await db.execute(
                """
                UPDATE wiki_pages SET status = 'deleted', updated_at_ms = ?
                WHERE id = ? AND space_id = ? AND status = 'active'
                  AND version = ? AND current_revision_id = ?
                """,
                (
                    updated_at,
                    page.id,
                    page.space_id,
                    page.version,
                    page.current_revision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WikiStoreError("change_set_conflict")
            await db.execute(
                "DELETE FROM wiki_pages_fts WHERE page_id = ?",
                (page.id,),
            )
        await self._publish_conversation_change_set(
            db,
            change_set,
            space,
            now=now,
            updated_at_ms=latest_update,
        )
        return ()

    @staticmethod
    def _render_page_delete_diff(slug: str, markdown: str) -> str:
        lines = markdown.splitlines()
        return "\n".join(
            (
                f"--- pages/{slug}.md",
                "+++ /dev/null",
                f"@@ -1,{len(lines)} +0,0 @@",
                *(f"-{line}" for line in lines),
                "",
            )
        )

    async def _approve_conversation_edge_transaction(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> tuple[WikiPage, ...] | None:
        context = await self._active_conversation_change_context(db, change_set)
        if context is None:
            return None
        space, _conversation = context
        items = await self._ordered_change_set_items(db, change_set.id)
        if any(item.operation_kind not in {"edge_add", "edge_delete"} for item in items):
            raise WikiStoreError("invalid_change_set")
        async with db.execute(
            "SELECT * FROM wiki_pages WHERE space_id = ? AND status = 'active'",
            (space.id,),
        ) as cursor:
            page_rows = await cursor.fetchall()
        pages = {row["id"]: _row_to_page(row) for row in page_rows}
        async with db.execute(
            "SELECT * FROM wiki_edges WHERE space_id = ?",
            (space.id,),
        ) as cursor:
            edge_rows = await cursor.fetchall()
        parsed_edges = tuple(_row_to_edge(row) for row in edge_rows)
        existing_edges = {edge.id: edge for edge in parsed_edges}
        delete_pairs: list[tuple[WikiChangeSetItem, WikiEdge]] = []
        deleted_ids: set[str] = set()
        for item in items:
            if item.operation_kind != "edge_delete":
                continue
            edge = existing_edges.get(item.target_id)
            try:
                payload = json.loads(item.payload_json)
                canonical_payload = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if edge is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            from_page = pages.get(edge.from_page_id)
            to_page = pages.get(edge.to_page_id)
            if from_page is None or to_page is None:
                await self._mark_change_set_stale(db, change_set)
                return None
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {
                    "schema",
                    "edge_id",
                    "from_page_id",
                    "to_page_id",
                    "relation_type",
                }
                or canonical_payload != item.payload_json
                or payload.get("schema") != "llm-wiki-edge-delete/v1"
                or payload.get("edge_id") != edge.id
                or payload.get("from_page_id") != edge.from_page_id
                or payload.get("to_page_id") != edge.to_page_id
                or payload.get("relation_type") != edge.relation_type
                or item.unified_diff
                != (
                    f"- edge pages/{from_page.slug}.md --{edge.relation_type}--> "
                    f"pages/{to_page.slug}.md\n"
                )
                or edge.id in deleted_ids
            ):
                raise WikiStoreError("invalid_change_set")
            deleted_ids.add(edge.id)
            delete_pairs.append((item, edge))
        effective_edges = {
            (
                edge.from_page_id,
                edge.to_page_id,
                edge.relation_type,
            )
            for edge in existing_edges.values()
            if edge.id not in deleted_ids
        }
        part_of_adjacency: dict[str, set[str]] = {}
        for from_id, to_id, relation_type in effective_edges:
            if relation_type == "part_of":
                part_of_adjacency.setdefault(from_id, set()).add(to_id)
        add_pairs: list[tuple[WikiChangeSetItem, WikiEdge]] = []
        now = max(self._clock_ms(), change_set.created_at_ms)
        allowed_relations = {
            "related_to",
            "references",
            "extends",
            "contradicts",
            "part_of",
        }
        for item in items:
            if item.operation_kind != "edge_add":
                continue
            try:
                payload = json.loads(item.payload_json)
                canonical_payload = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                relation_type = cast("WikiRelationType", payload["relation_type"])
                from_page = pages[payload["from_page_id"]]
                to_page = pages[payload["to_page_id"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_change_set") from exc
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema", "from_page_id", "to_page_id", "relation_type"}
                or canonical_payload != item.payload_json
                or payload.get("schema") != "llm-wiki-edge-add-agent/v1"
                or relation_type not in allowed_relations
                or from_page.id == to_page.id
                or (relation_type == "related_to" and from_page.id > to_page.id)
                or item.unified_diff
                != _render_edge_add_diff(
                    from_page.slug,
                    to_page.slug,
                    relation_type,
                )
            ):
                raise WikiStoreError("invalid_change_set")
            key = (from_page.id, to_page.id, relation_type)
            if key in effective_edges:
                raise WikiStoreError("invalid_change_set")
            if relation_type == "part_of":
                pending = [to_page.id]
                visited: set[str] = set()
                while pending:
                    node = pending.pop()
                    if node == from_page.id:
                        raise WikiStoreError("invalid_change_set")
                    if node in visited:
                        continue
                    visited.add(node)
                    pending.extend(part_of_adjacency.get(node, ()))
                part_of_adjacency.setdefault(from_page.id, set()).add(to_page.id)
            effective_edges.add(key)
            try:
                edge = WikiEdge(
                    id=self._edge_id_factory(),
                    space_id=space.id,
                    from_page_id=from_page.id,
                    to_page_id=to_page.id,
                    relation_type=relation_type,
                    change_set_id=change_set.id,
                    created_at_ms=now,
                )
            except ValueError as exc:
                raise WikiStoreError("invalid_change_set") from exc
            add_pairs.append((item, edge))
        for _item, edge in delete_pairs:
            cursor = await db.execute(
                "DELETE FROM wiki_edges WHERE id = ? AND space_id = ?",
                (edge.id, space.id),
            )
            if cursor.rowcount != 1:
                raise WikiStoreError("change_set_conflict")
        for item, edge in add_pairs:
            await db.execute(
                """
                INSERT INTO wiki_edges (
                    id, space_id, from_page_id, to_page_id, relation_type,
                    change_set_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.id,
                    edge.space_id,
                    edge.from_page_id,
                    edge.to_page_id,
                    edge.relation_type,
                    edge.change_set_id,
                    edge.created_at_ms,
                ),
            )
            await db.execute(
                "UPDATE wiki_change_set_items SET target_id = ? WHERE id = ?",
                (edge.id, item.id),
            )
        await self._publish_conversation_change_set(
            db,
            change_set,
            space,
            now=now,
            updated_at_ms=now,
        )
        return ()

    async def _mark_change_set_stale(
        self,
        db: aiosqlite.Connection,
        change_set: WikiChangeSet,
    ) -> None:
        decided_at = max(self._clock_ms(), change_set.created_at_ms)
        await db.execute(
            """
            UPDATE wiki_change_sets
            SET status = 'stale', decided_at_ms = ?
            WHERE id = ?
            """,
            (decided_at, change_set.id),
        )
        return None

    async def find_change_set_for_summary(
        self,
        summary_id: str,
    ) -> WikiChangeSet | None:
        try:
            validate_summary_id(summary_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_change_sets WHERE source_summary_id = ?",
            (summary_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _row_to_change_set(row)

    async def list_change_set_items(
        self,
        change_set_id: str,
    ) -> tuple[WikiChangeSetItem, ...]:
        await self.get_change_set(change_set_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_change_set_items
            WHERE change_set_id = ? ORDER BY ordinal, id
            """,
            (change_set_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_change_set_item(row) for row in rows)

    async def list_change_sets(self, space_id: str) -> tuple[WikiChangeSet, ...]:
        await self.get_space(space_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_change_sets
            WHERE space_id = ? ORDER BY created_at_ms, id
            """,
            (space_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_change_set(row) for row in rows)

    async def get_page(self, page_id: str) -> WikiPage:
        try:
            validate_page_id(page_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_pages WHERE id = ?",
            (page_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("page_not_found")
        return _row_to_page(row)

    async def list_pages(
        self,
        space_id: str,
        *,
        include_deleted: bool = False,
    ) -> tuple[WikiPage, ...]:
        await self.get_space(space_id)
        db = self._require_db()
        query = "SELECT * FROM wiki_pages WHERE space_id = ?"
        if not include_deleted:
            query += " AND status = 'active'"
        query += " ORDER BY slug, id"
        async with db.execute(query, (space_id,)) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_page(row) for row in rows)

    async def list_edges(self, space_id: str) -> tuple[WikiEdge, ...]:
        """Return approved page relations; system-derived source edges are excluded."""
        await self.get_space(space_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT e.*
            FROM wiki_edges AS e
            JOIN wiki_pages AS source_page ON source_page.id = e.from_page_id
            JOIN wiki_pages AS target_page ON target_page.id = e.to_page_id
            JOIN wiki_change_sets AS c ON c.id = e.change_set_id
            WHERE e.space_id = ?
              AND source_page.space_id = e.space_id
              AND target_page.space_id = e.space_id
              AND source_page.status = 'active'
              AND target_page.status = 'active'
              AND c.status = 'approved'
            ORDER BY e.relation_type, e.from_page_id, e.to_page_id, e.id
            """,
            (space_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_edge(row) for row in rows)

    async def get_edge(self, edge_id: str) -> WikiEdge:
        try:
            validate_edge_id(edge_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_edges WHERE id = ?",
            (edge_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("change_set_conflict")
        return _row_to_edge(row)

    async def get_graph_snapshot(self, space_id: str) -> WikiGraphSnapshot:
        """Project active pages, approved page edges and system source provenance."""
        space = await self.get_space(space_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT id, title
            FROM wiki_pages
            WHERE space_id = ? AND status = 'active'
            ORDER BY slug, id
            """,
            (space_id,),
        ) as cursor:
            page_rows = await cursor.fetchall()
        async with db.execute(
            """
            SELECT DISTINCT s.id, s.display_name
            FROM wiki_page_sources AS ps
            JOIN wiki_pages AS p ON p.id = ps.page_id
            JOIN wiki_sources AS s ON s.id = ps.source_id
            WHERE p.space_id = ? AND p.status = 'active' AND s.space_id = p.space_id
            ORDER BY s.display_name, s.id
            """,
            (space_id,),
        ) as cursor:
            source_rows = await cursor.fetchall()
        async with db.execute(
            """
            SELECT ps.page_id, ps.source_id
            FROM wiki_page_sources AS ps
            JOIN wiki_pages AS p ON p.id = ps.page_id
            JOIN wiki_sources AS s ON s.id = ps.source_id
            WHERE p.space_id = ? AND p.status = 'active' AND s.space_id = p.space_id
            ORDER BY ps.page_id, ps.source_id
            """,
            (space_id,),
        ) as cursor:
            source_edge_rows = await cursor.fetchall()
        page_edges = await self.list_edges(space_id)
        nodes = tuple(
            [WikiGraphNode(id=row["id"], kind="page", label=row["title"]) for row in page_rows]
            + [
                WikiGraphNode(
                    id=row["id"],
                    kind="source",
                    label=row["display_name"],
                )
                for row in source_rows
            ]
        )
        edges = tuple(
            [
                WikiGraphEdge(
                    id=edge.id,
                    from_node_id=edge.from_page_id,
                    to_node_id=edge.to_page_id,
                    relation_type=edge.relation_type,
                )
                for edge in page_edges
            ]
            + [
                WikiGraphEdge(
                    id=(f"derived_from:{row['page_id']}:{row['source_id']}"),
                    from_node_id=row["page_id"],
                    to_node_id=row["source_id"],
                    relation_type="derived_from",
                    system_managed=True,
                )
                for row in source_edge_rows
            ]
        )
        return WikiGraphSnapshot(
            space_id=space.id,
            graph_revision=space.graph_revision,
            nodes=nodes,
            edges=edges,
        )

    async def get_page_graph_neighborhood(
        self,
        space_id: str,
        page_id: str,
    ) -> WikiGraphSnapshot:
        """Return one page and its directly connected page/source nodes."""
        try:
            validate_page_id(page_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        snapshot = await self.get_graph_snapshot(space_id)
        node_by_id = {node.id: node for node in snapshot.nodes}
        page_node = node_by_id.get(page_id)
        if page_node is None or page_node.kind != "page":
            raise WikiStoreError("page_not_found")
        edges = tuple(
            edge
            for edge in snapshot.edges
            if edge.from_node_id == page_id or edge.to_node_id == page_id
        )
        node_ids = {page_id}
        for edge in edges:
            node_ids.add(edge.from_node_id)
            node_ids.add(edge.to_node_id)
        nodes = tuple(node for node in snapshot.nodes if node.id in node_ids)
        return WikiGraphSnapshot(
            space_id=snapshot.space_id,
            graph_revision=snapshot.graph_revision,
            nodes=nodes,
            edges=edges,
        )

    async def get_page_revision(self, revision_id: str) -> WikiPageRevision:
        try:
            validate_page_revision_id(revision_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_page_revisions WHERE id = ?",
            (revision_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("page_revision_not_found")
        return _row_to_page_revision(row)

    async def list_page_revisions(
        self,
        page_id: str,
    ) -> tuple[WikiPageRevision, ...]:
        await self.get_page(page_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id = ? ORDER BY version, id
            """,
            (page_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_page_revision(row) for row in rows)

    async def list_page_source_links(
        self,
        page_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """Return trusted Source IDs and canonical locator JSON for one page."""
        await self.get_page(page_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT source_id, source_locator_json
            FROM wiki_page_sources
            WHERE page_id = ? ORDER BY source_id
            """,
            (page_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple((row["source_id"], row["source_locator_json"]) for row in rows)

    async def _pages_for_change_set(
        self,
        change_set_id: str,
    ) -> tuple[WikiPage, ...]:
        db = self._require_db()
        async with db.execute(
            """
            SELECT p.*
            FROM wiki_change_set_items AS i
            JOIN wiki_pages AS p ON p.id = i.target_id
            WHERE i.change_set_id = ?
              AND p.status = 'active'
            ORDER BY i.ordinal, i.id
            """,
            (change_set_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_page(row) for row in rows)

    async def repair_page_mirrors(self) -> None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_pages WHERE status = 'active' ORDER BY space_id, slug, id"
        ) as cursor:
            rows = await cursor.fetchall()
        await self._sync_page_mirrors(tuple(_row_to_page(row) for row in rows))
        async with db.execute(
            "SELECT * FROM wiki_pages WHERE status = 'deleted' ORDER BY space_id, slug, id"
        ) as cursor:
            deleted_rows = await cursor.fetchall()
        for row in deleted_rows:
            page = _row_to_page(row)
            await asyncio.to_thread(
                self._file_store.remove_owned_file_if_present,
                page.space_id,
                f"pages/{page.slug}.md",
            )

    async def _sync_deleted_page_mirrors(self, change_set_id: str) -> None:
        db = self._require_db()
        async with db.execute(
            """
            SELECT p.* FROM wiki_change_set_items AS i
            JOIN wiki_pages AS p ON p.id = i.target_id
            WHERE i.change_set_id = ? AND i.operation_kind = 'page_delete'
              AND p.status = 'deleted'
            ORDER BY i.ordinal, i.id
            """,
            (change_set_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            page = _row_to_page(row)
            try:
                await asyncio.to_thread(
                    self._file_store.remove_owned_file_if_present,
                    page.space_id,
                    f"pages/{page.slug}.md",
                )
            except Exception as exc:
                raise WikiMirrorError("mirror_failed") from exc

    async def _sync_page_mirrors(self, pages: Sequence[WikiPage]) -> None:
        for page in pages:
            if page.current_revision_id is None:
                raise WikiMirrorError("mirror_failed")
            revision = await self.get_page_revision(page.current_revision_id)
            if (
                revision.page_id != page.id
                or revision.version != page.version
                or revision.title != page.title
                or hashlib.sha256(revision.markdown.encode("utf-8")).hexdigest()
                != revision.content_sha256
            ):
                raise WikiMirrorError("mirror_failed")
            try:
                await asyncio.to_thread(
                    self._file_store.write_owned_file_atomic,
                    page.space_id,
                    f"pages/{page.slug}.md",
                    revision.markdown.encode("utf-8"),
                    overwrite=True,
                    max_bytes=500_000,
                )
            except Exception as exc:
                raise WikiMirrorError("mirror_failed") from exc

    async def repair_page_search_index(self) -> None:
        """Rebuild the derived FTS projection from active current page revisions."""
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("DELETE FROM wiki_pages_fts")
                await db.execute(
                    """
                    INSERT INTO wiki_pages_fts (
                        page_id, space_id, title, aliases, content
                    )
                    SELECT p.id, p.space_id, p.title, p.aliases_json, r.markdown
                    FROM wiki_pages AS p
                    JOIN wiki_page_revisions AS r ON r.id = p.current_revision_id
                    JOIN wiki_change_sets AS c ON c.id = r.change_set_id
                    WHERE p.status = 'active' AND c.status = 'approved'
                    ORDER BY p.space_id, p.slug, p.id
                    """
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise

    async def search_pages(
        self,
        space_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> tuple[WikiPageSearchResult, ...]:
        await self.get_space(space_id)
        if limit < 1 or limit > 50:
            raise WikiStoreError("invalid_search_query")
        compiled = compile_wiki_fts_query(query)
        db = self._require_db()
        try:
            async with db.execute(
                """
                SELECT p.id AS page_id, p.space_id, p.current_revision_id,
                       p.version, p.slug, p.title,
                       snippet(wiki_pages_fts, 4, '【', '】', '…', 24) AS snippet,
                       bm25(wiki_pages_fts, 0.0, 0.0, 5.0, 3.0, 1.0) AS rank
                FROM wiki_pages_fts
                JOIN wiki_pages AS p ON p.id = wiki_pages_fts.page_id
                JOIN wiki_page_revisions AS r ON r.id = p.current_revision_id
                JOIN wiki_change_sets AS c ON c.id = r.change_set_id
                WHERE wiki_pages_fts MATCH ?
                  AND wiki_pages_fts.space_id = ?
                  AND p.space_id = ?
                  AND p.status = 'active'
                  AND c.status = 'approved'
                ORDER BY rank, p.id
                LIMIT ?
                """,
                (compiled, space_id, space_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        except aiosqlite.Error as exc:
            raise WikiStoreError("invalid_search_query") from exc
        return tuple(
            WikiPageSearchResult(
                page_id=row["page_id"],
                space_id=row["space_id"],
                revision_id=row["current_revision_id"],
                version=row["version"],
                slug=row["slug"],
                title=row["title"],
                snippet=row["snippet"] or "",
                rank=float(row["rank"]),
            )
            for row in rows
        )

    def _validate_parse_commit(
        self,
        source: WikiSource,
        job: WikiJob,
        attempts: Sequence[WikiParseAttempt],
        revision: WikiParseRevision,
        artifacts: Sequence[WikiArtifact],
    ) -> None:
        if (
            revision.source_id != source.id
            or revision.job_id != job.id
            or revision.source_sha256 != source.source_sha256
            or revision.requested_mode != job.requested_mode
        ):
            raise WikiStoreError("invalid_artifact")
        if [attempt.ordinal for attempt in attempts] != list(range(1, len(attempts) + 1)):
            raise WikiStoreError("invalid_artifact")
        if len({attempt.id for attempt in attempts}) != len(attempts):
            raise WikiStoreError("invalid_artifact")
        attempt_by_id = {attempt.id: attempt for attempt in attempts}
        for index, attempt in enumerate(attempts):
            if (
                attempt.source_id != source.id
                or attempt.job_id != job.id
                or attempt.source_sha256 != source.source_sha256
                or attempt.requested_mode != job.requested_mode
            ):
                raise WikiStoreError("invalid_artifact")
            fallback_id = attempt.fallback_from_attempt_id
            if fallback_id is not None and fallback_id not in {
                previous.id for previous in attempts[:index]
            }:
                raise WikiStoreError("invalid_artifact")
        selected = attempt_by_id.get(revision.selected_attempt_id)
        if (
            selected is None
            or selected != attempts[-1]
            or selected.state != "succeeded"
            or revision.parser != selected.parser
            or revision.parser_version != selected.parser_version
            or revision.preset != selected.preset
            or revision.routing_config_revision != selected.routing_config_revision
            or revision.routing_config_sha256 != selected.routing_config_sha256
        ):
            raise WikiStoreError("invalid_artifact")
        if revision.contract_version == 1 and len(attempts) != 1:
            raise WikiStoreError("invalid_artifact")
        if revision.contract_version == 2:
            if (
                job.requested_mode == "builtin"
                or len(attempts) != 1
                or attempts[0].parser != "mineru"
                or attempts[0].fallback_from_attempt_id is not None
            ):
                raise WikiStoreError("invalid_artifact")

        revision_dir = PurePosixPath(
            self._file_store.parse_revision_relative_path(source, revision.id)
        )
        markdown = [item for item in artifacts if item.kind == "parsed_markdown"]
        manifests = [item for item in artifacts if item.kind == "manifest"]
        pages = [item for item in artifacts if item.kind == "page_markdown"]
        if len(markdown) != 1 or len(manifests) != 1 or len(pages) != revision.page_count:
            raise WikiStoreError("invalid_artifact")
        if (
            PurePosixPath(markdown[0].relpath) != revision_dir / "parsed.md"
            or PurePosixPath(manifests[0].relpath) != revision_dir / "manifest.json"
            or revision.parsed_markdown_relpath != markdown[0].relpath
            or revision.parsed_markdown_sha256 != markdown[0].sha256
            or revision.manifest_relpath != manifests[0].relpath
            or revision.manifest_sha256 != manifests[0].sha256
        ):
            raise WikiStoreError("invalid_artifact")
        page_numbers: list[int] = []
        for page in pages:
            try:
                locator = json.loads(page.source_locator_json)
                page_number = locator["page_number"]
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiStoreError("invalid_artifact") from exc
            if not isinstance(page_number, int) or isinstance(page_number, bool):
                raise WikiStoreError("invalid_artifact")
            if PurePosixPath(page.relpath) != revision_dir / f"pages/{page_number:06d}.md":
                raise WikiStoreError("invalid_artifact")
            page_numbers.append(page_number)
        if sorted(page_numbers) != list(range(1, revision.page_count + 1)):
            raise WikiStoreError("invalid_artifact")
        images_dir = revision_dir / "images"
        for item in artifacts:
            if item.source_id != source.id or item.parse_revision_id != revision.id:
                raise WikiStoreError("invalid_artifact")
            item_path = PurePosixPath(item.relpath)
            if item.kind in {"embedded_image", "table_image"}:
                if item_path.parent != images_dir:
                    raise WikiStoreError("invalid_artifact")
            elif item.kind == "page_markdown":
                if item_path.parent != revision_dir / "pages":
                    raise WikiStoreError("invalid_artifact")
            elif item_path.parent != revision_dir:
                raise WikiStoreError("invalid_artifact")
        paths = [artifact.relpath.casefold() for artifact in artifacts]
        if len(set(paths)) != len(paths):
            raise WikiStoreError("invalid_artifact")

    async def _assert_parse_commit_cas(
        self,
        db: aiosqlite.Connection,
        source: WikiSource,
        job: WikiJob,
    ) -> None:
        async with db.execute(
            """
            SELECT status, updated_at_ms, selection_version,
                   selected_parse_revision_id
            FROM wiki_sources WHERE id = ?
            """,
            (source.id,),
        ) as cursor:
            source_row = await cursor.fetchone()
        async with db.execute(
            "SELECT status, started_at_ms FROM wiki_jobs WHERE id = ?",
            (job.id,),
        ) as cursor:
            job_row = await cursor.fetchone()
        if (
            source_row is None
            or source_row["status"] != "parsing"
            or source_row["updated_at_ms"] != source.updated_at_ms
            or source_row["selection_version"] != job.base_selection_version
            or source_row["selected_parse_revision_id"] != job.base_selected_parse_revision_id
            or job_row is None
            or job_row["status"] != "running"
            or job_row["started_at_ms"] != job.started_at_ms
        ):
            raise WikiStoreError("source_conflict")

    @staticmethod
    async def _insert_parse_attempt(
        db: aiosqlite.Connection,
        attempt: WikiParseAttempt,
    ) -> None:
        await db.execute(
            """
            INSERT INTO wiki_parse_attempts (
                id, provider_attempt_id, source_id, job_id, ordinal,
                requested_mode, source_sha256, parser, parser_version, preset,
                routing_config_revision, routing_config_sha256,
                route_reasons_json, state, safe_error_code, quality_report_json,
                output_size_bytes, output_sha256, fallback_from_attempt_id,
                started_at_ms, finished_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.id,
                attempt.provider_attempt_id,
                attempt.source_id,
                attempt.job_id,
                attempt.ordinal,
                attempt.requested_mode,
                attempt.source_sha256,
                attempt.parser,
                attempt.parser_version,
                attempt.preset,
                attempt.routing_config_revision,
                attempt.routing_config_sha256,
                attempt.route_reasons_json,
                attempt.state,
                attempt.safe_error_code,
                attempt.quality_report_json,
                attempt.output_size_bytes,
                attempt.output_sha256,
                attempt.fallback_from_attempt_id,
                attempt.started_at_ms,
                attempt.finished_at_ms,
            ),
        )

    @staticmethod
    async def _insert_parse_revision(
        db: aiosqlite.Connection,
        revision: WikiParseRevision,
    ) -> None:
        await db.execute(
            """
            INSERT INTO wiki_parse_revisions (
                id, source_id, job_id, selected_attempt_id, contract_version,
                artifact_schema, requested_mode, source_sha256, parser,
                parser_version, preset, routing_config_revision,
                routing_config_sha256, parsed_markdown_relpath,
                parsed_markdown_sha256, manifest_relpath, manifest_sha256,
                page_count, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision.id,
                revision.source_id,
                revision.job_id,
                revision.selected_attempt_id,
                revision.contract_version,
                revision.artifact_schema,
                revision.requested_mode,
                revision.source_sha256,
                revision.parser,
                revision.parser_version,
                revision.preset,
                revision.routing_config_revision,
                revision.routing_config_sha256,
                revision.parsed_markdown_relpath,
                revision.parsed_markdown_sha256,
                revision.manifest_relpath,
                revision.manifest_sha256,
                revision.page_count,
                revision.created_at_ms,
            ),
        )

    @staticmethod
    async def _insert_artifact(
        db: aiosqlite.Connection,
        artifact: WikiArtifact,
    ) -> None:
        await db.execute(
            """
            INSERT INTO wiki_artifacts (
                id, source_id, parse_revision_id, kind, relpath, mime_type,
                size_bytes, sha256, width, height, source_locator_json,
                created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.id,
                artifact.source_id,
                artifact.parse_revision_id,
                artifact.kind,
                artifact.relpath,
                artifact.mime_type,
                artifact.size_bytes,
                artifact.sha256,
                artifact.width,
                artifact.height,
                artifact.source_locator_json,
                artifact.created_at_ms,
            ),
        )

    async def get_parse_revision(self, parse_revision_id: str) -> WikiParseRevision:
        try:
            validate_parse_revision_id(parse_revision_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_parse_revisions WHERE id = ?",
            (parse_revision_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("artifact_not_found")
        return _row_to_parse_revision(row)

    async def get_selected_parse_revision(
        self,
        source_id: str,
    ) -> WikiParseRevision | None:
        source = await self.get_source(source_id)
        if source.selected_parse_revision_id is None:
            return None
        revision = await self.get_parse_revision(source.selected_parse_revision_id)
        if revision.source_id != source.id:
            raise WikiStoreError("database_corrupt")
        return revision

    async def list_parse_revisions(self, source_id: str) -> tuple[WikiParseRevision, ...]:
        await self.get_source(source_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_parse_revisions
            WHERE source_id = ? ORDER BY created_at_ms, id
            """,
            (source_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_parse_revision(row) for row in rows)

    async def list_parse_attempts(self, job_id: str) -> tuple[WikiParseAttempt, ...]:
        await self.get_job(job_id)
        db = self._require_db()
        async with db.execute(
            """
            SELECT * FROM wiki_parse_attempts
            WHERE job_id = ? ORDER BY ordinal, id
            """,
            (job_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_parse_attempt(row) for row in rows)

    async def record_terminal_parse_attempts(
        self,
        job_id: str,
        attempts: Sequence[WikiParseAttempt],
    ) -> tuple[WikiParseAttempt, ...]:
        """Persist terminal provider evidence without publishing a revision.

        A provider may report a successful parse attempt while its downloaded
        artifact later fails the application's trust-boundary validation.  In
        that case the attempt remains ``succeeded`` evidence, but no parse
        revision is created or selected.
        """
        job = await self.get_job(job_id)
        if job.status != "running" or job.source_id is None or not attempts:
            raise WikiStoreError("job_conflict")
        source = await self.get_source(job.source_id)
        if [attempt.ordinal for attempt in attempts] != list(range(1, len(attempts) + 1)):
            raise WikiStoreError("invalid_artifact")
        if any(
            attempt.job_id != job.id
            or attempt.source_id != source.id
            or attempt.source_sha256 != source.source_sha256
            or attempt.requested_mode != job.requested_mode
            or attempt.state not in {"succeeded", "failed", "quality_rejected", "cancelled"}
            for attempt in attempts
        ):
            raise WikiStoreError("invalid_artifact")
        for index, attempt in enumerate(attempts):
            fallback_id = attempt.fallback_from_attempt_id
            if fallback_id is not None and fallback_id not in {
                previous.id for previous in attempts[:index]
            }:
                raise WikiStoreError("invalid_artifact")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_parse_attempts WHERE job_id = ? ORDER BY ordinal",
                    (job.id,),
                ) as cursor:
                    existing_rows = await cursor.fetchall()
                existing = tuple(_row_to_parse_attempt(row) for row in existing_rows)
                if existing:
                    if existing != tuple(attempts):
                        raise WikiStoreError("job_conflict")
                    await db.execute("COMMIT")
                    return existing
                async with db.execute(
                    "SELECT status FROM wiki_jobs WHERE id = ?",
                    (job.id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or row["status"] != "running":
                    raise WikiStoreError("job_conflict")
                for attempt in attempts:
                    await self._insert_parse_attempt(db, attempt)
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("job_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        return tuple(attempts)

    async def record_unsuccessful_parse_attempts(
        self,
        job_id: str,
        attempts: Sequence[WikiParseAttempt],
    ) -> tuple[WikiParseAttempt, ...]:
        """Persist only failed/rejected/cancelled attempts for compatibility."""

        if any(attempt.state == "succeeded" for attempt in attempts):
            raise WikiStoreError("invalid_artifact")
        return await self.record_terminal_parse_attempts(job_id, attempts)

    @staticmethod
    def _selected_pointer(
        source: WikiSource,
        revision: WikiParseRevision,
    ) -> WikiSelectedParsePointer:
        return WikiSelectedParsePointer(
            source_id=source.id,
            source_sha256=source.source_sha256,
            parse_revision_id=revision.id,
            selection_version=source.selection_version,
            manifest_relpath=revision.manifest_relpath,
            manifest_sha256=revision.manifest_sha256,
            selected_at_ms=(
                source.selected_at_ms if source.selected_at_ms is not None else source.updated_at_ms
            ),
        )

    async def select_parse_revision(
        self,
        source_id: str,
        parse_revision_id: str,
        *,
        expected_selection_version: int,
    ) -> WikiSource:
        source = await self.get_source(source_id)
        revision = await self.get_parse_revision(parse_revision_id)
        if revision.source_id != source.id or source.status in {"parsing", "deleting"}:
            raise WikiStoreError("source_conflict")
        if source.selection_version != expected_selection_version:
            raise WikiStoreError("source_conflict")
        if source.selected_parse_revision_id == revision.id:
            return source
        artifacts = await self.list_artifacts(
            source.id,
            parse_revision_id=revision.id,
        )
        revision_job = await self.get_job(revision.job_id)
        revision_attempts = await self.list_parse_attempts(revision.job_id)
        self._validate_parse_commit(
            source,
            revision_job,
            revision_attempts,
            revision,
            artifacts,
        )
        for artifact in artifacts:
            content = await asyncio.to_thread(
                self._file_store.read_owned_file,
                source.space_id,
                artifact.relpath,
                max_bytes=artifact.size_bytes,
            )
            if (
                len(content) != artifact.size_bytes
                or hashlib.sha256(content).hexdigest() != artifact.sha256
            ):
                raise WikiStoreError("invalid_artifact")
        selected_at = max(self._clock_ms(), source.updated_at_ms + 1)
        selected = WikiSource.model_validate(
            source.model_copy(
                update={
                    "selected_parse_revision_id": revision.id,
                    "selection_version": source.selection_version + 1,
                    "selected_at_ms": selected_at,
                    "status": "parsed",
                    "safe_error_code": "",
                    "updated_at_ms": selected_at,
                }
            ).model_dump()
        )
        pointer = self._selected_pointer(selected, revision)
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                result = await db.execute(
                    """
                    UPDATE wiki_sources
                    SET selected_parse_revision_id = ?, selection_version = ?,
                        selected_at_ms = ?, status = 'parsed', safe_error_code = '',
                        updated_at_ms = ?
                    WHERE id = ? AND selection_version = ?
                          AND status NOT IN ('parsing', 'deleting')
                    """,
                    (
                        revision.id,
                        selected.selection_version,
                        selected_at,
                        selected.updated_at_ms,
                        source.id,
                        expected_selection_version,
                    ),
                )
                if result.rowcount != 1:
                    raise WikiStoreError("source_conflict")
                await asyncio.to_thread(
                    self._file_store.write_selected_parse_pointer,
                    source.space_id,
                    selected,
                    revision,
                    pointer,
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                await self._repair_selected_pointer_quietly(source)
                raise
        return selected

    async def repair_selected_parse_pointers(self) -> tuple[str, ...]:
        db = self._require_db()
        async with db.execute("SELECT * FROM wiki_sources ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        repaired: list[str] = []
        for row in rows:
            source = _row_to_source(row)
            if await self._sync_selected_pointer(source):
                repaired.append(source.id)
        return tuple(repaired)

    async def _sync_selected_pointer(self, source: WikiSource) -> bool:
        if source.status == "deleting":
            return False
        if source.selected_parse_revision_id is None:
            return await asyncio.to_thread(
                self._file_store.remove_owned_file_if_present,
                source.space_id,
                self._file_store.selected_parse_relative_path(source),
            )
        revision = await self.get_parse_revision(source.selected_parse_revision_id)
        if revision.source_id != source.id:
            raise WikiStoreError("database_corrupt")
        pointer = self._selected_pointer(source, revision)
        return await asyncio.to_thread(
            self._file_store.write_selected_parse_pointer,
            source.space_id,
            source,
            revision,
            pointer,
        )

    async def _repair_selected_pointer_quietly(self, source: WikiSource) -> None:
        try:
            await self._sync_selected_pointer(source)
        except Exception:
            pass

    async def get_artifact(self, artifact_id: str) -> WikiArtifact:
        try:
            validate_artifact_id(artifact_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_artifacts WHERE id = ?",
            (artifact_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("artifact_not_found")
        return _row_to_artifact(row)

    async def read_source_content(self, source: WikiSource) -> bytes:
        persisted = await self.get_source(source.id, space_id=source.space_id)
        if persisted != source:
            raise WikiStoreError("source_conflict")
        if source.status == "deleting":
            raise WikiStoreError("source_not_available")
        content = await asyncio.to_thread(
            self._file_store.read_owned_file,
            source.space_id,
            source.source_relpath,
            max_bytes=source.size_bytes,
        )
        if (
            len(content) != source.size_bytes
            or hashlib.sha256(content).hexdigest() != source.source_sha256
        ):
            raise WikiStoreError("invalid_source")
        return content

    async def read_artifact_content(
        self,
        source: WikiSource,
        artifact: WikiArtifact,
    ) -> bytes:
        persisted = await self.get_artifact(artifact.id)
        if persisted != artifact or artifact.source_id != source.id:
            raise WikiStoreError("source_conflict")
        current_source = await self.get_source(source.id, space_id=source.space_id)
        if current_source.status == "deleting":
            raise WikiStoreError("source_not_available")
        content = await asyncio.to_thread(
            self._file_store.read_owned_file,
            source.space_id,
            artifact.relpath,
            max_bytes=artifact.size_bytes,
        )
        if (
            len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise WikiStoreError("invalid_artifact")
        return content

    async def _rollback_source_file(
        self,
        source: WikiSource,
        wrote_file: bool,
        max_bytes: int,
    ) -> None:
        if not wrote_file:
            return
        try:
            await asyncio.to_thread(
                self._file_store.remove_owned_file_if_sha256,
                source.space_id,
                source.source_relpath,
                source.source_sha256,
                max_bytes=max_bytes,
            )
        except Exception:
            pass

    @staticmethod
    async def _assert_space_active(
        db: aiosqlite.Connection,
        space_id: str,
    ) -> None:
        async with db.execute(
            "SELECT status FROM wiki_spaces WHERE id = ?",
            (space_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("space_not_found")
        if row["status"] != "active":
            raise WikiStoreError("space_read_only")

    async def repair_space_mirrors(self) -> WikiMirrorRepairReport:
        spaces = await self.list_spaces()
        try:
            return await asyncio.to_thread(
                self._file_store.reconcile_space_layouts,
                spaces,
            )
        except WikiStoreError:
            raise
        except Exception as exc:
            raise WikiMirrorError("mirror_failed") from exc

    async def _sync_mirror_or_raise(self, space: WikiSpace) -> None:
        try:
            await asyncio.to_thread(self._file_store.create_or_repair_space_layout, space)
        except Exception as exc:
            raise WikiMirrorError("mirror_failed") from exc

    async def _initialize_schema(self) -> None:
        db = self._require_db()
        await self._validate_sqlite_capabilities()
        application_id = await self._read_pragma_int("application_id")
        if application_id not in {0, WIKI_APPLICATION_ID}:
            raise WikiSchemaError("schema_incompatible")
        await db.execute(_SCHEMA_META_DDL)
        version = await self.get_schema_version()
        if version is None:
            existing = await self._user_tables()
            if existing - {"wiki_schema_meta"}:
                raise WikiSchemaError("schema_incompatible")
            await self._initialize_fresh_schema()
        elif version in {1, 2, 3, 4, 5, 6, 7} and version < WIKI_SCHEMA_VERSION:
            raise WikiSchemaError("schema_rebuild_required")
        elif version != WIKI_SCHEMA_VERSION:
            raise WikiSchemaError("schema_incompatible")
        elif await self._read_pragma_int("application_id") != WIKI_APPLICATION_ID:
            raise WikiSchemaError("schema_incompatible")
        await self._validate_schema()

    async def _initialize_fresh_schema(self) -> None:
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT value FROM wiki_schema_meta WHERE key = ?",
                (_SCHEMA_META_KEY,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                await db.execute("COMMIT")
                return
            for statement in _DDL_STATEMENTS:
                await db.execute(statement)
            await db.execute(
                "INSERT INTO wiki_schema_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_META_KEY, WIKI_SCHEMA_VERSION),
            )
            await db.execute(f"PRAGMA application_id = {WIKI_APPLICATION_ID}")
            await db.execute(f"PRAGMA user_version = {WIKI_SCHEMA_VERSION}")
            await db.execute("COMMIT")
        except BaseException:
            await self._rollback_quietly(db)
            raise

    async def _validate_schema(self) -> None:
        db = self._require_db()
        if await self._read_pragma_int("application_id") != WIKI_APPLICATION_ID:
            raise WikiSchemaError("schema_incompatible")
        if await self._read_pragma_int("user_version") != WIKI_SCHEMA_VERSION:
            raise WikiSchemaError("schema_incompatible")
        async with db.execute("PRAGMA foreign_keys") as cursor:
            foreign_keys = await cursor.fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise WikiSchemaError("schema_incompatible")
        tables = await self._user_tables()
        required_tables = set(_REQUIRED_COLUMNS) | {"wiki_schema_meta"}
        if not required_tables.issubset(tables):
            raise WikiSchemaError("schema_incompatible")
        if any(name.startswith("knowledge_") for name in tables):
            raise WikiSchemaError("schema_incompatible")
        for table, required_columns in _REQUIRED_COLUMNS.items():
            columns = await self._table_columns(table)
            if not required_columns.issubset(columns):
                raise WikiSchemaError("schema_incompatible")
        async with db.execute("PRAGMA quick_check") as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] != "ok":
            raise WikiSchemaError("database_corrupt")
        async with db.execute("PRAGMA foreign_key_check") as cursor:
            violation = await cursor.fetchone()
        if violation is not None:
            raise WikiSchemaError("database_corrupt")

    async def _validate_sqlite_capabilities(self) -> None:
        db = self._require_db()
        try:
            async with db.execute(
                "SELECT sqlite_version(), json_valid('[]'), json_type('[]')"
            ) as cursor:
                row = await cursor.fetchone()
            await db.execute("CREATE VIRTUAL TABLE temp.wiki_fts_probe USING fts5(value)")
            await db.execute("DROP TABLE temp.wiki_fts_probe")
        except aiosqlite.Error as exc:
            raise WikiSchemaError("schema_incompatible") from exc
        if row is None:
            raise WikiSchemaError("schema_incompatible")
        try:
            version = tuple(int(part) for part in str(row[0]).split(".")[:3])
        except ValueError as exc:
            raise WikiSchemaError("schema_incompatible") from exc
        if len(version) != 3 or version < (3, 37, 0) or int(row[1]) != 1 or row[2] != "array":
            raise WikiSchemaError("schema_incompatible")

    async def _user_tables(self) -> set[str]:
        db = self._require_db()
        async with db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(row["name"]) for row in rows}

    async def _table_columns(self, table: str) -> frozenset[str]:
        if table not in _REQUIRED_COLUMNS:
            raise WikiSchemaError("schema_incompatible")
        db = self._require_db()
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        return frozenset(str(row["name"]) for row in rows)

    async def _read_pragma_int(self, name: str) -> int:
        if name not in {"application_id", "user_version"}:
            raise WikiSchemaError("schema_incompatible")
        db = self._require_db()
        async with db.execute(f"PRAGMA {name}") as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiSchemaError("schema_incompatible")
        return int(row[0])

    @staticmethod
    async def _rollback_quietly(db: aiosqlite.Connection) -> None:
        try:
            await db.execute("ROLLBACK")
        except aiosqlite.Error:
            pass


__all__ = [
    "LegacyPolicy",
    "WIKI_APPLICATION_ID",
    "WikiStore",
]
