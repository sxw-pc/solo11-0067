"""Idempotent normalized-record persistence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any, Callable

from .language import localized_to_json_values
from .normalize import NormalizedRecord, legacy_localized
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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _localized_json(record: NormalizedRecord) -> str:
    return _json(localized_to_json_values(dict(record.localized)))


def decode_record(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for field in RECORD_JSON_FIELDS:
        value[field] = json_loads(value.get(field), [])
    value["deleted"] = bool(value.pop("is_deleted", 0))
    # JSON-native {dc_field: [[text, language-or-null], ...]} for reports.
    value["localized"] = json_loads(value.pop("localized_values", None), {}) or {}
    return value


def migrate_legacy_localized(connection: sqlite3.Connection) -> int:
    """Backfill ``localized_values`` for pre-v2 databases.

    Existing scalar/list values are migrated as untagged legacy values (no
    language label is invented), and ``record_hash`` is recomputed with the
    current formula so stored rows stay consistent with the content they
    now claim to represent.  Returns the number of migrated rows.
    """
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(records)")
    }
    if "localized_values" not in columns:
        connection.execute(
            "ALTER TABLE records "
            "ADD COLUMN localized_values TEXT NOT NULL DEFAULT '{}'"
        )
    rows = connection.execute("SELECT * FROM records").fetchall()
    migrated = 0
    for row in rows:
        if json_loads(row["localized_values"], {}):
            continue  # already carries localized values
        decoded = decode_record(row)
        localized = legacy_localized(decoded)
        rebuilt = NormalizedRecord(
            identifier=decoded["identifier"],
            datestamp=decoded["datestamp"],
            set_specs=tuple(decoded["set_specs"]),
            deleted=decoded["deleted"],
            title=decoded["title"],
            creators=tuple(decoded["creators"]),
            subjects=tuple(decoded["subjects"]),
            description=decoded["description"],
            publisher=decoded["publisher"],
            contributors=tuple(decoded["contributors"]),
            date=decoded["date"],
            resource_type=decoded["resource_type"],
            format=decoded["format"],
            identifiers=tuple(decoded["identifiers"]),
            sources=tuple(decoded["sources"]),
            language=decoded["language"],
            relations=tuple(decoded["relations"]),
            coverage=tuple(decoded["coverage"]),
            rights=decoded["rights"],
            localized=localized,
        )
        connection.execute(
            "UPDATE records SET localized_values = ?, record_hash = ? "
            "WHERE identifier = ?",
            (_localized_json(rebuilt), rebuilt.record_hash, row["identifier"]),
        )
        migrated += 1
    return migrated


class RecordStore:
    def __init__(
        self, connect: Callable[[], AbstractContextManager[sqlite3.Connection]]
    ) -> None:
        self._connect = connect

    def upsert(
        self,
        record: NormalizedRecord,
        *,
        run_id: int,
        response_path: str,
        seen_at: str | None = None,
    ) -> str:
        data = (
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
            _localized_json(record),
            record.record_hash,
        )
        observed = seen_at or iso_z()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT record_hash, is_deleted FROM records WHERE identifier = ?",
                (record.identifier,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO records (
                        identifier, datestamp, set_specs, is_deleted, title,
                        creators, subjects, description, publisher, contributors,
                        date, resource_type, format, identifiers, sources,
                        language, relations, coverage, rights, localized_values,
                        record_hash, first_harvested_at, last_harvested_at,
                        last_run_id, last_response_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?)
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
                connection.commit()
                return "deleted" if record.deleted else "created"

            unchanged = (
                existing["record_hash"] == record.record_hash
                and bool(existing["is_deleted"]) == record.deleted
            )
            connection.execute(
                """
                UPDATE records SET
                    datestamp = ?, set_specs = ?, is_deleted = ?, title = ?,
                    creators = ?, subjects = ?, description = ?, publisher = ?,
                    contributors = ?, date = ?, resource_type = ?, format = ?,
                    identifiers = ?, sources = ?, language = ?, relations = ?,
                    coverage = ?, rights = ?, localized_values = ?, record_hash = ?,
                    last_harvested_at = ?, last_run_id = ?, last_response_path = ?
                WHERE identifier = ?
                """,
                (*data, observed, run_id, response_path, record.identifier),
            )
            connection.commit()
        if unchanged:
            return "unchanged"
        return "deleted" if record.deleted else "updated"

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE identifier = ?", (identifier,)
            ).fetchone()
        return decode_record(row) if row else None

    def for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE last_run_id = ? ORDER BY identifier",
                (run_id,),
            ).fetchall()
        return [decode_record(row) for row in rows]
