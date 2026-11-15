"""SQLite schema kept separate so database logic stays focused."""

from __future__ import annotations

SCHEMA_VERSION = 2

# Columns added to harvest_runs after the original schema. Existing databases
# are migrated in-place by Database.initialize().
RUN_CATALOG_COLUMNS: dict[str, str] = {
    "set_catalog_generation_id": "INTEGER",
    "set_catalog_generation": "INTEGER",
    "set_catalog_status": "TEXT",
    "set_catalog_set_name": "TEXT",
    "set_catalog_set_description": "TEXT",
    "set_catalog_set_state": "TEXT",
    "set_catalog_version_id": "TEXT",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS harvest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_url TEXT NOT NULL,
    metadata_prefix TEXT NOT NULL,
    set_spec TEXT,
    from_date TEXT,
    until_date TEXT,
    archive_root TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'interrupted', 'failed')
    ),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    checkpoint_token TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    set_catalog_generation_id INTEGER,
    set_catalog_generation INTEGER,
    set_catalog_status TEXT,
    set_catalog_set_name TEXT,
    set_catalog_set_description TEXT,
    set_catalog_set_state TEXT,
    set_catalog_version_id TEXT
);

CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    verb TEXT NOT NULL,
    request_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    archive_path TEXT NOT NULL,
    UNIQUE (run_id, page_number),
    FOREIGN KEY (run_id) REFERENCES harvest_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS records (
    identifier TEXT PRIMARY KEY,
    datestamp TEXT NOT NULL,
    set_specs TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    title TEXT NOT NULL DEFAULT '',
    creators TEXT NOT NULL DEFAULT '[]',
    subjects TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    contributors TEXT NOT NULL DEFAULT '[]',
    date TEXT NOT NULL DEFAULT '',
    resource_type TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL DEFAULT '',
    identifiers TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT '',
    relations TEXT NOT NULL DEFAULT '[]',
    coverage TEXT NOT NULL DEFAULT '[]',
    rights TEXT NOT NULL DEFAULT '',
    record_hash TEXT NOT NULL,
    first_harvested_at TEXT NOT NULL,
    last_harvested_at TEXT NOT NULL,
    last_run_id INTEGER NOT NULL,
    last_response_path TEXT,
    FOREIGN KEY (last_run_id) REFERENCES harvest_runs(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    event TEXT NOT NULL,
    identifier TEXT,
    detail TEXT,
    FOREIGN KEY (run_id) REFERENCES harvest_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    identifier TEXT,
    code TEXT,
    message TEXT NOT NULL,
    detail TEXT,
    FOREIGN KEY (run_id) REFERENCES harvest_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS set_catalog_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_url TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('staging', 'active', 'superseded', 'rejected')
    ),
    conflict_policy TEXT NOT NULL DEFAULT 'strict',
    set_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    activated_at TEXT,
    error TEXT,
    UNIQUE (repository_url, generation)
);

CREATE TABLE IF NOT EXISTS set_catalog_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    request_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    set_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (generation_id, page_number),
    FOREIGN KEY (generation_id) REFERENCES set_catalog_generations(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS set_catalog_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    set_spec TEXT NOT NULL,
    set_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('available', 'conflict', 'invalid')),
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_page INTEGER,
    last_page INTEGER,
    UNIQUE (generation_id, set_spec),
    FOREIGN KEY (generation_id) REFERENCES set_catalog_generations(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS set_catalog_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    set_spec TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    set_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (generation_id) REFERENCES set_catalog_generations(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at
    ON harvest_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_responses_run
    ON raw_responses(run_id, page_number);
CREATE INDEX IF NOT EXISTS idx_records_last_run
    ON records(last_run_id);
CREATE INDEX IF NOT EXISTS idx_events_run
    ON events(run_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_errors_run
    ON errors(run_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_catalog_generations_repo
    ON set_catalog_generations(repository_url, status, generation DESC);
CREATE INDEX IF NOT EXISTS idx_catalog_members_generation
    ON set_catalog_members(generation_id, set_spec);
CREATE INDEX IF NOT EXISTS idx_catalog_occurrences_generation
    ON set_catalog_occurrences(generation_id, set_spec, page_number);
"""
