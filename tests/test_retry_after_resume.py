"""End-to-end Retry-After, deferred deadline and resume-gating tests.

A small controllable loopback OAI server sits in front of the real mock
repository so tests can inject an exact 429/503-plus-Retry-After response for
a chosen request (first page or a specific resumption token) and then let the
same request succeed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from metaharvest.config import RetryConfig
from metaharvest.db import Database
from metaharvest.harvester import Harvester, ResumeTooEarly
from metaharvest.oai_errors import HarvestError


class FaultServer:
    """Loopback proxy that can fail chosen OAI requests once.

    ``faults`` is a list of dicts consumed in order when their ``match``
    predicate (receiving the decoded query) is true::

        {"match": lambda q: q.get("verb") == ["ListRecords"], "status": 503,
         "retry_after": "120", "body": b"..."}
    """

    def __init__(self, upstream_url: str) -> None:
        self.upstream_url = upstream_url
        self.faults: list[dict] = []
        self.requests: list[dict[str, list[str]]] = []
        self.raw_paths: list[str] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}/oai"

    def add_fault(self, match, status, *, retry_after=None, body=b"<html>down</html>"):
        with self._lock:
            self.faults.append(
                {
                    "match": match,
                    "status": status,
                    "retry_after": retry_after,
                    "body": body,
                    "used": False,
                }
            )

    def _take_fault(self, query: dict[str, list[str]]):
        with self._lock:
            for fault in self.faults:
                if not fault["used"] and fault["match"](query):
                    fault["used"] = True
                    return fault
        return None

    def __enter__(self) -> FaultServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=5)

    def _handler(self):
        import urllib.request

        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                return

            def do_GET(self):  # noqa: N802
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
                with test._lock:
                    test.requests.append(query)
                    test.raw_paths.append(self.path)
                fault = test._take_fault(query)
                if fault is not None:
                    self.send_response(fault["status"])
                    if fault["retry_after"] is not None:
                        self.send_header("Retry-After", fault["retry_after"])
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(fault["body"])))
                    self.end_headers()
                    self.wfile.write(fault["body"])
                    return
                upstream = test.upstream_url + "?" + parsed.query
                with urllib.request.urlopen(upstream, timeout=10) as response:
                    body = response.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


@pytest.fixture()
def fault_server(live_mock):
    with FaultServer(live_mock.endpoint) as server:
        yield server


def _retry(policy: str, **overrides) -> RetryConfig:
    values = dict(
        policy=policy,
        max_attempts=3,
        default_wait_seconds=2,
        max_retry_after_seconds=120,
        total_wait_seconds=300,
        deferred_wait_seconds=2,
    )
    values.update(overrides)
    return RetryConfig(**values)


def _config(harvest_config_factory, server: FaultServer, policy: str, **retry):
    config = harvest_config_factory()
    return replace(
        config,
        repository=replace(config.repository, base_url=server.endpoint),
        retry=_retry(policy, **retry),
    )


def _is_first_page(query: dict[str, list[str]]) -> bool:
    return "resumptionToken" not in query


def _is_continuation(query: dict[str, list[str]]) -> bool:
    return "resumptionToken" in query


def _sleeps(recorder: list[float]):
    def sleep(seconds: float) -> None:
        recorder.append(seconds)

    return sleep


def test_continuation_503_then_success_inline_exactly_once(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(_is_continuation, 503, retry_after="1")
    sleeps: list[float] = []
    config = _config(harvest_config_factory, fault_server, "inline")
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()

    assert result.status == "completed"
    assert result.page_count == 3
    assert result.record_count == 12
    assert result.created_count == 11
    assert result.deleted_count == 1
    assert sleeps == [1.0]

    db = Database(config.storage.db_path)
    raw = db.raw_responses_for_run(result.run_id)
    assert [item["page_number"] for item in raw] == [1, 2, 3]
    assert all(item["http_status"] == 200 for item in raw)
    # The continuation retry replayed the exact same token request.
    continuation = [
        query for query in fault_server.requests if "resumptionToken" in query
    ]
    # T1 fails, T1 is replayed, then T2 advances: three continuation requests.
    assert len(continuation) == 3
    tokens = [query["resumptionToken"][0] for query in continuation]
    assert tokens[0] == tokens[1]
    assert tokens[1] != tokens[2]

    attempts = db.request_attempts_for_run(result.run_id)
    # One row per HTTP attempt: 3 successes + 1 failed continuation = 4.
    assert len(attempts) == 4
    failed = [item for item in attempts if item["http_status"] == 503]
    assert len(failed) == 1
    assert failed[0]["outcome"] == "retry_scheduled"
    assert failed[0]["retryable"] is True
    assert failed[0]["retry_after"] == "1"
    assert sum(1 for item in attempts if item["outcome"] == "success") == 3
    # A retry does not inflate counts: only 3 pages of records processed once.
    assert result.record_count == 12


def test_first_page_503_then_success_inline(harvest_config_factory, fault_server):
    fault_server.add_fault(_is_first_page, 503, retry_after="2")
    sleeps: list[float] = []
    config = _config(harvest_config_factory, fault_server, "inline")
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "completed"
    assert result.page_count == 3
    assert result.record_count == 12
    assert sleeps == [2.0]
    raw = Database(config.storage.db_path).raw_responses_for_run(result.run_id)
    # The 503 body is NOT page 1 evidence.
    assert [item["page_number"] for item in raw] == [1, 2, 3]
    assert all(item["http_status"] == 200 for item in raw)


def test_retry_after_http_date_honoured(harvest_config_factory, fault_server):
    fault_server.add_fault(_is_first_page, 503, retry_after="Thu, 01 Jan 2099 00:00:00 GMT")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory, fault_server, "inline", max_retry_after_seconds=30
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "completed"
    # Far-future date clamps to the per-attempt ceiling, not infinity.
    assert sleeps == [30.0]


def test_overlong_retry_after_clamped_to_ceiling(harvest_config_factory, fault_server):
    fault_server.add_fault(_is_first_page, 429, retry_after="99999")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory, fault_server, "inline", max_retry_after_seconds=15
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "completed"
    assert sleeps == [15.0]


def test_malformed_retry_after_uses_default(harvest_config_factory, fault_server):
    fault_server.add_fault(_is_first_page, 503, retry_after="later-today")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory, fault_server, "inline", default_wait_seconds=3
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "completed"
    assert sleeps == [3.0]


def test_total_wait_budget_exhausted_interrupts_without_sleep(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(_is_first_page, 503, retry_after="50")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory,
        fault_server,
        "inline",
        total_wait_seconds=20,
        max_retry_after_seconds=60,
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "interrupted"
    assert result.interrupted_reason == "retry_budget_exhausted"
    assert result.retry_not_before is not None
    assert sleeps == []
    db = Database(config.storage.db_path)
    assert db.raw_responses_for_run(result.run_id) == []
    assert db.request_attempts_for_run(result.run_id)[-1]["outcome"] == "retry_deferred"

    # Before the due time, resume refuses and sends no request.
    sent_before = len(fault_server.requests)
    with pytest.raises(ResumeTooEarly):
        Harvester(config).resume(result.run_id)
    assert len(fault_server.requests) == sent_before


def test_attempt_budget_exhausted_marks_interrupted(harvest_config_factory, fault_server):
    for _ in range(5):
        fault_server.add_fault(_is_first_page, 503, retry_after="0")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory, fault_server, "inline", max_attempts=3
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "interrupted"
    assert result.interrupted_reason == "retry_exhausted"
    db = Database(config.storage.db_path)
    attempts = db.request_attempts_for_run(result.run_id)
    assert len(attempts) == 3
    assert sum(1 for item in attempts if item["outcome"] == "http_error") == 1
    assert db.raw_responses_for_run(result.run_id) == []


def test_http_200_oai_error_is_not_retried(harvest_config_factory, fault_server, live_mock):
    # Ask for a set that does not exist: the mock returns HTTP 200 noRecordsMatch.
    config = _config(harvest_config_factory, fault_server, "inline")
    config = replace(
        config, repository=replace(config.repository, set_spec="missing-set")
    )
    result = Harvester(config).start()
    assert result.status == "completed"
    assert result.page_count == 1
    assert len(fault_server.requests) == 1
    db = Database(config.storage.db_path)
    attempts = db.request_attempts_for_run(result.run_id)
    assert [item["outcome"] for item in attempts] == ["oai_error"]
    assert db.raw_responses_for_run(result.run_id)[0]["http_status"] == 200


def test_http_200_bad_resumption_token_is_never_retried(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(
        _is_continuation,
        200,
        retry_after=None,
        body=(
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            b"<responseDate>2026-01-01T00:00:00Z</responseDate>"
            b'<request verb="ListRecords">http://127.0.0.1/oai</request>'
            b'<error code="badResumptionToken">expired</error></OAI-PMH>'
        ),
    )
    config = _config(harvest_config_factory, fault_server, "inline")
    with pytest.raises(HarvestError):
        Harvester(config).start()
    db = Database(config.storage.db_path)
    run = db.latest_run()
    assert run["status"] == "failed"
    attempts = db.request_attempts_for_run(run["id"])
    assert [item["outcome"] for item in attempts] == ["success", "oai_error"]


def test_non_retryable_http_status_fails_immediately(harvest_config_factory, fault_server):
    fault_server.add_fault(_is_first_page, 404, retry_after="1")
    sleeps: list[float] = []
    config = _config(harvest_config_factory, fault_server, "inline")
    with pytest.raises(HarvestError):
        Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert sleeps == []
    db = Database(config.storage.db_path)
    attempts = db.request_attempts_for_run(db.latest_run()["id"])
    assert [item["outcome"] for item in attempts] == ["http_error"]
    assert attempts[0]["retryable"] is False


def test_retry_shape_mismatch_is_not_replayed(harvest_config_factory, fault_server):
    """If the client reports a different request shape, do not replay it."""
    from metaharvest.http_client import HTTPClient
    from metaharvest.oai_client import OAIClient
    from metaharvest.oai_errors import HttpRequestError
    from metaharvest.harvester import Harvester as H

    config = _config(harvest_config_factory, fault_server, "inline")
    http = HTTPClient(
        timeout_seconds=1,
        audit_log=config.storage.audit_log,
    )

    class MismatchingClient:
        """Sends a first-page-shaped request even for a continuation."""

        def __init__(self) -> None:
            self.requests: list[dict[str, str]] = []
            self.calls = 0

        def list_records(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return OAIClient(
                    config.repository.base_url, http
                ).list_records(metadata_prefix="oai_dc")
            raise HttpRequestError(
                "HTTP 503",
                url="http://127.0.0.1/oai",
                status_code=503,
                headers={"retry-after": "1"},
                params={"verb": "ListRecords", "metadataPrefix": "oai_dc"},
                retryable=True,
            )

    client = MismatchingClient()
    result = H(config, client=client).start()  # type: ignore[arg-type]
    assert result.status == "interrupted"
    assert result.interrupted_reason == "retry_shape_mismatch"
    assert client.calls == 2  # page 1, then one failed continuation - no replay
    db = Database(config.storage.db_path)
    assert [item["page_number"] for item in db.raw_responses_for_run(result.run_id)] == [1]
    assert (
        db.request_attempts_for_run(result.run_id)[-1]["outcome"]
        == "retry_shape_mismatch"
    )


def test_total_wait_budget_is_shared_across_pages(
    harvest_config_factory, fault_server
):
    """Two 8s waits exceed a 10s total budget; the second defers."""
    fault_server.add_fault(_is_continuation, 503, retry_after="8")
    fault_server.add_fault(_is_continuation, 503, retry_after="8")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory,
        fault_server,
        "inline",
        total_wait_seconds=10,
        max_retry_after_seconds=60,
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert sleeps == [8.0]
    assert result.status == "interrupted"
    assert result.interrupted_reason == "retry_budget_exhausted"
    db = Database(config.storage.db_path)
    # Only page 1 is visible; the second page's wait was refused outright.
    assert [item["page_number"] for item in db.raw_responses_for_run(result.run_id)] == [1]
    assert result.retry_not_before is not None


def test_deferred_short_wait_completes(harvest_config_factory, fault_server):
    fault_server.add_fault(_is_continuation, 503, retry_after="1")
    sleeps: list[float] = []
    config = _config(
        harvest_config_factory, fault_server, "deferred", deferred_wait_seconds=5
    )
    result = Harvester(config, sleeper=_sleeps(sleeps)).start()
    assert result.status == "completed"
    assert sleeps == [1.0]


def test_inline_versus_deferred_same_fault(harvest_config_factory, fault_server):
    """Same 503/Retry-After 120s: inline completes, deferred interrupts."""
    # Inline: budget 300s >= 120s, so one uninterrupted batch run completes.
    fault_server.add_fault(_is_continuation, 503, retry_after="120")
    inline_sleeps: list[float] = []
    inline_cfg = _config(harvest_config_factory, fault_server, "inline")
    inline = Harvester(inline_cfg, sleeper=_sleeps(inline_sleeps)).start()
    assert inline.status == "completed"
    assert inline.record_count == 12
    assert inline_sleeps == [120.0]

    # Deferred: short allowance only, so it persists the deadline and stops.
    fault_server.add_fault(_is_continuation, 503, retry_after="120")
    deferred_sleeps: list[float] = []
    deferred_cfg = _config(
        harvest_config_factory, fault_server, "deferred", deferred_wait_seconds=5
    )
    deferred = Harvester(deferred_cfg, sleeper=_sleeps(deferred_sleeps)).start()
    assert deferred.status == "interrupted"
    assert deferred.retry_not_before is not None
    assert deferred_sleeps == []
    # Only earlier pages are visible: page 1 processed, page 2 not.
    assert deferred.page_count == 1
    assert deferred.record_count == 4
    db = Database(deferred_cfg.storage.db_path)
    assert [item["page_number"] for item in db.raw_responses_for_run(deferred.run_id)] == [1]

    # Deferred resume before due sends nothing and raises.
    before = len(fault_server.requests)
    with pytest.raises(ResumeTooEarly):
        Harvester(deferred_cfg, sleeper=_sleeps(deferred_sleeps)).resume(deferred.run_id)
    assert len(fault_server.requests) == before
    assert deferred_sleeps == []


def test_deferred_resume_after_due_uses_same_token(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(_is_continuation, 503, retry_after="60")
    config = _config(
        harvest_config_factory, fault_server, "deferred", deferred_wait_seconds=5,
        total_wait_seconds=300,
    )
    first = Harvester(config).start()
    assert first.status == "interrupted"
    assert first.checkpoint_token  # token for page 2 was saved from page 1
    saved_token = first.checkpoint_token
    db = Database(config.storage.db_path)
    run = db.get_run(first.run_id)
    assert run["retry_not_before"]

    # Simulate the scheduler waking after the deadline: recentre the clock.
    due = datetime.fromisoformat(str(run["retry_not_before"]).replace("Z", "+00:00"))
    now = due + timedelta(seconds=1)
    clock = lambda: now  # noqa: E731
    resumed = Harvester(config, clock=clock).resume(first.run_id)

    assert resumed.status == "completed"
    assert resumed.page_count == 3
    assert resumed.record_count == 12
    # The resume request used the exact saved continuation token, not a restart.
    token_queries = [q for q in fault_server.requests if "resumptionToken" in q]
    assert any(q["resumptionToken"] == [saved_token] for q in token_queries)
    assert db.get_run(first.run_id)["retry_not_before"] is None


def test_resume_after_deferred_does_not_double_process(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(_is_continuation, 503, retry_after="60")
    config = _config(harvest_config_factory, fault_server, "deferred")
    first = Harvester(config).start()
    db = Database(config.storage.db_path)
    assert first.record_count == 4
    assert first.page_count == 1

    run = db.get_run(first.run_id)
    due = datetime.fromisoformat(str(run["retry_not_before"]).replace("Z", "+00:00"))
    resumed = Harvester(
        config, clock=lambda: due + timedelta(seconds=1)
    ).resume(first.run_id)
    assert resumed.status == "completed"
    assert resumed.record_count == 12
    assert resumed.page_count == 3
    # Page 1 evidence and records were not duplicated or re-counted.
    raw = db.raw_responses_for_run(first.run_id)
    assert [item["page_number"] for item in raw] == [1, 2, 3]
    records = db.records.for_run(first.run_id)
    assert len({record["identifier"] for record in records}) == len(records)


def test_interrupted_wait_persists_deadline_without_processing(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(_is_continuation, 503, retry_after="30")

    def interrupted_sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    config = _config(harvest_config_factory, fault_server, "inline")
    result = Harvester(config, sleeper=interrupted_sleep).start()
    assert result.status == "interrupted"
    assert result.interrupted_reason == "retry_interrupted"
    assert result.page_count == 1
    assert result.record_count == 4
    assert result.retry_not_before is not None
    db = Database(config.storage.db_path)
    assert [item["page_number"] for item in db.raw_responses_for_run(result.run_id)] == [1]
    assert db.get_run(result.run_id)["checkpoint_token"]
    with pytest.raises(ResumeTooEarly):
        Harvester(config).resume(result.run_id)


def test_retry_on_continuation_preserves_token_shape(
    harvest_config_factory, fault_server
):
    fault_server.add_fault(_is_continuation, 503, retry_after="1")
    config = _config(harvest_config_factory, fault_server, "inline")
    Harvester(config, sleeper=lambda seconds: None).start()
    continuation_queries = [
        query for query in fault_server.requests if "resumptionToken" in query
    ]
    # Every continuation request carries ONLY verb+resumptionToken.
    for query in continuation_queries:
        assert set(query) == {"verb", "resumptionToken"}
    # And the retried token equals the one from the preceding failed attempt.
    tokens = [query["resumptionToken"][0] for query in continuation_queries]
    assert len(tokens) == 3
    assert tokens[0] == tokens[1]
    assert tokens[1] != tokens[2]


def test_report_exposes_attempt_evidence_and_integrity(
    harvest_config_factory, fault_server
):
    from metaharvest.report import build_report

    fault_server.add_fault(_is_continuation, 503, retry_after="1")
    config = _config(harvest_config_factory, fault_server, "inline")
    result = Harvester(config, sleeper=lambda seconds: None).start()
    db = Database(config.storage.db_path)
    report = build_report(db, result.run_id)
    assert report["attempt_summary"]["total"] == len(report["attempts"])
    assert report["attempt_summary"]["outcomes"]["retry_scheduled"] == 1
    assert report["attempt_summary"]["outcomes"]["success"] == 3
    assert report["attempt_summary"]["retried"] == 1
    assert report["integrity"]["status"] == "ok"
    assert len(report["raw_responses"]) == 3


def test_cli_retry_policy_inline_completes(
    harvest_config_factory, fault_server, tmp_path, capsys
):
    from metaharvest.cli import main

    config = harvest_config_factory()
    # Use the stub config in a file so the CLI can load it end to end.
    config_path = _write_config(tmp_path, config, fault_server.endpoint)
    fault_server.add_fault(_is_first_page, 503, retry_after="0")
    code = main(
        [
            "harvest",
            "--config",
            str(config_path),
            "--retry-policy",
            "inline",
            "--db",
            str(config.storage.db_path),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["retry_policy"] == "inline"
    assert payload["record_count"] == 12
    assert payload["attempts"] == 4


def test_cli_deferred_interrupts_then_refuses_early_resume(
    harvest_config_factory, fault_server, tmp_path, capsys
):
    from metaharvest.cli import main

    config = harvest_config_factory()
    config_path = _write_config(tmp_path, config, fault_server.endpoint)
    fault_server.add_fault(_is_continuation, 503, retry_after="120")
    assert (
        main(
            [
                "harvest",
                "--config",
                str(config_path),
                "--retry-policy",
                "deferred",
                "--db",
                str(config.storage.db_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "interrupted"
    assert payload["retry_policy"] == "deferred"
    assert payload["retry_not_before"]
    assert payload["page_count"] == 1
    assert "resume_hint" in payload

    before = len(fault_server.requests)
    code = main(
        [
            "harvest",
            "--config",
            str(config_path),
            "--resume",
            str(payload["run_id"]),
            "--db",
            str(config.storage.db_path),
        ]
    )
    assert code == 3
    assert "尚未到重试时间" in capsys.readouterr().err
    assert len(fault_server.requests) == before


def _write_config(tmp_path: Path, config, base_url: str) -> Path:
    import yaml

    document = {
        "repository": {
            "base_url": base_url,
            "metadata_prefix": config.repository.metadata_prefix,
            "set_spec": config.repository.set_spec,
            "from_date": config.repository.from_date,
            "until_date": config.repository.until_date,
        },
        "http": {
            "timeout_seconds": config.http.timeout_seconds,
            "user_agent": config.http.user_agent,
        },
        "retry": {
            "policy": config.retry.policy,
            "max_attempts": config.retry.max_attempts,
            "default_wait_seconds": 0,
            "max_retry_after_seconds": config.retry.max_retry_after_seconds,
            "total_wait_seconds": config.retry.total_wait_seconds,
            "deferred_wait_seconds": config.retry.deferred_wait_seconds,
        },
        "storage": {
            "db_path": str(config.storage.db_path),
            "archive_dir": str(config.storage.archive_dir),
            "report_dir": str(config.storage.report_dir),
            "audit_log": str(config.storage.audit_log),
        },
    }
    path = tmp_path / "harvest.yml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path
