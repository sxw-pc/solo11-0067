"""SQLite ledger, idempotent record upserts, and evidence metadata."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db_schema import SCHEMA_SQL, SCHEMA_VERSION
from .record_store import (
    SNAPSHOT_COMPLETE,
    SNAPSHOT_INCOMPLETE,
    SNAPSHOT_PARTIAL,
    RecordSnapshot,
    RecordStore,
    VERSION_CONTENT_COLUMNS as _VERSION_CONTENT_COLUMNS,
)
from .util import ensure_directory, iso_z, json_loads

# Columns added to ``harvest_runs`` in schema v2. Kept here so the migration can
# ALTER an existing v1 table without recreating it.
_SNAPSHOT_COLUMNS = (
    (
        "snapshot_status",
        "TEXT NOT NULL DEFAULT 'none' CHECK ("
        "snapshot_status IN ('none', 'partial', 'complete', 'incomplete'))",
    ),
    ("snapshot_watermark", "INTEGER"),
    ("snapshot_base_watermark", "INTEGER"),
    ("snapshot_record_count", "INTEGER NOT NULL DEFAULT 0"),
    ("snapshot_frozen_at", "TEXT"),
)



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

    def initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                self._migrate(connection)
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("PRAGMA journal_mode = WAL")

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Upgrade a v1 database in place without losing ledger or records.

        The v1 ``records`` table is a destructive latest-state cache: for an
        identifier touched by multiple runs, only the newest content survives.
        History older than the newest touch therefore cannot be invented.

        Strategy:

        * Fresh database: create the current schema and stop.
        * Add the v2 snapshot columns to ``harvest_runs`` via ``ALTER TABLE``.
        * Seed ``record_versions`` with one ``baseline`` row per surviving
          ``records`` row, attributed to that row's ``last_run_id``.  This is
          the only version we can prove; intermediate states are never
          fabricated.
        * A completed *full* harvest that is still the last observer of every
          surviving row provably equals the current cache, so it is frozen as
          ``complete``.  Every other completed run is frozen as ``incomplete``
          and its snapshot returns no records.  Runs with no attributable rows
          read as ``unavailable``.  Nothing is ever silently labelled complete.

        ``snapshot_base_watermark`` is set to ``0`` for every migrated run, so
        the base state equals "nothing before the version log" and only the
        seeded baselines replay — never the mutable ``records`` cache.
        """
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "records" not in tables:
            connection.executescript(SCHEMA_SQL)
            connection.commit()
            return

        connection.executescript(SCHEMA_SQL)
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(harvest_runs)")
        }
        for name, definition in _SNAPSHOT_COLUMNS:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE harvest_runs ADD COLUMN {name} {definition}"
                )

        already_seeded = connection.execute(
            "SELECT 1 FROM record_versions LIMIT 1"
        ).fetchone()
        if already_seeded:
            return

        runs = connection.execute(
            "SELECT id, status, from_date, set_spec, finished_at "
            "FROM harvest_runs ORDER BY id"
        ).fetchall()
        records = connection.execute("SELECT * FROM records").fetchall()
        by_run: dict[int, list[sqlite3.Row]] = {}
        for row in records:
            by_run.setdefault(int(row["last_run_id"]), []).append(row)

        for run in runs:
            run_id = int(run["id"])
            rows = by_run.get(run_id, [])
            watermark = 0
            for row in rows:
                cursor = connection.execute(
                    f"""
                    INSERT INTO record_versions (
                        run_id, identifier, outcome, {", ".join(_VERSION_CONTENT_COLUMNS)},
                        response_path, observed_at
                    ) VALUES ({", ".join("?" for _ in range(len(_VERSION_CONTENT_COLUMNS) + 5))})
                    """,
                    (
                        run_id,
                        row["identifier"],
                        "baseline",
                        *(row[column] for column in _VERSION_CONTENT_COLUMNS),
                        row["last_response_path"],
                        row["last_harvested_at"],
                    ),
                )
                seq = int(cursor.lastrowid or 0)
                watermark = max(watermark, seq)
                connection.execute(
                    """
                    INSERT INTO run_observations (
                        run_id, identifier, version_seq, outcome, observed_at
                    ) VALUES (?, ?, ?, 'baseline', ?)
                    """,
                    (run_id, row["identifier"], seq, row["last_harvested_at"]),
                )

            is_completed = run["status"] == "completed"
            if not is_completed:
                snapshot_status = SNAPSHOT_PARTIAL if watermark else "none"
            elif watermark == 0:
                snapshot_status = SNAPSHOT_INCOMPLETE
            else:
                full_harvest = (
                    run["from_date"] is None and run["set_spec"] is None
                )
                last_observer_of_all = len(rows) == len(records)
                snapshot_status = (
                    SNAPSHOT_COMPLETE
                    if full_harvest and last_observer_of_all
                    else SNAPSHOT_INCOMPLETE
                )
            connection.execute(
                """
                UPDATE harvest_runs SET
                    snapshot_status = ?, snapshot_watermark = ?,
                    snapshot_base_watermark = ?,
                    snapshot_record_count = ?, snapshot_frozen_at = ?
                WHERE id = ?
                """,
                (
                    snapshot_status,
                    watermark or None,
                    0,
                    # Only a provably complete legacy snapshot has a countable
                    # effective set; incomplete/unavailable ones report 0.
                    len(rows) if snapshot_status == SNAPSHOT_COMPLETE else 0,
                    run["finished_at"] or iso_z(),
                    run_id,
                ),
            )
        connection.commit()

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
    ) -> int:
        """Create a run and record the version watermark it inherited.

        ``snapshot_base_watermark`` is the highest ``seq`` at creation time.
        The run's historical view is built from that base plus its own
        observations, so another run finishing while this one is interrupted
        cannot leak into it and a resumed run remains reproducible.
        """
        with self._connect() as connection:
            base_watermark = int(
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM record_versions"
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO harvest_runs (
                    repository_url, metadata_prefix, set_spec, from_date,
                    until_date, archive_root, config_hash, status, started_at,
                    snapshot_base_watermark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
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
                    base_watermark,
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
        """Mark a run terminal and freeze its historical snapshot in one commit.

        The watermark (highest ``record_versions.seq`` right now) is recorded
        only for terminal states.  A ``running`` run has no frozen view, so
        versions written by later runs cannot leak into a run that never
        finished.  ``completed`` freezes as ``complete``; ``interrupted`` and
        ``failed`` freeze as ``partial``.
        """
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
            if status in {"completed", "interrupted", "failed"}:
                self._freeze_within(
                    connection,
                    run_id,
                    status=status,
                    frozen_at=finished_at or iso_z(),
                )
            connection.commit()

    def freeze_snapshot(
        self, run_id: int, *, status: str, finished_at: str | None = None
    ) -> None:
        frozen_at = finished_at or iso_z()
        with self._connect() as connection:
            self._freeze_within(
                connection, run_id, status=status, frozen_at=frozen_at
            )
            connection.commit()

    @staticmethod
    def _freeze_within(
        connection: sqlite3.Connection,
        run_id: int,
        *,
        status: str,
        frozen_at: str,
    ) -> None:
        """Freeze the run's snapshot boundary.

        The snapshot itself is reconstructed on read from
        ``snapshot_base_watermark`` plus this run's own versions, so no copies
        are stored.  ``snapshot_watermark`` records the highest ``seq`` that
        existed at the moment the run reached a terminal state; it is the
        run's exclusive upper bound for replay and lets a later reader prove
        which versions were visible to the run.  ``snapshot_record_count``
        counts the resulting effective set so the ledger shows its size.
        """
        snapshot_status = (
            SNAPSHOT_COMPLETE if status == "completed" else SNAPSHOT_PARTIAL
        )
        run = connection.execute(
            "SELECT snapshot_base_watermark FROM harvest_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        base_watermark = (
            int(run["snapshot_base_watermark"])
            if run is not None and run["snapshot_base_watermark"] is not None
            else 0
        )
        watermark = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM record_versions"
            ).fetchone()[0]
        )
        count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT identifier FROM (
                        SELECT identifier FROM record_versions
                        WHERE seq <= ?
                        UNION
                        SELECT identifier FROM run_observations WHERE run_id = ?
                    )
                )
                """,
                (base_watermark, run_id),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE harvest_runs SET
                snapshot_status = ?, snapshot_watermark = ?,
                snapshot_record_count = ?, snapshot_frozen_at = ?
            WHERE id = ?
            """,
            (snapshot_status, watermark, count, frozen_at, run_id),
        )

    def snapshot_for_run(self, run_id: int) -> RecordSnapshot | None:
        return self.records.snapshot_for_run(run_id)

    def changes_for_run(self, run_id: int) -> list[dict[str, Any]]:
        return self.records.changes_for_run(run_id)

    def versions_for_identifier(self, identifier: str) -> list[dict[str, Any]]:
        return self.records.versions_for_identifier(identifier)

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
