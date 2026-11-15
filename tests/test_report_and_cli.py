from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from metaharvest.cli import main
from metaharvest.db import Database
from metaharvest.harvester import Harvester
from metaharvest.report import build_report, write_reports

from conftest import HARVEST_CONFIG_PATH, MOCK_CONFIG_PATH


def test_reports_include_integrity_and_detect_tampering(harvest_config_factory):
    config = harvest_config_factory()
    result = Harvester(config).start()
    db = Database(config.storage.db_path)
    output = write_reports(db, result.run_id, config.storage.report_dir)
    assert set(output.files) == {"json", "csv", "changes_csv", "markdown"}
    assert output.integrity["status"] == "ok"
    assert output.integrity["checked_files"] == 3

    payload = json.loads(output.files["json"].read_text(encoding="utf-8"))
    assert payload["run"]["id"] == result.run_id
    assert len(payload["records"]) == 12
    assert payload["integrity"]["status"] == "ok"
    # A full harvest changes every record, so the change list matches here.
    assert len(payload["changes"]) == 12
    assert payload["view"]["complete"] is True
    assert payload["view"]["snapshot_status"] == "complete"
    assert payload["view"]["snapshot_record_count"] == 12
    assert "原始响应完整性" in output.files["markdown"].read_text(encoding="utf-8")
    assert "完整快照" in output.files["markdown"].read_text(encoding="utf-8")
    assert "本次变更清单" in output.files["markdown"].read_text(encoding="utf-8")

    rows = list(
        csv.DictReader(io.StringIO(output.files["csv"].read_text(encoding="utf-8")))
    )
    assert len(rows) == 12
    assert rows[0]["identifier"].startswith("oai:demo.invalid:")
    assert rows[0]["creators"]
    change_rows = list(
        csv.DictReader(
            io.StringIO(output.files["changes_csv"].read_text(encoding="utf-8"))
        )
    )
    assert len(change_rows) == 12
    assert "outcome" in change_rows[0]

    raw = db.raw_responses_for_run(result.run_id)[0]
    evidence = config.storage.archive_dir / raw["archive_path"]
    with evidence.open("ab") as handle:
        handle.write(b"tampered")
    failed = build_report(db, result.run_id)
    assert failed["integrity"]["status"] == "failed"
    assert failed["integrity"]["sha256_mismatches"]


def test_cli_status_report_and_demo(harvest_config_factory, tmp_path: Path, capsys):
    config = harvest_config_factory()
    result = Harvester(config).start()
    assert main(["status", "--db", str(config.storage.db_path)]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["run_count"] == 1
    assert status_payload["runs"][0]["status"] == "completed"

    report_dir = tmp_path / "cli-reports"
    assert (
        main(
            [
                "report",
                "--db",
                str(config.storage.db_path),
                "--run-id",
                str(result.run_id),
                "--output-dir",
                str(report_dir),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report_payload = json.loads(capsys.readouterr().out)
    assert report_payload["integrity"]["status"] == "ok"
    assert Path(report_payload["files"]["json"]).is_file()

    demo_dir = tmp_path / "demo"
    assert (
        main(
            [
                "demo",
                "--workdir",
                str(demo_dir),
                "--mock-config",
                str(MOCK_CONFIG_PATH),
                "--harvest-config",
                str(HARVEST_CONFIG_PATH),
            ]
        )
        == 0
    )
    demo_payload = json.loads(capsys.readouterr().out)
    assert len(demo_payload["runs"]) == 3
    assert demo_payload["runs"][0]["harvest"]["record_count"] == 12
    assert demo_payload["runs"][2]["harvest"]["unchanged_count"] == 6
    assert all(run["integrity"] == "ok" for run in demo_payload["runs"])
    assert Path(demo_payload["session_dir"]).is_dir()
