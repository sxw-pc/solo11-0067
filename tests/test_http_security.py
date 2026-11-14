from __future__ import annotations

import json
from pathlib import Path

import pytest

from metaharvest.constants import MAX_RESPONSE_BYTES
from metaharvest.http_client import HTTPClient, validate_local_url
from metaharvest.oai_errors import (
    HttpRequestError,
    ResponseTooLarge,
    SecurityViolation,
)


class FakeResponse:
    def __init__(self, status_code=200, body=b"<ok/>", headers=None, url=""):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.url = url
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787/oai",
        "http://localhost:8787/oai",
        "http://[::1]:8787/oai",
        "https://127.0.0.1/oai",
    ],
)
def test_allowed_local_urls(url):
    assert validate_local_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/oai",
        "http://127.0.0.1.evil.invalid/oai",
        "ftp://127.0.0.1/oai",
        "http://user:pass@127.0.0.1/oai",
        "http://127.0.0.1:notaport/oai",
        "http://[::2]:8787/oai",
        "http://127.0.0.1/oai\nInjected: yes",
    ],
)
def test_non_local_or_malformed_urls_are_rejected(url):
    with pytest.raises(SecurityViolation):
        validate_local_url(url)


def test_blocked_host_never_reaches_network_and_is_audited(tmp_path: Path):
    session = FakeSession([FakeResponse(url="http://example.com/oai")])
    client = HTTPClient(audit_log=tmp_path / "audit.jsonl", session=session)
    with pytest.raises(SecurityViolation):
        client.get("http://example.com/oai")
    assert session.calls == []
    entries = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert entries[-1]["outcome"] == "blocked"
    assert entries[-1]["host"] == "example.com"


def test_external_redirect_is_blocked_before_following(tmp_path: Path):
    first = FakeResponse(
        status_code=302,
        headers={"Location": "http://demo.invalid/collect"},
        url="http://127.0.0.1:8787/oai",
    )
    session = FakeSession([first])
    client = HTTPClient(audit_log=tmp_path / "audit.jsonl", session=session)
    with pytest.raises(SecurityViolation):
        client.get("http://127.0.0.1:8787/oai")
    assert len(session.calls) == 1
    assert first.closed is True
    outcomes = [
        json.loads(line)["outcome"]
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "blocked_redirect" in outcomes


def test_local_success_and_http_error_are_audited(tmp_path: Path):
    audit = tmp_path / "audit.jsonl"
    session = FakeSession(
        [
            FakeResponse(
                body=b"<OAI-PMH/>",
                headers={"Content-Length": "10", "Content-Type": "text/xml"},
                url="http://127.0.0.1:8787/oai?verb=Identify",
            ),
            FakeResponse(status_code=503, url="http://127.0.0.1:8787/oai"),
        ]
    )
    client = HTTPClient(audit_log=audit, session=session)
    response = client.get("http://127.0.0.1:8787/oai", {"verb": "Identify"})
    assert response.content == b"<OAI-PMH/>"
    assert response.headers["content-type"] == "text/xml"
    assert response.fetched_at.endswith("Z")
    with pytest.raises(HttpRequestError) as caught:
        client.get("http://127.0.0.1:8787/oai", {"verb": "Identify"})
    assert caught.value.status_code == 503
    outcomes = [
        json.loads(line)["outcome"]
        for line in audit.read_text(encoding="utf-8").splitlines()
    ]
    assert outcomes == ["request", "success", "request", "http_error"]


def test_declared_oversized_response_is_rejected(tmp_path: Path):
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)},
                url="http://127.0.0.1:8787/oai",
            )
        ]
    )
    client = HTTPClient(audit_log=tmp_path / "audit.jsonl", session=session)
    with pytest.raises(ResponseTooLarge):
        client.get("http://127.0.0.1:8787/oai")
