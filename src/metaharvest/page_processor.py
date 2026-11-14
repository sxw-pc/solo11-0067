"""Transform one parsed OAI page into idempotent database writes."""

from __future__ import annotations

from .db import Database
from .normalize import normalize_record
from .oai_errors import RecordParseError
from .parser import RecordPage
from .util import iso_z


def process_page(
    db: Database, run_id: int, page: RecordPage, *, response_path: str
) -> dict[str, int]:
    counts = {
        "record_count": 0,
        "created_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "deleted_count": 0,
        "error_count": 0,
    }
    for issue in page.issues:
        counts["record_count"] += 1
        counts["error_count"] += 1
        db.add_error(
            run_id,
            scope="record",
            message=issue.message,
            code=issue.code,
            identifier=issue.identifier,
        )
    seen_at = iso_z()
    for parsed in page.records:
        counts["record_count"] += 1
        try:
            record = normalize_record(parsed)
        except RecordParseError as exc:
            counts["error_count"] += 1
            db.add_error(
                run_id,
                scope="record",
                message=str(exc),
                code="normalizationError",
                identifier=parsed.identifier,
            )
            continue
        outcome = db.records.upsert(
            record,
            run_id=run_id,
            response_path=response_path,
            seen_at=seen_at,
        )
        counts[f"{outcome}_count"] += 1
        if outcome != "unchanged":
            db.add_event(
                run_id,
                f"record_{outcome}",
                identifier=record.identifier,
                detail={"datestamp": record.datestamp},
            )
    return counts
