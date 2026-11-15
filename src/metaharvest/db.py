"""SQLite ledger, idempotent record upserts, and evidence metadata."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db_schema import RUN_CATALOG_COLUMNS, SCHEMA_SQL, SCHEMA_VERSION
from .record_store import RecordStore
from .util import ensure_directory, iso_z, json_loads


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_directory(self.path.parent)
        self._connect_factory = self._connect
        self.records = RecordStore(self._connect_factory)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        """Public context manager for multi-statement atomic writes."""
        return self._connect()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA_SQL)
            self._migrate_run_catalog_columns(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("PRAGMA journal_mode = WAL")

    @staticmethod
    def _migrate_run_catalog_columns(connection: sqlite3.Connection) -> None:
        """Add catalog snapshot columns to databases created before v2."""
        existing = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(harvest_runs)").fetchall()
        }
        for column, column_type in RUN_CATALOG_COLUMNS.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE harvest_runs ADD COLUMN {column} {column_type}"
                )

    def create_run(
        self,
        *,
        repository_url: str,
        metadata_prefix: str,
        set_spec: str | None,
        from_date: str | None,
        until_date: str | None,
        archive_root: Path,
        config_hash: str,
        set_catalog_generation_id: int | None = None,
        set_catalog_generation: int | None = None,
        set_catalog_status: str | None = None,
        set_catalog_set_name: str | None = None,
        set_catalog_set_description: str | None = None,
        set_catalog_set_state: str | None = None,
        set_catalog_version_id: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO harvest_runs (
                    repository_url, metadata_prefix, set_spec, from_date,
                    until_date, archive_root, config_hash, status, started_at,
                    set_catalog_generation_id, set_catalog_generation,
                    set_catalog_status, set_catalog_set_name,
                    set_catalog_set_description, set_catalog_set_state,
                    set_catalog_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_url,
                    metadata_prefix,
                    set_spec,
                    from_date,
                    until_date,
                    str(archive_root),
                    config_hash,
                    iso_z(),
                    set_catalog_generation_id,
                    set_catalog_generation,
                    set_catalog_status,
                    set_catalog_set_name,
                    set_catalog_set_description,
                    set_catalog_set_state,
                    set_catalog_version_id,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM harvest_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_run(self, status: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM harvest_runs"
        parameters: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_progress(
        self,
        run_id: int,
        *,
        page_count: int,
        record_count: int,
        created_count: int,
        updated_count: int,
        unchanged_count: int,
        deleted_count: int,
        error_count: int,
        checkpoint_token: str | None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE harvest_runs SET
                    page_count = ?, record_count = ?, created_count = ?,
                    updated_count = ?, unchanged_count = ?, deleted_count = ?,
                    error_count = ?, checkpoint_token = ?, status = 'running'
                WHERE id = ?
                """,
                (
                    page_count,
                    record_count,
                    created_count,
                    updated_count,
                    unchanged_count,
                    deleted_count,
                    error_count,
                    checkpoint_token,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"收割运行不存在: {run_id}")
            connection.commit()

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        finished_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        checkpoint = "NULL" if status == "completed" else "checkpoint_token"
        sql = f"""
            UPDATE harvest_runs
            SET status = ?, finished_at = ?, last_error = ?,
                checkpoint_token = {checkpoint}
            WHERE id = ?
        """
        with self._connect() as connection:
            connection.execute(
                sql, (status, finished_at or iso_z(), last_error, run_id)
            )
            connection.commit()

    def add_raw_response(
        self,
        run_id: int,
        *,
        page_number: int,
        verb: str,
        request_url: str,
        fetched_at: str,
        http_status: int,
        sha256: str,
        size_bytes: int,
        archive_path: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO raw_responses (
                    run_id, page_number, verb, request_url, fetched_at,
                    http_status, sha256, size_bytes, archive_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, page_number) DO UPDATE SET
                    verb = excluded.verb,
                    request_url = excluded.request_url,
                    fetched_at = excluded.fetched_at,
                    http_status = excluded.http_status,
                    sha256 = excluded.sha256,
                    size_bytes = excluded.size_bytes,
                    archive_path = excluded.archive_path
                """,
                (
                    run_id,
                    page_number,
                    verb,
                    request_url,
                    fetched_at,
                    http_status,
                    sha256,
                    size_bytes,
                    archive_path,
                ),
            )
            connection.commit()

    def add_event(
        self,
        run_id: int,
        event: str,
        *,
        level: str = "info",
        identifier: str | None = None,
        detail: Any = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events (run_id, occurred_at, level, event, identifier, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    iso_z(),
                    level,
                    event,
                    identifier,
                    _json(detail) if detail is not None else None,
                ),
            )
            connection.commit()

    def add_error(
        self,
        run_id: int,
        *,
        scope: str,
        message: str,
        code: str | None = None,
        identifier: str | None = None,
        detail: Any = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO errors (
                    run_id, occurred_at, scope, identifier, code, message, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    iso_z(),
                    scope,
                    identifier,
                    code,
                    message,
                    _json(detail) if detail is not None else None,
                ),
            )
            connection.commit()

    def raw_responses_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM raw_responses WHERE run_id = ? ORDER BY page_number",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def events_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["detail"] = json_loads(row.get("detail"), None)
        return result

    def errors_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM errors WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["detail"] = json_loads(row.get("detail"), None)
        return result
