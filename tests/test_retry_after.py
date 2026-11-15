"""Retry-After parsing, classification and bounded budget tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from metaharvest.constants import RETRYABLE_HTTP_STATUSES
from metaharvest.http_client import is_transient_network_error
from metaharvest.oai_errors import HttpRequestError
from metaharvest.retry_policy import (
    is_retryable_failure,
    parse_retry_after,
    resolve_retry_wait,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _error(status=None, *, retryable=False, headers=None):
    return HttpRequestError(
        "boom", url="http://127.0.0.1/oai", status_code=status,
        retryable=retryable, headers=headers or {},
    )


@pytest.mark.parametrize("status", [429, 503, 408, 502, 504])
def test_allowlisted_statuses_are_retryable(status):
    assert status in RETRYABLE_HTTP_STATUSES
    assert is_retryable_failure(_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 501, 422])
def test_non_allowlisted_statuses_are_final(status):
    assert is_retryable_failure(_error(status)) is False


def test_connection_errors_rely_on_classifier_flag():
    assert is_retryable_failure(_error(retryable=True)) is True
    assert is_retryable_failure(_error(retryable=False)) is False
    # A status always wins over a stray retryable flag.
    assert is_retryable_failure(_error(400, retryable=True)) is False


def test_transient_network_classifier_accepts_timeouts_and_resets():
    assert is_transient_network_error(requests.Timeout("slow")) is True
    assert is_transient_network_error(requests.ConnectionError("refused")) is True
    reset = requests.ConnectionError("reset")
    reset.__cause__ = ConnectionResetError(104, "Connection reset by peer")
    assert is_transient_network_error(reset) is True


def test_transient_network_classifier_rejects_other_failures():
    assert is_transient_network_error(requests.TooManyRedirects("loop")) is False
    assert is_transient_network_error(requests.exceptions.MissingSchema("bad")) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("120", 120.0), ("0", 0.0), ("1.5", 1.5), (" 30 ", 30.0)],
)
def test_parse_delta_seconds(raw, expected):
    assert parse_retry_after(raw, now=NOW) == expected


def test_parse_http_date_uses_absolute_due_time():
    due = NOW + timedelta(seconds=90)
    header = format_datetime(due, usegmt=True)
    assert parse_retry_after(header, now=NOW) == pytest.approx(90, abs=1)


def test_past_http_date_is_due_now():
    header = format_datetime(NOW - timedelta(hours=1), usegmt=True)
    assert parse_retry_after(header, now=NOW) == 0.0


@pytest.mark.parametrize("raw", ["", "   ", "soon", "12s", "Sat, 99 Xxx 2026", "-5"])
def test_malformed_retry_after_returns_none(raw):
    assert parse_retry_after(raw, now=NOW) is None


def test_resolve_prefers_header_and_clamps_to_ceiling():
    wait, raw, source = resolve_retry_wait(
        _error(503, headers={"Retry-After": "900"}),
        now=NOW,
        default_wait_seconds=5,
        max_retry_after_seconds=120,
    )
    assert (wait, raw, source) == (120.0, "900", "header")


def test_resolve_falls_back_to_default_when_missing_or_malformed():
    for headers in ({}, {"Retry-After": "whenever"}):
        wait, raw, source = resolve_retry_wait(
            _error(503, headers=headers),
            now=NOW,
            default_wait_seconds=7,
            max_retry_after_seconds=120,
        )
        assert (wait, source) == (7.0, "default")


def test_resolve_case_insensitive_header_lookup():
    wait, raw, _ = resolve_retry_wait(
        _error(503, headers={"retry-after": "3"}),
        now=NOW,
        default_wait_seconds=5,
        max_retry_after_seconds=120,
    )
    assert (wait, raw) == (3.0, "3")
