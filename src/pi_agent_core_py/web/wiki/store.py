"""SQLite canonical store for page-centric LLM Wiki schema v2."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import aiosqlite

from .errors import WikiMirrorError, WikiSchemaError, WikiStoreError
from .files import WikiFileStore
from .legacy import retire_legacy_knowledge
from .models import (
    WIKI_SCHEMA_VERSION,
    WikiArtifact,
    WikiArtifactKind,
    WikiJob,
    WikiJobKind,
    WikiJobStatus,
    WikiLegacyBackupReceipt,
    WikiMirrorRepairReport,
    WikiParseAttempt,
    WikiParseAttemptState,
    WikiParseMode,
    WikiParseRevision,
    WikiSelectedParsePointer,
    WikiSource,
    WikiSourceMimeType,
    WikiSourceStatus,
    WikiSpace,
    WikiSpaceStatus,
    validate_artifact_id,
    validate_job_id,
    validate_parse_revision_id,
    validate_source_id,
    validate_space_id,
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
        CHECK (kind IN ('parse', 'synthesize_entry_page', 'rebuild_search',
                        'rebuild_graph_projection')),
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        CHECK (attempt >= 1),
        CHECK (requested_mode IN ('builtin', 'auto', 'fast', 'accurate')),
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
        CHECK (requested_mode IN ('builtin', 'auto', 'fast', 'accurate')),
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
        CHECK (requested_mode IN ('builtin', 'auto', 'fast', 'accurate')),
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
                legacy_backup_receipt=backup_receipt,
            )
            await store._initialize_schema()
            await connection.execute("PRAGMA journal_mode=WAL")
            store._startup_repair_report = await store.repair_space_mirrors()
            await store.repair_selected_parse_pointers()
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
        )
        await store._initialize_schema()
        store._startup_repair_report = await store.repair_space_mirrors()
        await store.repair_selected_parse_pointers()
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
        """Soft-delete by entering ``deleting``; physical deletion is a later workflow."""
        return await self.set_space_status(
            space_id,
            "deleting",
            expected_updated_at_ms=expected_updated_at_ms,
        )

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
            raise WikiStoreError("invalid_source")
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
            except aiosqlite.IntegrityError as exc:
                raise WikiStoreError("job_conflict") from exc
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
            if job.requested_mode == "builtin" or len(attempts) > 2:
                raise WikiStoreError("invalid_artifact")
            if len(attempts) == 2:
                first, second = attempts
                if (
                    job.requested_mode != "auto"
                    or first.parser != "pymupdf4llm"
                    or first.state != "quality_rejected"
                    or second.parser != "docling"
                    or second.fallback_from_attempt_id != first.id
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
            or source_row["selected_parse_revision_id"]
            != job.base_selected_parse_revision_id
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
                source.selected_at_ms
                if source.selected_at_ms is not None
                else source.updated_at_ms
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
        elif version == 1 and WIKI_SCHEMA_VERSION == 2:
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
