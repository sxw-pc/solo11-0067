"""Idempotent normalized-record persistence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode_record(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for field in RECORD_JSON_FIELDS:
        value[field] = json_loads(value.get(field), [])
    value["deleted"] = bool(value.pop("is_deleted", 0))
    return value


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
                        language, relations, coverage, rights, record_hash,
                        first_harvested_at, last_harvested_at, last_run_id,
                        last_response_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?)
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
                    coverage = ?, rights = ?, record_hash = ?,
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
