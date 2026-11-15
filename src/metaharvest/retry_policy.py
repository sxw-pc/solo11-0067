"""Retry-After parsing and bounded retry classification.

The retry loop itself lives in :mod:`metaharvest.harvester` because only the
harvester owns the checkpoint, page archive and run-lifecycle boundary. This
module keeps the pure decisions (what is retryable, how long to wait, when the
server's due time is) separate and unit-testable.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

from .constants import RETRYABLE_HTTP_STATUSES
from .oai_errors import HttpRequestError
from .util import iso_z

_DELTA_SECONDS = re.compile(r"^\d+(?:\.\d+)?$")


def is_retryable_failure(error: HttpRequestError) -> bool:
    """Return True only for the explicit transient allowlist.

    A status code always wins: even if a caller marked the error retryable, a
    non-allowlisted HTTP status (for example 400 or 500) is never retried.
    Connection-level failures carry no status and rely on ``retryable``.
    """
    if error.status_code is not None:
        return error.status_code in RETRYABLE_HTTP_STATUSES
    return bool(error.retryable)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse ``Retry-After`` as delta seconds or an HTTP date.

    Returns ``None`` when the header is absent or malformed so the caller can
    fall back to a bounded default wait. Negative deltas are treated as zero:
    the server already considers the request due.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _DELTA_SECONDS.fullmatch(text):
        try:
            seconds = float(text)
        except ValueError:  # pragma: no cover - regex already guarantees a number
            return None
        return max(0.0, seconds)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - current).total_seconds())


def resolve_retry_wait(
    error: HttpRequestError,
    *,
    now: datetime,
    default_wait_seconds: float,
    max_retry_after_seconds: float,
) -> tuple[float, str | None, str]:
    """Resolve the bounded wait for one failed attempt.

    Returns ``(wait_seconds, raw_retry_after, source)`` where ``source`` is
    ``header`` when a usable Retry-After was parsed and ``default`` otherwise.
    The wait is clamped to the configured per-attempt ceiling; the caller is
    responsible for comparing it against the total-wait budget.
    """
    raw = _header(error.headers, "retry-after")
    parsed = parse_retry_after(raw, now=now)
    source = "header" if parsed is not None else "default"
    wait = default_wait_seconds if parsed is None else parsed
    return min(max(0.0, wait), max_retry_after_seconds), raw, source


def retry_deadline(now: datetime, wait_seconds: float) -> str:
    return iso_z(now + timedelta(seconds=max(0.0, wait_seconds)))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return None
