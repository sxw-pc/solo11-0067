"""Retry configuration validation and ledger migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from metaharvest.config import ConfigError, RetryConfig, load_harvest_config
from metaharvest.db import Database

from conftest import HARVEST_CONFIG_PATH

BASE = """
repository:
  base_url: "http://127.0.0.1:8787/oai"
storage:
  db_path: "{tmp}/h.sqlite3"
  archive_dir: "{tmp}/archive"
  report_dir: "{tmp}/reports"
  audit_log: "{tmp}/audit.jsonl"
"""


def _write(tmp_path: Path, retry: str) -> Path:
    path = tmp_path / "harvest.yml"
    path.write_text(
        BASE.format(tmp=tmp_path) + "\nretry:\n" + retry, encoding="utf-8"
    )
    return path


def test_default_retry_config_is_deferred():
    retry = load_harvest_config(HARVEST_CONFIG_PATH).retry
    assert retry.policy == "deferred"
    assert retry.max_attempts == 3
    assert retry.total_wait_seconds >= retry.max_retry_after_seconds
    assert retry.deferred_wait_seconds <= retry.total_wait_seconds


def test_retry_config_parses_all_fields(tmp_path: Path):
    config = load_harvest_config(
        _write(
            tmp_path,
            "  policy: inline\n"
            "  max_attempts: 4\n"
            "  default_wait_seconds: 1.5\n"
            "  max_retry_after_seconds: 30\n"
            "  total_wait_seconds: 60\n"
            "  deferred_wait_seconds: 2\n",
        )
    )
    assert config.retry == RetryConfig(
        policy="inline",
        max_attempts=4,
        default_wait_seconds=1.5,
        max_retry_after_seconds=30,
        total_wait_seconds=60,
        deferred_wait_seconds=2,
    )


@pytest.mark.parametrize(
    ("retry", "message"),
    [
        ("  policy: sometimes\n", "retry.policy"),
        ("  max_attempts: 0\n", "max_attempts"),
        ("  total_wait_seconds: -1\n", "不得为负数"),
        (
            "  total_wait_seconds: 10\n  max_retry_after_seconds: 20\n",
            "max_retry_after_seconds",
        ),
        (
            "  total_wait_seconds: 10\n  deferred_wait_seconds: 20\n",
            "deferred_wait_seconds",
        ),
        (
            "  max_retry_after_seconds: 5\n  default_wait_seconds: 10\n",
            "default_wait_seconds",
        ),
    ],
)
def test_retry_config_rejects_invalid_values(tmp_path: Path, retry: str, message: str):
    with pytest.raises(ConfigError, match=message):
        load_harvest_config(_write(tmp_path, retry))


def test_existing_v1_ledger_is_migrated_without_data_loss(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE harvest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository_url TEXT NOT NULL,
            metadata_prefix TEXT NOT NULL,
            set_spec TEXT,
            from_date TEXT,
            until_date TEXT,
            archive_root TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            status TEXT NOT NULL,
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
        INSERT INTO harvest_runs (
            repository_url, metadata_prefix, archive_root, config_hash,
            status, started_at, page_count
        ) VALUES ('http://127.0.0.1/oai', 'oai_dc', '/tmp/archive', 'h',
                  'interrupted', '2026-01-01T00:00:00Z', 2);
        PRAGMA user_version = 1;
        """
    )
    connection.commit()
    connection.close()

    db = Database(db_path)
    run = db.get_run(1)
    assert run is not None
    assert run["page_count"] == 2
    assert run["retry_not_before"] is None
    assert run["retry_policy"] is None
    assert run["interrupted_reason"] is None
    assert db.request_attempts_for_run(1) == []

    # Existing rows can be updated with the new columns after migration.
    db.mark_interrupted(1, reason="retry_deferred", retry_not_before="2030-01-01T00:00:00Z")
    run = db.get_run(1)
    assert run["retry_not_before"] == "2030-01-01T00:00:00Z"
    assert run["interrupted_reason"] == "retry_deferred"
