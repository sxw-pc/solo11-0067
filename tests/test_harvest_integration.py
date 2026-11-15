from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from metaharvest.db import Database
from metaharvest.harvester import Harvester
from metaharvest.oai_errors import HarvestError


def audit_entries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_full_harvest_and_repeat_is_idempotent(harvest_config_factory):
    config = harvest_config_factory()
    result = Harvester(config).start()
    assert result.status == "completed"
    assert result.page_count == 3
    assert result.record_count == 12
    assert result.created_count == 11
    assert result.deleted_count == 1
    assert result.updated_count == 0
    assert result.checkpoint_token is None

    db = Database(config.storage.db_path)
    records = db.records.for_run(result.run_id)
    assert len(records) == 12
    deleted = [record for record in records if record["deleted"]]
    assert [record["identifier"] for record in deleted] == [
        "oai:demo.invalid:datasets/0006"
    ]
    active = next(
        record
        for record in records
        if record["identifier"] == "oai:demo.invalid:theses/0001"
    )
    assert active["title"].startswith("面向小型机构仓储")
    assert active["creators"] == ["示例作者甲", "示例作者乙"]
    assert active["set_specs"] == ["theses"]
    assert active["record_hash"]

    raw = db.raw_responses_for_run(result.run_id)
    assert len(raw) == 3
    for item in raw:
        path = config.storage.archive_dir / item["archive_path"]
        assert path.is_file()
        assert path.read_bytes().startswith(b"<?xml")
        assert item["http_status"] == 200
    assert all(entry["outcome"] != "blocked" for entry in audit_entries(config.storage.audit_log))
    assert all(
        entry["host"] in {"127.0.0.1", "localhost", "::1"}
        for entry in audit_entries(config.storage.audit_log)
    )

    repeat = Harvester(config).start()
    assert repeat.status == "completed"
    assert repeat.created_count == 0
    assert repeat.updated_count == 0
    assert repeat.deleted_count == 0
    assert repeat.unchanged_count == 12
    assert len(db.records.for_run(repeat.run_id)) == 12


def test_incremental_window_and_set_filter(harvest_config_factory):
    config = harvest_config_factory()
    Harvester(config).start()

    incremental = harvest_config_factory(from_date="2024-05-01T00:00:00Z")
    result = Harvester(incremental).start()
    assert result.page_count == 2
    assert result.record_count == 6
    assert result.unchanged_count == 6
    assert result.created_count == 0

    theses = harvest_config_factory(set_spec="theses", until_date="2024-03-31")
    from metaharvest.set_catalog import SetCatalog, resolve_catalog_client

    SetCatalog(
        Database(theses.storage.db_path), resolve_catalog_client(theses)
    ).refresh()
    subset = Harvester(theses).start()
    assert subset.record_count == 2
    assert subset.status == "completed"


def test_interrupted_run_resumes_from_checkpoint(harvest_config_factory):
    config = harvest_config_factory()
    first = Harvester(config).start(max_pages=1)
    assert first.status == "interrupted"
    assert first.page_count == 1
    assert first.created_count == 4
    assert first.checkpoint_token

    db = Database(config.storage.db_path)
    assert db.get_run(first.run_id)["status"] == "interrupted"
    resumed = Harvester(config).resume(first.run_id)
    assert resumed.status == "completed"
    assert resumed.page_count == 3
    assert resumed.record_count == 12
    assert resumed.created_count == 11
    assert resumed.deleted_count == 1
    assert len(db.raw_responses_for_run(first.run_id)) == 3
    assert db.get_run(first.run_id)["checkpoint_token"] is None


def test_external_repository_is_blocked_and_audited(harvest_config_factory):
    config = harvest_config_factory()
    external = replace(
        config,
        repository=replace(
            config.repository, base_url="http://repository.demo.invalid/oai"
        ),
    )
    with pytest.raises(HarvestError) as caught:
        Harvester(external).start()
    run = Database(config.storage.db_path).get_run(caught.value.run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert "仅允许访问本机" in run["last_error"]
    assert Database(config.storage.db_path).raw_responses_for_run(caught.value.run_id) == []
    entries = audit_entries(config.storage.audit_log)
    assert entries[-1]["outcome"] == "blocked"
    assert entries[-1]["host"] == "repository.demo.invalid"


def test_no_records_match_completes_with_raw_evidence(harvest_config_factory):
    config = harvest_config_factory(from_date="2030-01-01T00:00:00Z")
    result = Harvester(config).start()
    assert result.status == "completed"
    assert result.record_count == 0
    assert result.page_count == 1
    db = Database(config.storage.db_path)
    assert len(db.raw_responses_for_run(result.run_id)) == 1
    assert db.errors_for_run(result.run_id) == []
