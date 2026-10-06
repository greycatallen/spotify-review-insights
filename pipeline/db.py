"""SQLite state store: one file per run namespace (pilot_100, checkpoint_500, full, ...).

WAL mode + one transaction per saved batch gives atomic, crash-safe progress. Results are
keyed by (text_sha256, label_config), so exact duplicate texts share one validated result and
changing the model/prompt/schema config forces new work.
"""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    src_order INTEGER NOT NULL UNIQUE,
    review_text TEXT NOT NULL,
    review_rating TEXT NOT NULL,
    review_likes TEXT NOT NULL,
    app_version TEXT NOT NULL,
    review_timestamp TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    is_empty INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_text ON reviews(text_sha256);

-- One row per distinct nonempty text; canonical_id = earliest source row with that text.
CREATE TABLE IF NOT EXISTS texts (
    text_sha256 TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL REFERENCES reviews(review_id),
    n_reviews INTEGER NOT NULL,
    char_len INTEGER NOT NULL
);

-- Validated labels for a distinct text under one label_config. request_id links to calls.
CREATE TABLE IF NOT EXISTS results (
    text_sha256 TEXT NOT NULL,
    label_config TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    intent TEXT NOT NULL,
    sentiment REAL NOT NULL,
    severity INTEGER NOT NULL,
    entities TEXT NOT NULL,          -- JSON list of strings
    evidence_quote TEXT NOT NULL,
    needs_review INTEGER NOT NULL,
    subtopic TEXT,                   -- optional richer internal label
    request_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT 'primary',   -- primary | fallback
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_sha256, label_config)
);

-- Every attempted model call, including failures; exported to calls.jsonl.
CREATE TABLE IF NOT EXISTS calls (
    request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,              -- enrich | verify | group | memo
    phase TEXT,                      -- initial | resume (enrich); pilot run kind otherwise
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    settings TEXT NOT NULL,          -- JSON: effort/thinking/max_tokens/batch size...
    label_config TEXT,
    review_ids TEXT NOT NULL,        -- JSON list actually sent
    outcome TEXT NOT NULL,           -- succeeded | failed
    attempt INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    billed_units REAL,               -- for non-token billing (e.g. per classification)
    billed_unit TEXT,
    cost_usd REAL,                   -- computed from rates at call time; NULL if unknown
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    latency_s REAL NOT NULL,
    error TEXT,
    extra TEXT                       -- JSON: invalid ids, provider usage, uncertain flag...
);

-- Per-text attempt bookkeeping so failures are retained with reasons.
CREATE TABLE IF NOT EXISTS failures (
    text_sha256 TEXT NOT NULL,
    label_config TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (text_sha256, label_config)
);

-- Shared spend ledger: reserve worst case before dispatch, settle after.
CREATE TABLE IF NOT EXISTS spend (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    request_id TEXT,
    reserved_usd REAL NOT NULL,
    actual_usd REAL,
    status TEXT NOT NULL,            -- reserved | settled | released | unreconciled
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT
);
CREATE TABLE IF NOT EXISTS membership (
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    review_id TEXT NOT NULL REFERENCES reviews(review_id),
    PRIMARY KEY (issue_id, review_id)
);

CREATE TABLE IF NOT EXISTS verifications (
    review_id TEXT NOT NULL,
    label_config TEXT NOT NULL,
    request_id TEXT NOT NULL,
    topic TEXT, intent TEXT, severity INTEGER,
    agrees_topic INTEGER, agrees_intent INTEGER, agrees_severity INTEGER,
    PRIMARY KEY (review_id, label_config)
);
"""


def connect(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


class transaction:
    """BEGIN IMMEDIATE ... COMMIT; rolls back on any exception."""

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc, tb):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
