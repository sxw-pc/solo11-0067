"""SQLite schema kept separate so database logic stays focused.

Schema history
--------------
v1
    ``records`` held one mutable row per OAI identifier and ``last_run_id``.
    A run's record set could not be reconstructed after later runs touched a
    row, and ``for_run`` was only a "last observed by" filter.

v2
    ``record_versions`` is an append-only log of *changed* observations.
    Every row records the effective content of one identifier after the
    observation, together with the run that observed it.  ``harvest_runs``
    gains a frozen ``snapshot_watermark`` (the highest ``record_versions.seq``
    at the moment the run reached a terminal state) plus a snapshot status.
    A full snapshot for a run is rebuilt by taking, per identifier, the newest
    version whose ``seq`` is not greater than the run's watermark.

    ``records`` is kept as the latest-state cache (``get`` and ``for_run``);
    snapshots never read it, so a later run cannot rewrite an older snapshot.
"""

from __future__ import annotations

SCHEMA_VERSION = 2

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
    -- 'none'     : no snapshot was ever frozen for this run
    -- 'partial'  : interrupted/failed run, frozen but not complete
    -- 'complete' : completed run, frozen and complete
    -- 'incomplete': legacy completed run whose earlier history was lost
    snapshot_status TEXT NOT NULL DEFAULT 'none' CHECK (
        snapshot_status IN ('none', 'partial', 'complete', 'incomplete')
    ),
    snapshot_watermark INTEGER,
    snapshot_base_watermark INTEGER,
    snapshot_record_count INTEGER NOT NULL DEFAULT 0,
    snapshot_frozen_at TEXT
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

CREATE TABLE IF NOT EXISTS record_versions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    identifier TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('created', 'updated', 'deleted', 'baseline')
    ),
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
    response_path TEXT,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES harvest_runs(id) ON DELETE CASCADE
);

-- One small row per (run, identifier) the run actually observed.  This lets a
-- snapshot include unchanged observations (whose content lives in an older
-- version) without duplicating content per run.  The captured ``version_seq``
-- pins the exact immutable version the run saw, so the run's view cannot drift
-- even if another run later changes the record.  The primary key also makes a
-- retried checkpoint page idempotent: one run never lists a record twice.
CREATE TABLE IF NOT EXISTS run_observations (
    run_id INTEGER NOT NULL,
    identifier TEXT NOT NULL,
    version_seq INTEGER NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('created', 'updated', 'deleted', 'unchanged', 'baseline')
    ),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, identifier),
    FOREIGN KEY (run_id) REFERENCES harvest_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (version_seq) REFERENCES record_versions(seq)
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

CREATE INDEX IF NOT EXISTS idx_runs_started_at
    ON harvest_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_responses_run
    ON raw_responses(run_id, page_number);
CREATE INDEX IF NOT EXISTS idx_records_last_run
    ON records(last_run_id);
CREATE INDEX IF NOT EXISTS idx_record_versions_identifier
    ON record_versions(identifier, seq DESC);
CREATE INDEX IF NOT EXISTS idx_record_versions_run
    ON record_versions(run_id, identifier);
CREATE INDEX IF NOT EXISTS idx_record_versions_seq
    ON record_versions(seq);
CREATE INDEX IF NOT EXISTS idx_run_observations_identifier
    ON run_observations(identifier);
CREATE INDEX IF NOT EXISTS idx_events_run
    ON events(run_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_errors_run
    ON errors(run_id, occurred_at);
"""

# The pre-version-history schema, kept verbatim so migrations and upgrade tests
# can build a realistic v1 database.
SCHEMA_V1_SQL = """
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
    last_error TEXT
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
"""
