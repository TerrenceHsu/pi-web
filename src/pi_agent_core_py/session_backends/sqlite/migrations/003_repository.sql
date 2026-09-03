ALTER TABLE sessions ADD COLUMN parent_session_id TEXT;

ALTER TABLE session_entries ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'message';
ALTER TABLE session_entries ADD COLUMN payload_json TEXT;
ALTER TABLE session_entries ADD COLUMN global_seq INTEGER;

ALTER TABLE session_facts ADD COLUMN global_seq INTEGER;
ALTER TABLE session_operation_records ADD COLUMN global_seq INTEGER;
ALTER TABLE session_lanes ADD COLUMN open_operation_id TEXT;

CREATE TABLE session_sequences (
    session_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE session_stats (
    session_id TEXT PRIMARY KEY,
    message_count INTEGER NOT NULL DEFAULT 0,
    cached_tokens REAL NOT NULL DEFAULT 0,
    uncached_tokens REAL NOT NULL DEFAULT 0,
    total_tokens REAL NOT NULL DEFAULT 0,
    cost_total REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE session_records (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    id TEXT NOT NULL,
    lane TEXT NOT NULL,
    run_id TEXT,
    type TEXT NOT NULL,
    operation_kind TEXT,
    timestamp INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (session_id, id),
    UNIQUE (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE session_lane_moves (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    lane TEXT NOT NULL,
    leaf_id TEXT,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE session_global_facts (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    key TEXT,
    value_json TEXT,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE session_log (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    item_id TEXT,
    timestamp INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE writer_leases (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    fence INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE branch_entries (
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    entry_seq INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    custom_type TEXT,
    PRIMARY KEY (session_id, branch_id, entry_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE branch_tips (
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    tip_id TEXT NOT NULL,
    PRIMARY KEY (session_id, tip_id),
    UNIQUE (session_id, branch_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_session_entries_type_seq
    ON session_entries(session_id, entry_type, global_seq);
CREATE UNIQUE INDEX uq_session_entries_global_seq
    ON session_entries(session_id, global_seq) WHERE global_seq IS NOT NULL;
CREATE UNIQUE INDEX uq_session_facts_global_seq
    ON session_facts(session_id, global_seq) WHERE global_seq IS NOT NULL;
CREATE UNIQUE INDEX uq_session_operation_records_global_seq
    ON session_operation_records(session_id, global_seq)
    WHERE global_seq IS NOT NULL;
CREATE INDEX idx_session_records_lane_seq
    ON session_records(session_id, lane, seq);
CREATE INDEX idx_session_records_type_seq
    ON session_records(session_id, type, seq);
CREATE INDEX idx_session_records_run_seq
    ON session_records(session_id, run_id, seq);
CREATE INDEX idx_session_global_facts_kind_key_seq
    ON session_global_facts(session_id, kind, key, seq);
CREATE INDEX idx_session_log_seq
    ON session_log(session_id, seq);
CREATE INDEX idx_branch_entries_branch_seq
    ON branch_entries(session_id, branch_id, entry_seq);
CREATE INDEX idx_branch_entries_entry
    ON branch_entries(session_id, entry_id, branch_id, entry_seq);
