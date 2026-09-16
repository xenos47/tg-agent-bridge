CREATE TABLE peers (
    peer_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('user','group','channel')),
    title TEXT NOT NULL,
    username TEXT,
    slug TEXT NOT NULL UNIQUE,
    watched INTEGER NOT NULL DEFAULT 1 CHECK (watched IN (0,1)),
    sendable INTEGER NOT NULL DEFAULT 0 CHECK (sendable IN (0,1)),
    priority INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL
);
CREATE INDEX idx_peers_watched ON peers(watched) WHERE watched = 1;
CREATE TRIGGER peers_slug_immutable
BEFORE UPDATE OF slug ON peers
WHEN old.slug != new.slug
BEGIN
    SELECT RAISE(ABORT, 'peer slug is immutable');
END;

CREATE TABLE messages (
    peer_id INTEGER NOT NULL REFERENCES peers(peer_id),
    msg_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT NOT NULL DEFAULT '',
    reply_to INTEGER,
    fwd_from TEXT,
    has_media INTEGER NOT NULL DEFAULT 0 CHECK (has_media IN (0,1)),
    media_kind TEXT,
    edit_ts INTEGER,
    deleted_at INTEGER,
    raw_json TEXT NOT NULL,
    synced_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, msg_id)
);
CREATE INDEX idx_messages_ts ON messages(ts DESC);
CREATE INDEX idx_messages_peer_ts ON messages(peer_id, ts DESC);
CREATE INDEX idx_messages_thread ON messages(peer_id, reply_to) WHERE reply_to IS NOT NULL;

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE tags (
    peer_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('rule','manual','agent')),
    rule_id TEXT,
    confidence REAL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (peer_id, msg_id, tag),
    FOREIGN KEY (peer_id, msg_id) REFERENCES messages(peer_id, msg_id) ON DELETE CASCADE
);
CREATE INDEX idx_tags_tag ON tags(tag, created_at DESC);

CREATE TABLE sync_state (
    peer_id INTEGER PRIMARY KEY REFERENCES peers(peer_id),
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    backfill_done INTEGER NOT NULL DEFAULT 0 CHECK (backfill_done IN (0,1)),
    backfill_cursor INTEGER,
    last_synced_at INTEGER,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    cooldown_until INTEGER
);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id INTEGER NOT NULL REFERENCES peers(peer_id),
    body TEXT NOT NULL,
    reply_to INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','sent','failed')),
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    decided_by TEXT,
    decided_at INTEGER,
    sent_msg_id INTEGER,
    sent_at INTEGER,
    error TEXT
);
CREATE INDEX idx_outbox_status ON outbox(status, created_at);

CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE INDEX idx_audit_ts ON audit(ts DESC);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

CREATE TABLE rate_limit (
    scope TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, window_start)
);
