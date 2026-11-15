"""Resumable, retry-aware OAI-PMH harvesting pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .archive import ArchiveStore
from .config import HarvestConfig, RetryConfig
from .db import Database
from .http_client import HTTPClient
from .oai_client import OAIClient, OAIResponse, build_params
from .oai_errors import HarvestError, HttpRequestError, OAIProtocolError
from .page_processor import process_page
from .parser import OAIEnvelope, parse_oai_response, parse_record_page
from .retry_policy import (
    is_retryable_failure,
    resolve_retry_wait,
    retry_deadline,
)
from .util import hash_payload, iso_z, parse_datestamp


class ResumeTooEarly(HarvestError):
    """A deferred retry deadline has not elapsed; no request may be sent."""

    def __init__(self, message: str, *, run_id: int, retry_not_before: str) -> None:
        super().__init__(message, run_id=run_id)
        self.retry_not_before = retry_not_before


@dataclass(frozen=True)
class HarvestResult:
    run_id: int
    status: str
    page_count: int
    record_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    deleted_count: int
    error_count: int
    checkpoint_token: str | None
    retry_policy: str | None = None
    retry_not_before: str | None = None
    interrupted_reason: str | None = None
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "page_count": self.page_count,
            "record_count": self.record_count,
            "created_count": self.created_count,
            "updated_count": self.updated_count,
            "unchanged_count": self.unchanged_count,
            "deleted_count": self.deleted_count,
            "error_count": self.error_count,
            "checkpoint_token": self.checkpoint_token,
            "retry_policy": self.retry_policy,
            "retry_not_before": self.retry_not_before,
            "interrupted_reason": self.interrupted_reason,
            "attempts": self.attempts,
        }


def _result_from_run(run: dict[str, Any], *, attempts: int = 0) -> HarvestResult:
    return HarvestResult(
        run_id=int(run["id"]),
        status=str(run["status"]),
        page_count=int(run["page_count"]),
        record_count=int(run["record_count"]),
        created_count=int(run["created_count"]),
        updated_count=int(run["updated_count"]),
        unchanged_count=int(run["unchanged_count"]),
        deleted_count=int(run["deleted_count"]),
        error_count=int(run["error_count"]),
        checkpoint_token=run["checkpoint_token"],
        retry_policy=run.get("retry_policy"),
        retry_not_before=run.get("retry_not_before"),
        interrupted_reason=run.get("interrupted_reason"),
        attempts=attempts,
    )


@dataclass
class _PageBatch:
    """Outcome of one page request's bounded retry sequence.

    Exactly one of ``response`` / ``error`` is set for a terminal outcome. An
    interrupted batch carries the persisted deadline and never carries a
    response, guaranteeing a failed response is never archived or processed.
    """

    response: OAIResponse | None = None
    envelope: OAIEnvelope | None = None
    protocol_error: OAIProtocolError | None = None
    error: HttpRequestError | None = None
    interrupted: bool = False
    retry_not_before: str | None = None
    reason: str = ""
    attempts: int = 0


@dataclass
class _RetryBook:
    """Run-call retry bookkeeping shared across pages."""

    attempt_number: int = 0
    waited_total: float = 0.0


class Harvester:
    def __init__(
        self,
        config: HarvestConfig,
        *,
        db: Database | None = None,
        client: OAIClient | None = None,
        archive: ArchiveStore | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.retry: RetryConfig = config.retry
        self.db = db or Database(config.storage.db_path)
        self.http = HTTPClient(
            timeout_seconds=config.http.timeout_seconds,
            user_agent=config.http.user_agent,
            audit_log=config.storage.audit_log,
        )
        self.client = client or OAIClient(config.repository.base_url, self.http)
        self.archive = archive or ArchiveStore(config.storage.archive_dir)
        self._sleep = sleeper or time.sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._client_injected = client is not None
        self._archive_injected = archive is not None

    # ------------------------------------------------------------------ start

    def start(self, *, max_pages: int | None = None) -> HarvestResult:
        pages_limit = self._page_limit(max_pages)
        repo = self.config.repository
        config_hash = hash_payload(
            {
                "base_url": repo.base_url,
                "metadata_prefix": repo.metadata_prefix,
                "set_spec": repo.set_spec,
                "from_date": repo.from_date,
                "until_date": repo.until_date,
            }
        )
        run_id = self.db.create_run(
            repository_url=repo.base_url,
            metadata_prefix=repo.metadata_prefix,
            set_spec=repo.set_spec,
            from_date=repo.from_date,
            until_date=repo.until_date,
            archive_root=self.archive.root,
            config_hash=config_hash,
            retry_policy=self.retry.policy,
        )
        self.db.add_event(
            run_id,
            "run_started",
            detail={
                "base_url": repo.base_url,
                "metadata_prefix": repo.metadata_prefix,
                "set_spec": repo.set_spec,
                "from_date": repo.from_date,
                "until_date": repo.until_date,
                "max_pages": pages_limit,
                "retry_policy": self.retry.policy,
            },
        )
        run = self.db.get_run(run_id)
        assert run is not None
        return self._execute(run, token=None, pages_limit=pages_limit)

    # ----------------------------------------------------------------- resume

    def resume(self, run_id: int, *, max_pages: int | None = None) -> HarvestResult:
        run = self.db.get_run(run_id)
        if run is None:
            raise HarvestError(f"收割运行不存在: {run_id}", run_id=run_id)
        if run["status"] == "completed":
            raise HarvestError(f"运行 #{run_id} 已完成，无需续采", run_id=run_id)
        self._refuse_early_resume(run)
        policy = self._resume_policy(run)
        if policy != self.retry.policy:
            # Persisted policy wins so a resumed run keeps its operator-visible
            # semantics even if the config file changed in between.
            self.retry = RetryConfig(
                policy=policy,
                max_attempts=self.retry.max_attempts,
                default_wait_seconds=self.retry.default_wait_seconds,
                max_retry_after_seconds=self.retry.max_retry_after_seconds,
                total_wait_seconds=self.retry.total_wait_seconds,
                deferred_wait_seconds=self.retry.deferred_wait_seconds,
            )
        pages_limit = self._page_limit(max_pages)
        if not self._client_injected:
            self.client = OAIClient(str(run["repository_url"]), self.http)
        if not self._archive_injected:
            self.archive = ArchiveStore(Path(str(run["archive_root"])))
        self.db.add_event(
            run_id,
            "run_resumed",
            detail={
                "max_pages": pages_limit,
                "policy": policy,
                "checkpoint": bool(run["checkpoint_token"]),
            },
        )
        self.db.save_progress(
            run_id,
            page_count=int(run["page_count"]),
            record_count=int(run["record_count"]),
            created_count=int(run["created_count"]),
            updated_count=int(run["updated_count"]),
            unchanged_count=int(run["unchanged_count"]),
            deleted_count=int(run["deleted_count"]),
            error_count=int(run["error_count"]),
            checkpoint_token=run["checkpoint_token"],
        )
        return self._execute(run, token=run["checkpoint_token"], pages_limit=pages_limit)

    def _resume_policy(self, run: dict[str, Any]) -> str:
        policy = run.get("retry_policy")
        return str(policy) if policy in {"inline", "deferred"} else self.retry.policy

    def _refuse_early_resume(self, run: dict[str, Any]) -> None:
        due = run.get("retry_not_before")
        if not due:
            return
        try:
            due_at = parse_datestamp(str(due))
        except ValueError as exc:
            raise HarvestError(
                f"运行 #{run['id']} 的 retry_not_before 非法: {due}",
                run_id=int(run["id"]),
            ) from exc
        now = self._clock().astimezone(timezone.utc)
        if now < due_at:
            remaining = (due_at - now).total_seconds()
            raise ResumeTooEarly(
                f"运行 #{run['id']} 的重试窗口尚未到达（还需 {remaining:.0f} 秒，"
                f"retry_not_before={due}），未发送任何请求",
                run_id=int(run["id"]),
                retry_not_before=str(due),
            )

    def _page_limit(self, override: int | None) -> int | None:
        value = override if override is not None else self.config.max_pages
        if value is not None and int(value) <= 0:
            raise HarvestError("max_pages 必须大于 0")
        return int(value) if value is not None else None

    # ------------------------------------------------------------------ fetch

    def _fetch(self, run: dict[str, Any], token: str | None) -> OAIResponse:
        return self.client.list_records(
            metadata_prefix=str(run["metadata_prefix"]),
            from_date=run["from_date"],
            until_date=run["until_date"],
            set_spec=run["set_spec"],
            resumption_token=token,
        )

    def _request_params(self, run: dict[str, Any], token: str | None) -> dict[str, str]:
        """The exact parameters a page request sends (token shape preserved).

        Delegates to the same builder ``OAIClient.list_records`` uses so the
        attempt evidence can never drift from the request actually sent.
        """
        return build_params(
            "ListRecords",
            {
                "metadataPrefix": None if token else run["metadata_prefix"],
                "from": None if token else run["from_date"],
                "until": None if token else run["until_date"],
                "set": None if token else run["set_spec"],
                "resumptionToken": token,
            },
        )

    def _fetch_until_parsed(
        self,
        run: dict[str, Any],
        *,
        token: str | None,
        page_target: int,
        book: _RetryBook,
    ) -> _PageBatch:
        """Bounded retry for one page request.

        Never touches counters, checkpoint, archive or record store. Returns
        the first HTTP+OAI success, an interrupted batch with a persisted
        deadline, or a terminal error. HTTP 200 OAI protocol errors are
        classified here and are *never* retried.
        """
        run_id = int(run["id"])
        params = self._request_params(run, token)
        fallback_url = str(run["repository_url"])
        attempts_this_page = 0
        while True:
            attempts_this_page += 1
            book.attempt_number += 1
            attempt_number = book.attempt_number
            attempted_at = iso_z(self._clock())
            try:
                response = self._fetch(run, token)
            except HttpRequestError as exc:
                retryable = is_retryable_failure(exc)
                sent_params = exc.params or params
                attempt_id = self.db.add_request_attempt(
                    run_id,
                    attempt_number=attempt_number,
                    request_url=exc.url or fallback_url,
                    verb="ListRecords",
                    request_params=sent_params,
                    page_target=page_target,
                    attempted_at=attempted_at,
                    outcome="http_error",
                    retryable=retryable,
                    http_status=exc.status_code,
                    retry_after=_retry_after_header(exc.headers),
                    reason=str(exc),
                    finished_at=iso_z(self._clock()),
                )
                if not retryable:
                    return _PageBatch(
                        error=exc, attempts=attempts_this_page, reason="not_retryable"
                    )
                if attempts_this_page >= self.retry.max_attempts:
                    return _PageBatch(
                        error=exc,
                        attempts=attempts_this_page,
                        interrupted=True,
                        reason="retry_exhausted",
                    )
                # Never replay a stale first-page shape for a continuation
                # request (or vice versa): the request parameters must match
                # the token this page was fetched with.
                if sent_params != params:
                    self.db.update_request_attempt(
                        attempt_id,
                        outcome="retry_shape_mismatch",
                        reason="retry_shape_mismatch",
                    )
                    return _PageBatch(
                        error=exc,
                        attempts=attempts_this_page,
                        interrupted=True,
                        reason="retry_shape_mismatch",
                    )
                now = self._clock()
                wait, raw, source = resolve_retry_wait(
                    exc,
                    now=now,
                    default_wait_seconds=self.retry.default_wait_seconds,
                    max_retry_after_seconds=self.retry.max_retry_after_seconds,
                )
                due_at = retry_deadline(now, wait)
                may_sleep, reason = self._wait_decision(wait, book)
                self.db.update_request_attempt(
                    attempt_id,
                    outcome="retry_scheduled" if may_sleep else "retry_deferred",
                    wait_seconds=wait if may_sleep else None,
                    due_at=due_at,
                    reason=f"{source}:{reason}",
                )
                if not may_sleep:
                    return _PageBatch(
                        error=exc,
                        attempts=attempts_this_page,
                        interrupted=True,
                        retry_not_before=due_at,
                        reason=reason,
                    )
                if wait > 0:
                    if not self._sleep_interruptible(
                        run_id, wait, page_target, due_at, attempt_number
                    ):
                        self.db.update_request_attempt(
                            attempt_id,
                            outcome="retry_interrupted",
                            reason=f"{source}:retry_interrupted",
                            finished_at=iso_z(self._clock()),
                        )
                        return _PageBatch(
                            error=exc,
                            attempts=attempts_this_page,
                            interrupted=True,
                            retry_not_before=due_at,
                            reason="retry_interrupted",
                        )
                    book.waited_total += wait
                continue

            # HTTP success: classify at the OAI protocol layer before archiving.
            try:
                envelope = parse_oai_response(
                    response.content, request_url=response.request_url
                )
            except OAIProtocolError as exc:
                self.db.add_request_attempt(
                    run_id,
                    attempt_number=attempt_number,
                    request_url=response.request_url,
                    verb=response.verb,
                    request_params=response.params,
                    page_target=page_target,
                    attempted_at=attempted_at,
                    outcome="oai_error",
                    http_status=response.status_code,
                    reason=f"{exc.code}: {exc.message}",
                    finished_at=iso_z(self._clock()),
                )
                return _PageBatch(
                    response=response,
                    protocol_error=exc,
                    attempts=attempts_this_page,
                )
            except Exception as exc:
                self.db.add_request_attempt(
                    run_id,
                    attempt_number=attempt_number,
                    request_url=response.request_url,
                    verb=response.verb,
                    request_params=response.params,
                    page_target=page_target,
                    attempted_at=attempted_at,
                    outcome="protocol_error",
                    http_status=response.status_code,
                    reason=str(exc),
                    finished_at=iso_z(self._clock()),
                )
                raise
            self.db.add_request_attempt(
                run_id,
                attempt_number=attempt_number,
                request_url=response.request_url,
                verb=response.verb,
                request_params=response.params,
                page_target=page_target,
                attempted_at=attempted_at,
                outcome="success",
                http_status=response.status_code,
                finished_at=iso_z(self._clock()),
            )
            return _PageBatch(
                response=response, envelope=envelope, attempts=attempts_this_page
            )

    def _wait_decision(self, wait: float, book: _RetryBook) -> tuple[bool, str]:
        """Decide whether to sleep or persist ``retry_not_before``.

        ``wait`` is already clamped to the per-attempt ceiling but has *not*
        been shortened to the deferred allowance: a deferred server-requested
        delay longer than the short allowance must be persisted, never
        silently shortened, so the operator resume still honors it.
        """
        if wait == 0:
            return True, "due_now"
        if book.waited_total + wait > self.retry.total_wait_seconds:
            return False, "retry_budget_exhausted"
        if self.retry.policy == "inline":
            return True, "inline_wait"
        if wait <= self.retry.deferred_wait_seconds:
            return True, "deferred_short_wait"
        return False, "retry_deferred"

    def _sleep_interruptible(
        self,
        run_id: int,
        wait: float,
        page_target: int,
        due_at: str,
        attempt_number: int,
    ) -> bool:
        try:
            self._sleep(wait)
        except KeyboardInterrupt:
            self.db.add_event(
                run_id,
                "retry_interrupted",
                level="warning",
                detail={"page": page_target, "attempt": attempt_number, "due_at": due_at},
            )
            return False
        self.db.add_event(
            run_id,
            "retry_slept",
            detail={
                "page": page_target,
                "attempt": attempt_number,
                "sleep_seconds": wait,
                "due_at": due_at,
            },
        )
        return True

    def _execute(
        self, run: dict[str, Any], *, token: str | None, pages_limit: int | None
    ) -> HarvestResult:
        run_id = int(run["id"])
        counters = {
            "page_count": int(run["page_count"]),
            "record_count": int(run["record_count"]),
            "created_count": int(run["created_count"]),
            "updated_count": int(run["updated_count"]),
            "unchanged_count": int(run["unchanged_count"]),
            "deleted_count": int(run["deleted_count"]),
            "error_count": int(run["error_count"]),
        }
        pages_this_call = 0
        checkpoint = token
        book = _RetryBook(
            attempt_number=len(self.db.request_attempts_for_run(run_id)),
        )
        total_attempts = 0
        try:
            while True:
                page_number = counters["page_count"] + 1
                batch = self._fetch_until_parsed(
                    run, token=token, page_target=page_number, book=book
                )
                total_attempts += batch.attempts
                if batch.interrupted:
                    self._interrupt_for_retry(run, batch, counters, checkpoint)
                    return _result_from_run(
                        self.db.get_run(run_id) or run, attempts=total_attempts
                    )
                if batch.error is not None:
                    raise batch.error
                assert batch.response is not None
                if (
                    batch.protocol_error is not None
                    and batch.protocol_error.code != "noRecordsMatch"
                ):
                    # HTTP 200 but a non-transient OAI protocol error (for
                    # example badResumptionToken): never retried and never
                    # archived as page evidence.
                    raise batch.protocol_error

                response = batch.response
                archived = self.archive.save(
                    run_id=run_id,
                    page_number=page_number,
                    content=response.content,
                    request_url=response.request_url,
                    params=response.params,
                    fetched_at=response.fetched_at,
                )
                self.db.add_raw_response(
                    run_id,
                    page_number=page_number,
                    verb=response.verb,
                    request_url=response.request_url,
                    fetched_at=response.fetched_at,
                    http_status=response.status_code,
                    sha256=archived.sha256,
                    size_bytes=archived.size_bytes,
                    archive_path=archived.relative_path,
                )

                if batch.protocol_error is not None:
                    # Only noRecordsMatch reaches here: every other HTTP 200
                    # OAI error was raised before archiving. A noRecordsMatch
                    # body is still valid evidence and completes the run.
                    exc = batch.protocol_error
                    counters["page_count"] += 1
                    self.db.save_progress(run_id, **counters, checkpoint_token=None)
                    self.db.add_event(
                        run_id,
                        "no_records_match",
                        detail={"page": page_number, "message": exc.message},
                    )
                    self.db.finish_run(run_id, "completed")
                    return _result_from_run(
                        self.db.get_run(run_id) or run, attempts=total_attempts
                    )

                assert batch.envelope is not None
                page = parse_record_page(batch.envelope.root)
                counters["page_count"] += 1
                page_counts = process_page(
                    self.db, run_id, page, response_path=archived.relative_path
                )
                for key, value in page_counts.items():
                    counters[key] += value
                next_token = page.token.value if page.token else None
                checkpoint = next_token
                self.db.save_progress(run_id, **counters, checkpoint_token=next_token)
                self.db.add_event(
                    run_id,
                    "page_harvested",
                    detail={
                        "page": page_number,
                        "records": len(page.records),
                        "issues": len(page.issues),
                        "archive_path": archived.relative_path,
                        "next_token": bool(next_token),
                        "attempts": batch.attempts,
                    },
                )
                if next_token is None:
                    self.db.finish_run(run_id, "completed")
                    self.db.add_event(run_id, "run_completed")
                    return _result_from_run(
                        self.db.get_run(run_id) or run, attempts=total_attempts
                    )
                pages_this_call += 1
                if pages_limit is not None and pages_this_call >= pages_limit:
                    self.db.mark_interrupted(
                        run_id, reason="max_pages", retry_not_before=None
                    )
                    self.db.add_event(
                        run_id,
                        "run_interrupted",
                        detail={
                            "reason": "达到 max_pages 限制",
                            "page_count": page_number,
                        },
                    )
                    return _result_from_run(
                        self.db.get_run(run_id) or run, attempts=total_attempts
                    )
                token = next_token
        except KeyboardInterrupt:
            self._best_effort_progress(run_id, counters, checkpoint)
            self.db.finish_run(run_id, "interrupted", last_error="用户中断")
            self.db.add_event(run_id, "run_interrupted", level="warning")
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", None) or type(exc).__name__
            message = str(exc)
            counters["error_count"] += 1
            self._best_effort_progress(run_id, counters, checkpoint)
            self.db.add_error(
                run_id,
                scope="run",
                message=message,
                code=str(error_code),
                detail={"exception": type(exc).__name__},
            )
            self.db.finish_run(run_id, "failed", last_error=message)
            self.db.add_event(
                run_id, "run_failed", level="error", detail={"message": message}
            )
            if isinstance(exc, HarvestError):
                raise
            raise HarvestError(f"收割失败: {message}", run_id=run_id) from exc

    def _interrupt_for_retry(
        self,
        run: dict[str, Any],
        batch: _PageBatch,
        counters: dict[str, int],
        checkpoint: str | None,
    ) -> None:
        run_id = int(run["id"])
        # The failed response is *not* a page: counters and the checkpoint stay
        # exactly as they were before this page's attempts began.
        self._best_effort_progress(run_id, counters, checkpoint)
        message = batch.reason
        if batch.error is not None:
            message = f"{batch.reason}: {batch.error}"
        self.db.mark_interrupted(
            run_id,
            reason=batch.reason,
            retry_not_before=batch.retry_not_before,
            last_error=message,
        )
        self.db.add_event(
            run_id,
            "run_retry_deferred",
            level="warning",
            detail={
                "reason": batch.reason,
                "retry_not_before": batch.retry_not_before,
                "attempts": batch.attempts,
                "page": counters["page_count"] + 1,
                "policy": self.retry.policy,
            },
        )

    def _best_effort_progress(
        self, run_id: int, counters: dict[str, int], checkpoint: str | None
    ) -> None:
        try:
            self.db.save_progress(run_id, **counters, checkpoint_token=checkpoint)
        except Exception:
            return


def _retry_after_header(headers: dict[str, str]) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            return str(value)
    return None


__all__ = ["HarvestResult", "Harvester", "ResumeTooEarly"]
