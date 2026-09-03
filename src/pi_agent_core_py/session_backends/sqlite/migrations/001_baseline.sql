CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    active_lane     TEXT NOT NULL DEFAULT 'main'
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, idx)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    turn_id         TEXT,
    content_json    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_entries (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    parent_id       TEXT,
    message_id      TEXT NOT NULL,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES session_entries(id),
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS session_lanes (
    session_id      TEXT NOT NULL,
    name            TEXT NOT NULL,
    leaf_entry_id   TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (session_id, name),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (leaf_entry_id) REFERENCES session_entries(id)
);

CREATE TABLE IF NOT EXISTS session_facts (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    entry_id        TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (entry_id) REFERENCES session_entries(id),
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS session_operations (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    lane            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    source_leaf_id  TEXT,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (source_leaf_id) REFERENCES session_entries(id)
);

CREATE TABLE IF NOT EXISTS session_operation_records (
    id              TEXT PRIMARY KEY,
    operation_id    TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    record_type     TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES session_operations(id)
        ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_idx
    ON messages(session_id, idx);
CREATE INDEX IF NOT EXISTS idx_snapshots_session_created
    ON snapshots(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_updated
    ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_session_entries_parent
    ON session_entries(session_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_session_entries_message
    ON session_entries(session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_session_facts_entry
    ON session_facts(session_id, entry_id, kind, seq);
CREATE INDEX IF NOT EXISTS idx_session_operations_lane
    ON session_operations(session_id, lane, created_at);
CREATE INDEX IF NOT EXISTS idx_session_operation_records_operation
    ON session_operation_records(operation_id, seq);
