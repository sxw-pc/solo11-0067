"""One-command, fully local end-to-end demonstration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import StorageConfig, load_harvest_config, load_mock_config
from .db import Database
from .harvester import HarvestResult, Harvester
from .mock_runtime import running_mock
from .report import ReportOutput, write_reports
from .util import short_token, timestamp_slug


@dataclass(frozen=True)
class DemoSummary:
    session_dir: Path
    endpoint: str
    runs: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_dir": str(self.session_dir),
            "endpoint": self.endpoint,
            "runs": list(self.runs),
        }


def _result_payload(
    label: str, result: HarvestResult, report: ReportOutput
) -> dict[str, Any]:
    return {
        "label": label,
        "harvest": result.as_dict(),
        "reports": {key: str(path) for key, path in report.files.items()},
        "integrity": report.integrity["status"],
    }


def run_demo(
    *,
    workdir: Path,
    mock_config_path: Path,
    harvest_config_path: Path,
) -> DemoSummary:
    template = load_harvest_config(harvest_config_path)
    mock_config = load_mock_config(mock_config_path)
    session_dir = (
        workdir.expanduser().resolve()
        / f"demo-{timestamp_slug()}-{short_token()}"
    )
    session_dir.mkdir(parents=True, exist_ok=False)
    db_path = session_dir / "metaharvest.sqlite3"
    archive_dir = session_dir / "archive"
    report_dir = session_dir / "reports"
    audit_log = session_dir / "audit" / "requests.jsonl"
    storage = StorageConfig(
        db_path=db_path,
        archive_dir=archive_dir,
        report_dir=report_dir,
        audit_log=audit_log,
    )
    summaries: list[dict[str, Any]] = []

    with running_mock(mock_config) as mock:
        base = replace(
            template,
            repository=replace(template.repository, base_url=mock.endpoint),
            storage=storage,
            max_pages=None,
        )
        full = Harvester(base).start()
        full_report = write_reports(Database(db_path), full.run_id, report_dir)
        summaries.append(_result_payload("full", full, full_report))

        incremental_repo = replace(
            base.repository,
            base_url=mock.endpoint,
            from_date="2024-05-01T00:00:00Z",
        )
        incremental_config = replace(base, repository=incremental_repo)
        incremental = Harvester(incremental_config).start()
        incremental_report = write_reports(
            Database(db_path), incremental.run_id, report_dir
        )
        summaries.append(
            _result_payload("incremental", incremental, incremental_report)
        )

        repeat = Harvester(incremental_config).start()
        repeat_report = write_reports(Database(db_path), repeat.run_id, report_dir)
        summaries.append(_result_payload("repeat_unchanged", repeat, repeat_report))

    return DemoSummary(
        session_dir=session_dir,
        endpoint=mock.endpoint,
        runs=tuple(summaries),
    )
