"""Resumable OAI-PMH harvesting pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive import ArchiveStore
from .config import HarvestConfig
from .db import Database
from .http_client import HTTPClient
from .oai_client import OAIClient, OAIResponse
from .oai_errors import HarvestError, OAIProtocolError
from .page_processor import process_page
from .parser import parse_oai_response, parse_record_page
from .util import hash_payload


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
    snapshot_status: str = "none"
    snapshot_complete: bool = False
    snapshot_watermark: int | None = None

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
            "snapshot_status": self.snapshot_status,
            "snapshot_complete": self.snapshot_complete,
            "snapshot_watermark": self.snapshot_watermark,
        }


def _result_from_run(run: dict[str, Any]) -> HarvestResult:
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
        snapshot_status=str(run.get("snapshot_status") or "none"),
        snapshot_complete=str(run.get("snapshot_status") or "none") == "complete",
        snapshot_watermark=run.get("snapshot_watermark"),
    )


class Harvester:
    def __init__(
        self,
        config: HarvestConfig,
        *,
        db: Database | None = None,
        client: OAIClient | None = None,
        archive: ArchiveStore | None = None,
    ) -> None:
        self.config = config
        self.db = db or Database(config.storage.db_path)
        self.http = HTTPClient(
            timeout_seconds=config.http.timeout_seconds,
            user_agent=config.http.user_agent,
            audit_log=config.storage.audit_log,
        )
        self.client = client or OAIClient(config.repository.base_url, self.http)
        self.archive = archive or ArchiveStore(config.storage.archive_dir)
        self._client_injected = client is not None
        self._archive_injected = archive is not None

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
            },
        )
        run = self.db.get_run(run_id)
        assert run is not None
        return self._execute(run, token=None, pages_limit=pages_limit)

    def resume(self, run_id: int, *, max_pages: int | None = None) -> HarvestResult:
        run = self.db.get_run(run_id)
        if run is None:
            raise HarvestError(f"收割运行不存在: {run_id}", run_id=run_id)
        if run["status"] == "completed":
            raise HarvestError(f"运行 #{run_id} 已完成，无需续采", run_id=run_id)
        pages_limit = self._page_limit(max_pages)
        if not self._client_injected:
            self.client = OAIClient(str(run["repository_url"]), self.http)
        if not self._archive_injected:
            self.archive = ArchiveStore(Path(str(run["archive_root"])))
        self.db.add_event(run_id, "run_resumed", detail={"max_pages": pages_limit})
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

    def _page_limit(self, override: int | None) -> int | None:
        value = override if override is not None else self.config.max_pages
        if value is not None and int(value) <= 0:
            raise HarvestError("max_pages 必须大于 0")
        return int(value) if value is not None else None

    def _fetch(self, run: dict[str, Any], token: str | None) -> OAIResponse:
        return self.client.list_records(
            metadata_prefix=str(run["metadata_prefix"]),
            from_date=run["from_date"],
            until_date=run["until_date"],
            set_spec=run["set_spec"],
            resumption_token=token,
        )

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
        try:
            while True:
                response = self._fetch(run, token)
                page_number = counters["page_count"] + 1
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
                try:
                    envelope = parse_oai_response(
                        response.content, request_url=response.request_url
                    )
                    page = parse_record_page(envelope.root)
                except OAIProtocolError as exc:
                    if exc.code == "noRecordsMatch":
                        counters["page_count"] += 1
                        self.db.save_progress(
                            run_id, **counters, checkpoint_token=None
                        )
                        self.db.add_event(
                            run_id,
                            "no_records_match",
                            detail={"page": page_number, "message": exc.message},
                        )
                        self.db.finish_run(run_id, "completed")
                        return _result_from_run(self.db.get_run(run_id) or run)
                    raise

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
                    },
                )
                if next_token is None:
                    self.db.finish_run(run_id, "completed")
                    self.db.add_event(run_id, "run_completed")
                    return _result_from_run(self.db.get_run(run_id) or run)
                pages_this_call += 1
                if pages_limit is not None and pages_this_call >= pages_limit:
                    self.db.finish_run(run_id, "interrupted")
                    self.db.add_event(
                        run_id,
                        "run_interrupted",
                        detail={"reason": "达到 max_pages 限制", "page_count": page_number},
                    )
                    return _result_from_run(self.db.get_run(run_id) or run)
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

    def _best_effort_progress(
        self, run_id: int, counters: dict[str, int], checkpoint: str | None
    ) -> None:
        try:
            self.db.save_progress(run_id, **counters, checkpoint_token=checkpoint)
        except Exception:
            return
