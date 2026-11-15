"""Idempotent record persistence plus immutable per-run history.

``records``
    One mutable cache row per OAI identifier holding the latest state.  It is
    what :meth:`RecordStore.get` and :meth:`RecordStore.for_run` expose, and it
    is *not* a historical view: a later run overwrites it.

``record_versions``
    Append-only log of *changed* effective states.  A row is appended only when
    an observation changes an identifier's state (or introduces it).  Rows are
    never updated by later runs, so a historical version cannot drift.

``run_observations``
    One small ``(run_id, identifier)`` row per identifier a run actually saw,
    pinning the ``record_versions.seq`` visible at that moment.  This is what
    lets a run's snapshot include unchanged records without copying content
    once per run.

A run's snapshot is rebuilt, never materialised per run:

    effective(run) = newest version at or below the run's base watermark,
                     overridden by the exact version each observation pinned.

Storage grows with the number of *changes* plus one small row per observed
``(run, identifier)``; reads cost one grouped index scan per snapshot.  This is
strictly cheaper than copying every record for every completed run, and it
makes the result independent of when (or how many times) it is read.

Deletion is a tombstone version (``is_deleted = 1`` with empty metadata).
Earlier active versions stay reachable through
:meth:`RecordStore.versions_for_identifier`; the tombstone is what the snapshot
shows as the effective state.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable

from .normalize import NormalizedRecord
from .util import iso_z, json_loads

RECORD_JSON_FIELDS = (
    "set_specs",
    "creators",
    "subjects",
    "contributors",
    "identifiers",
    "sources",
    "relations",
    "coverage",
)

# Columns shared by ``records`` and ``record_versions`` (minus the differing
# bookkeeping columns such as first_harvested_at / observed_at / outcome).
_CONTENT_COLUMNS = (
    "datestamp",
    "set_specs",
    "is_deleted",
    "title",
    "creators",
    "subjects",
    "description",
    "publisher",
    "contributors",
    "date",
    "resource_type",
    "format",
    "identifiers",
    "sources",
    "language",
    "relations",
    "coverage",
    "rights",
    "record_hash",
)

# Content columns appended to ``record_versions`` verbatim.
VERSION_CONTENT_COLUMNS = _CONTENT_COLUMNS

SNAPSHOT_COMPLETE = "complete"
SNAPSHOT_PARTIAL = "partial"
SNAPSHOT_INCOMPLETE = "incomplete"
SNAPSHOT_RUNNING = "running"
SNAPSHOT_UNAVAILABLE = "unavailable"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode_record(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for field in RECORD_JSON_FIELDS:
        value[field] = json_loads(value.get(field), [])
    value["deleted"] = bool(value.pop("is_deleted", 0))
    return value


def decode_version(row: sqlite3.Row) -> dict[str, Any]:
    value = decode_record(row)
    value["version_run_id"] = value.pop("run_id")
    value["version_seq"] = value.pop("seq")
    value["changed_in_run"] = bool(value.pop("changed_in_run", 0))
    value["observed_at"] = value.pop("observed_at", None)
    value["response_path"] = value.pop("response_path", None)
    return value


def _content_values(record: NormalizedRecord) -> tuple[Any, ...]:
    return (
        record.datestamp,
        _json(list(record.set_specs)),
        int(record.deleted),
        record.title,
        _json(list(record.creators)),
        _json(list(record.subjects)),
        record.description,
        record.publisher,
        _json(list(record.contributors)),
        record.date,
        record.resource_type,
        record.format,
        _json(list(record.identifiers)),
        _json(list(record.sources)),
        record.language,
        _json(list(record.relations)),
        _json(list(record.coverage)),
        record.rights,
        record.record_hash,
    )


@dataclass(frozen=True)
class RecordSnapshot:
    """Effective state of every known identifier at one run's watermark."""

    run_id: int
    status: str
    complete: bool
    watermark: int | None
    base_watermark: int | None
    frozen_at: str | None
    records: tuple[dict[str, Any], ...]

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def deleted_count(self) -> int:
        return sum(1 for record in self.records if record["deleted"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "complete": self.complete,
            "watermark": self.watermark,
            "base_watermark": self.base_watermark,
            "frozen_at": self.frozen_at,
            "record_count": self.record_count,
            "deleted_count": self.deleted_count,
            "records": list(self.records),
        }


class RecordStore:
    def __init__(
        self, connect: Callable[[], AbstractContextManager[sqlite3.Connection]]
    ) -> None:
        self._connect = connect

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def upsert(
        self,
        record: NormalizedRecord,
        *,
        run_id: int,
        response_path: str,
        seen_at: str | None = None,
    ) -> str:
        data = _content_values(record)
        observed = seen_at or iso_z()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT record_hash, is_deleted FROM records WHERE identifier = ?",
                (record.identifier,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    f"""
                    INSERT INTO records (
                        identifier, {', '.join(_CONTENT_COLUMNS)},
                        first_harvested_at, last_harvested_at, last_run_id,
                        last_response_path
                    ) VALUES ({', '.join('?' for _ in range(len(_CONTENT_COLUMNS) + 5))})
                    """,
                    (
                        record.identifier,
                        *data,
                        observed,
                        observed,
                        run_id,
                        response_path,
                    ),
                )
                outcome = "deleted" if record.deleted else "created"
                version_seq = self._append_version(
                    connection,
                    record,
                    run_id=run_id,
                    outcome=outcome,
                    response_path=response_path,
                    observed=observed,
                )
                self._append_observation(
                    connection,
                    identifier=record.identifier,
                    run_id=run_id,
                    outcome=outcome,
                    observed=observed,
                    version_seq=version_seq,
                )
                return outcome
            unchanged = (
                existing["record_hash"] == record.record_hash
                and bool(existing["is_deleted"]) == record.deleted
            )
            assignments = ", ".join(f"{column} = ?" for column in _CONTENT_COLUMNS)
            connection.execute(
                f"""
                UPDATE records SET {assignments},
                    last_harvested_at = ?, last_run_id = ?, last_response_path = ?
                WHERE identifier = ?
                """,
                (*data, observed, run_id, response_path, record.identifier),
            )
            if unchanged:
                # The observation did not change the effective state: no
                # version row is written.  Recording last_run_id here keeps the
                # legacy ``for_run`` touch list working.
                self._append_observation(
                    connection,
                    identifier=record.identifier,
                    run_id=run_id,
                    outcome="unchanged",
                    observed=observed,
                    record_hash=record.record_hash,
                    deleted=record.deleted,
                )
                return "unchanged"
            outcome = "deleted" if record.deleted else "updated"
            version_seq = self._append_version(
                connection,
                record,
                run_id=run_id,
                outcome=outcome,
                response_path=response_path,
                observed=observed,
            )
            self._append_observation(
                connection,
                identifier=record.identifier,
                run_id=run_id,
                outcome=outcome,
                observed=observed,
                version_seq=version_seq,
            )
            return outcome

    def _append_version(
        self,
        connection: sqlite3.Connection,
        record: NormalizedRecord,
        *,
        run_id: int,
        outcome: str,
        response_path: str,
        observed: str,
    ) -> int:
        """Append one immutable version and return its ``seq``.

        Versions are strictly append-only: a resumed run that reprocesses a
        page appends a new row rather than mutating an old one, so a version
        another run already pinned can never change.  Read paths take the
        newest version per ``(run_id, identifier)``, which keeps the history
        view free of duplicates while preserving the append-only invariant.
        """
        cursor = connection.execute(
            f"""
            INSERT INTO record_versions (
                run_id, identifier, outcome, {', '.join(_CONTENT_COLUMNS)},
                response_path, observed_at
            ) VALUES ({', '.join('?' for _ in range(len(_CONTENT_COLUMNS) + 5))})
            """,
            (
                run_id,
                record.identifier,
                outcome,
                *_content_values(record),
                response_path,
                observed,
            ),
        )
        return int(cursor.lastrowid)

    def _append_observation(
        self,
        connection: sqlite3.Connection,
        *,
        identifier: str,
        run_id: int,
        outcome: str,
        observed: str,
        version_seq: int | None = None,
        record_hash: str | None = None,
        deleted: bool | None = None,
    ) -> None:
        """Record that a run observed an identifier, pinning the version seen.

        For a changed observation the version is this run's own new row.  For
        an unchanged one the pin is the newest version whose content hash and
        deletion flag match what the run just saw.  Pinning by content (rather
        than "latest at query time") means a concurrent or later run cannot
        retroactively change what this run's history view shows, and retrying
        the same checkpoint page is idempotent: the ``(run_id, identifier)``
        primary key keeps one row, so history never duplicates a record.
        """
        if version_seq is None:
            # Pin the exact state the run observed.  Priority: this run's own
            # version (a retried/reprocessed page) first, then a version
            # visible at the run's base watermark, then any matching version.
            #
            # The last case covers a resumed run observing a record another run
            # created or updated while this run was interrupted.  It is safe to
            # pin any version because versions are append-only: a later resume
            # appends a new row and never mutates the pinned one, so the pin
            # cannot drift.
            candidates = connection.execute(
                """
                SELECT v.seq FROM record_versions AS v
                JOIN harvest_runs AS r ON r.id = ?
                WHERE v.identifier = ?
                  AND v.record_hash = ?
                  AND v.is_deleted = ?
                ORDER BY
                    CASE
                        WHEN v.run_id = ? THEN 0
                        WHEN v.seq <= COALESCE(r.snapshot_base_watermark, 0)
                            THEN 1
                        ELSE 2
                    END,
                    v.seq DESC
                LIMIT 1
                """,
                (
                    run_id,
                    identifier,
                    record_hash,
                    int(bool(deleted)),
                    run_id,
                ),
            ).fetchone()
            if candidates is None:
                candidates = connection.execute(
                    """
                    SELECT v.seq FROM record_versions AS v
                    JOIN harvest_runs AS r ON r.id = ?
                    WHERE v.identifier = ?
                    ORDER BY
                        CASE
                            WHEN v.run_id = ? THEN 0
                            WHEN v.seq <= COALESCE(r.snapshot_base_watermark, 0)
                                THEN 1
                            ELSE 2
                        END,
                        v.seq DESC
                    LIMIT 1
                    """,
                    (run_id, identifier, run_id),
                ).fetchone()
            if candidates is None:
                # Defensive: an unchanged record with no prior version should
                # be impossible because ``records`` had a matching hash.
                return
            version_seq = int(candidates["seq"])
        connection.execute(
            """
            INSERT INTO run_observations (
                run_id, identifier, version_seq, outcome, observed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, identifier) DO UPDATE SET
                -- A changed observation always wins: it carries a new version
                -- and is this run's effective result.  An unchanged
                -- re-observation (a retried checkpoint page) must not erase a
                -- change this same run already made.
                version_seq = CASE
                    WHEN excluded.outcome != 'unchanged'
                    THEN excluded.version_seq
                    ELSE run_observations.version_seq
                END,
                outcome = CASE
                    WHEN excluded.outcome != 'unchanged'
                    THEN excluded.outcome
                    ELSE run_observations.outcome
                END,
                observed_at = excluded.observed_at
            """,
            (run_id, identifier, version_seq, outcome, observed),
        )

    # ------------------------------------------------------------------
    # Reads: latest cache
    # ------------------------------------------------------------------
    def get(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE identifier = ?", (identifier,)
            ).fetchone()
        return decode_record(row) if row else None

    def for_run(self, run_id: int) -> list[dict[str, Any]]:
        """Latest cache rows last *observed* by a run.

        This is a touch list, not a historical snapshot: unchanged observations
        still update ``last_run_id``, and later runs overwrite the row.  Use
        :meth:`snapshot_for_run` for reproducible history.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE last_run_id = ? ORDER BY identifier",
                (run_id,),
            ).fetchall()
        return [decode_record(row) for row in rows]

    # ------------------------------------------------------------------
    # Reads: immutable history
    # ------------------------------------------------------------------
    def changes_for_run(self, run_id: int) -> list[dict[str, Any]]:
        """Records whose effective state changed during ``run_id``.

        A retried page can append several versions for the same identifier in
        one run; only the newest per identifier is the run's effective change.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT v.*, 1 AS changed_in_run
                FROM record_versions AS v
                JOIN (
                    SELECT identifier, MAX(seq) AS seq
                    FROM record_versions
                    WHERE run_id = ? AND outcome != 'baseline'
                    GROUP BY identifier
                ) AS latest ON latest.seq = v.seq
                ORDER BY v.identifier
                """,
                (run_id,),
            ).fetchall()
        return [decode_version(row) for row in rows]

    def versions_for_identifier(self, identifier: str) -> list[dict[str, Any]]:
        """Full retained version chain for one identifier, oldest first.

        Includes every append, so a retried page is visible as repeated
        attempts.  Snapshots deduplicate per run by taking the newest version
        each run appended.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *, 0 AS changed_in_run FROM record_versions
                WHERE identifier = ? ORDER BY seq
                """,
                (identifier,),
            ).fetchall()
        return [decode_version(row) for row in rows]

    def snapshot_for_run(self, run_id: int) -> RecordSnapshot | None:
        """Rebuild the effective record set for one run, deterministically.

        Reconstruction rule (independent of when the query runs):

        1. Start from the newest version of every identifier at or below the
           run's **base watermark** — the highest ``seq`` that existed when the
           run was created.  All identifiers not observed by the run inherit
           exactly that earlier state.
        2. For every identifier the run observed (``run_observations``), use the
           version pinned at observation time.  For a changed record that is
           the run's own version; for an unchanged one it is the version whose
           content matched at that moment.

        Both inputs are immutable: versions are append-only, and observations
        pin a concrete ``seq``.  Later runs cannot alter either, so a snapshot
        cannot drift.  ``run_observations`` is keyed by ``(run_id, identifier)``
        so one run never lists the same record twice, no matter how many times
        a checkpoint page is retried.  An interrupted run resumed after another
        run finished still reflects only what it inherited and what it observed.

        Returns ``None`` when the run does not exist.  ``complete`` is true
        only for a completed run; interrupted/failed runs are ``partial``;
        legacy runs whose history predates the version log are ``incomplete``.
        """
        with self._connect() as connection:
            run = connection.execute(
                """
                SELECT id, status, snapshot_status, snapshot_watermark,
                       snapshot_base_watermark, snapshot_frozen_at
                FROM harvest_runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                return None
            status = str(run["snapshot_status"] or "none")
            watermark = run["snapshot_watermark"]
            if watermark is None:
                if status == SNAPSHOT_INCOMPLETE:
                    return RecordSnapshot(
                        run_id=run_id,
                        status=SNAPSHOT_INCOMPLETE,
                        complete=False,
                        watermark=None,
                        base_watermark=run["snapshot_base_watermark"],
                        frozen_at=run["snapshot_frozen_at"],
                        records=(),
                    )
                if str(run["status"]) != "running":
                    return RecordSnapshot(
                        run_id=run_id,
                        status=SNAPSHOT_UNAVAILABLE,
                        complete=False,
                        watermark=None,
                        base_watermark=run["snapshot_base_watermark"],
                        frozen_at=run["snapshot_frozen_at"],
                        records=(),
                    )
                # A running run has no frozen boundary.  Expose a clearly
                # non-complete live view so callers can inspect progress, but
                # never let it answer as a finished snapshot.
                base_watermark = run["snapshot_base_watermark"]
                if base_watermark is None:
                    base_watermark = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(seq), 0) FROM record_versions"
                        ).fetchone()[0]
                    )
                rows = self._effective_rows(
                    connection, int(base_watermark), run_id
                )
                return RecordSnapshot(
                    run_id=run_id,
                    status=SNAPSHOT_RUNNING,
                    complete=False,
                    watermark=None,
                    base_watermark=int(base_watermark),
                    frozen_at=None,
                    records=tuple(
                        {
                            **decode_version(row),
                            "changed_in_run": row["run_outcome"]
                            in {"created", "updated", "deleted"},
                            "run_outcome": row["run_outcome"],
                        }
                        for row in rows
                    ),
                )

            if status == SNAPSHOT_INCOMPLETE:
                # Pre-migration history cannot be reconstructed.  Returning
                # the surviving baselines would silently present later state as
                # this run's; refuse instead.
                return RecordSnapshot(
                    run_id=run_id,
                    status=SNAPSHOT_INCOMPLETE,
                    complete=False,
                    watermark=int(watermark),
                    base_watermark=run["snapshot_base_watermark"],
                    frozen_at=run["snapshot_frozen_at"],
                    records=(),
                )

            # A run created before base watermarks existed falls back to the
            # frozen (terminal) watermark.
            base_watermark = run["snapshot_base_watermark"]
            if base_watermark is None:
                base_watermark = watermark
            rows = self._effective_rows(connection, int(base_watermark), run_id)

        if status == SNAPSHOT_COMPLETE:
            reported = SNAPSHOT_COMPLETE
        elif status == SNAPSHOT_PARTIAL:
            reported = SNAPSHOT_PARTIAL
        else:
            reported = SNAPSHOT_UNAVAILABLE
        records = tuple(
            {
                **decode_version(row),
                "changed_in_run": row["run_outcome"]
                in {"created", "updated", "deleted"},
                "run_outcome": row["run_outcome"],
            }
            for row in rows
        )
        return RecordSnapshot(
            run_id=run_id,
            status=reported,
            complete=reported == SNAPSHOT_COMPLETE,
            watermark=int(watermark),
            base_watermark=int(base_watermark),
            frozen_at=run["snapshot_frozen_at"],
            records=records,
        )

    @staticmethod
    def _effective_rows(
        connection: sqlite3.Connection, base_watermark: int, run_id: int
    ) -> list[sqlite3.Row]:
        """Effective (run_outcome, version row) pairs for one run view.

        Identifiers the run observed use the version pinned at observation
        time; everything else inherits the newest version at or below the base
        watermark.  Both are immutable, so the result cannot drift.
        """
        return connection.execute(
            """
            WITH base AS (
                SELECT identifier, MAX(seq) AS seq
                FROM record_versions
                WHERE seq <= ?
                GROUP BY identifier
            ),
            observed AS (
                SELECT identifier, version_seq AS seq, outcome
                FROM run_observations
                WHERE run_id = ?
            ),
            effective AS (
                SELECT identifier, seq FROM base
                WHERE identifier NOT IN (SELECT identifier FROM observed)
                UNION ALL
                SELECT identifier, seq FROM observed
            )
            SELECT v.*, COALESCE(o.outcome, 'unchanged') AS run_outcome
            FROM effective AS e
            JOIN record_versions AS v ON v.seq = e.seq
            LEFT JOIN run_observations AS o
                ON o.run_id = ? AND o.identifier = e.identifier
            ORDER BY e.identifier
            """,
            (base_watermark, run_id, run_id),
        ).fetchall()

__all__ = [
    "RecordSnapshot",
    "RecordStore",
    "SNAPSHOT_COMPLETE",
    "SNAPSHOT_INCOMPLETE",
    "SNAPSHOT_PARTIAL",
    "SNAPSHOT_RUNNING",
    "SNAPSHOT_UNAVAILABLE",
    "VERSION_CONTENT_COLUMNS",
    "decode_record",
    "decode_version",
]
