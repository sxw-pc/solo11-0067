"""Small, dependency-free helpers shared across layers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_z(value: datetime | None = None) -> str:
    """Return a second-precision UTC timestamp ending in ``Z``."""
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def timestamp_slug(value: datetime | None = None) -> str:
    return iso_z(value).replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")


def parse_datestamp(value: str) -> datetime:
    """Parse an OAI UTC datestamp or date-only value.

    Raises ``ValueError`` when the value is not a supported timestamp.
    """
    text = str(value).strip()
    if _DATE_ONLY.fullmatch(text):
        parsed_date = date.fromisoformat(text)
        return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    iso_value = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    parsed = datetime.fromisoformat(iso_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_datestamp(value: str) -> str:
    return iso_z(parse_datestamp(value))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def hash_payload(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def collapse_ws(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def unique_clean(values: Iterable[Any]) -> tuple[str, ...]:
    """Clean whitespace and de-duplicate case-insensitively, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        cleaned = collapse_ws(raw)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return tuple(result)


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [collapse_ws(item) for item in value if collapse_ws(item)]
    cleaned = collapse_ws(value)
    return [cleaned] if cleaned else []


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_directory(path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default


def short_token() -> str:
    return uuid.uuid4().hex[:8]
