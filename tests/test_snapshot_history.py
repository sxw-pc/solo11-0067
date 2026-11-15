"""Immutable run snapshots: history, deletes, resume, retries, and migration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from metaharvest.db import Database
from metaharvest.db_schema import SCHEMA_V1_SQL
from metaharvest.harvester import Harvester
from metaharvest.normalize import NormalizedRecord

from conftest import HARVEST_CONFIG_PATH, MOCK_CONFIG_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _record(
    identifier: str,
    title: str,
    *,
    deleted: bool = False,
    datestamp: str = "2024-01-01T00:00:00Z",
    creators: tuple[str, ...] = (),
) -> NormalizedRecord:
    return NormalizedRecord(
        identifier=identifier,
        datestamp=datestamp,
        set_specs=("demo",),
        deleted=deleted,
        title=title,
        creators=creators,
        subjects=(),
        description="",
        publisher="",
        contributors=(),
        date="",
        resource_type="",
        format="",
        identifiers=(),
        sources=(),
        language="",
        relations=(),
        coverage=(),
        rights="",
    )


def _run(db: Database) -> int:
    return db.create_run(
        repository_url="http://127.0.0.1/oai",
        metadata_prefix="oai_dc",
        set_spec=None,
        from_date=None,
        until_date=None,
        archive_root=Path("/tmp/archive"),
        config_hash="test",
    )


def _store(db: Database, run_id: int, records, *, path: str = "run-x/page-0001.xml"):
    outcomes = []
    for record in records:
        outcomes.append(
            db.records.upsert(record, run_id=run_id, response_path=path)
        )
    return outcomes


def _snapshot_map(db: Database, run_id: int) -> dict[str, dict]:
    snapshot = db.snapshot_for_run(run_id)
    assert snapshot is not None
    return {record["identifier"]: record for record in snapshot.records}


# ---------------------------------------------------------------------------
# Pure store-level semantics
# ---------------------------------------------------------------------------
def test_two_incremental_runs_with_unchanged_record_and_delete(tmp_path: Path):
    db = Database(tmp_path / "snap.sqlite3")

    first = _run(db)
    assert _store(
        db,
        first,
        [
            _record("A", "alpha v1", creators=("one",)),
            _record("B", "beta v1"),
            _record("C", "", deleted=True),
        ],
    ) == ["created", "created", "deleted"]
    db.finish_run(first, "completed")

    second = _run(db)
    # A is unchanged, B is updated, C is not seen in this incremental window.
    assert _store(
        db, second, [_record("A", "alpha v1", creators=("one",)), _record("B", "beta v2")]
    ) == ["unchanged", "updated"]
    db.finish_run(second, "completed")

    # Each run's complete snapshot includes inherited records.
    first_map = _snapshot_map(db, first)
    second_map = _snapshot_map(db, second)
    assert set(first_map) == {"A", "B", "C"}
    assert set(second_map) == {"A", "B", "C"}

    # Unchanged A in run 2 inherits run 1's version without being in changes.
    assert second_map["A"]["title"] == "alpha v1"
    assert second_map["A"]["version_run_id"] == first
    assert second_map["A"]["run_outcome"] == "unchanged"
    # B was updated in run 2.
    assert second_map["B"]["title"] == "beta v2"
    assert second_map["B"]["version_run_id"] == second
    assert second_map["B"]["changed_in_run"] is True
    # C is still a tombstone, inherited.
    assert second_map["C"]["deleted"] is True
    assert second_map["C"]["version_run_id"] == first

    # The change list for run 2 holds only B.
    changes = db.changes_for_run(second)
    assert [change["identifier"] for change in changes] == ["B"]
    assert changes[0]["outcome"] == "updated"

    # Run 1's snapshot is not rewritten by run 2.
    assert first_map["B"]["title"] == "beta v1"
    assert first_map["B"]["changed_in_run"] is True
    assert first_map["A"]["record_hash"] == second_map["A"]["record_hash"]


def test_deletion_tombstone_preserves_old_versions(tmp_path: Path):
    db = Database(tmp_path / "tombstone.sqlite3")

    first = _run(db)
    _store(db, first, [_record("A", "alive")])
    db.finish_run(first, "completed")

    second = _run(db)
    assert _store(db, second, [_record("A", "", deleted=True)]) == ["deleted"]
    db.finish_run(second, "completed")

    versions = db.versions_for_identifier("A")
    assert [version["outcome"] for version in versions] == ["created", "deleted"]
    assert versions[0]["title"] == "alive"
    assert versions[0]["deleted"] is False
    assert versions[1]["deleted"] is True
    assert versions[1]["title"] == ""

    # Old snapshot still shows the active record; new one shows the tombstone.
    assert _snapshot_map(db, first)["A"]["deleted"] is False
    assert _snapshot_map(db, first)["A"]["title"] == "alive"
    assert _snapshot_map(db, second)["A"]["deleted"] is True
    assert _snapshot_map(db, second)["A"]["version_run_id"] == second


def test_snapshots_do_not_drift_after_later_runs(tmp_path: Path):
    db = Database(tmp_path / "drift.sqlite3")

    run_ids = []
    for round_index, title in enumerate(("v1", "v2", "v3"), start=1):
        run_id = _run(db)
        _store(db, run_id, [_record("A", title), _record(f"R{round_index}", "x")])
        db.finish_run(run_id, "completed")
        run_ids.append(run_id)

    baseline = {run_id: _snapshot_map(db, run_id) for run_id in run_ids}
    # Query twice; results must be identical (no dependence on read time).
    again = {run_id: _snapshot_map(db, run_id) for run_id in run_ids}
    assert {
        run_id: {k: v["record_hash"] for k, v in records.items()}
        for run_id, records in baseline.items()
    } == {
        run_id: {k: v["record_hash"] for k, v in records.items()}
        for run_id, records in again.items()
    }
    assert baseline[run_ids[0]]["A"]["title"] == "v1"
    assert baseline[run_ids[1]]["A"]["title"] == "v2"
    assert baseline[run_ids[2]]["A"]["title"] == "v3"
    # Each snapshot accumulates the records created up to that point.
    assert len(baseline[run_ids[0]]) == 2
    assert len(baseline[run_ids[1]]) == 3
    assert len(baseline[run_ids[2]]) == 4


def test_interrupted_run_resumed_after_another_run_does_not_leak(tmp_path: Path):
    db = Database(tmp_path / "interleave.sqlite3")

    interrupted = _run(db)
    _store(db, interrupted, [_record("A", "a-from-run1")])
    db.finish_run(interrupted, "interrupted")

    other = _run(db)
    _store(db, other, [_record("A", "a-from-run2"), _record("B", "b-from-run2")])
    db.finish_run(other, "completed")

    # Resume the first run: it observes B as already current (unchanged).
    assert _store(db, interrupted, [_record("B", "b-from-run2")]) == ["unchanged"]
    db.finish_run(interrupted, "completed")

    interrupted_snapshot = _snapshot_map(db, interrupted)
    other_snapshot = _snapshot_map(db, other)

    # The resumed run never sees run 2's *update* to A (it inherited A@run1).
    assert interrupted_snapshot["A"]["title"] == "a-from-run1"
    assert interrupted_snapshot["A"]["version_run_id"] == interrupted
    # It does include B, which it observed directly.
    assert interrupted_snapshot["B"]["title"] == "b-from-run2"
    assert interrupted_snapshot["B"]["run_outcome"] == "unchanged"

    assert other_snapshot["A"]["title"] == "a-from-run2"
    assert other_snapshot["A"]["version_run_id"] == other


def test_retrying_checkpoint_page_does_not_duplicate_history(tmp_path: Path):
    db = Database(tmp_path / "retry.sqlite3")

    run_id = _run(db)
    _store(db, run_id, [_record("A", "v1"), _record("B", "b1")])
    # Simulate re-fetching the same uncommitted checkpoint page.
    _store(db, run_id, [_record("A", "v1"), _record("B", "b1")])
    db.finish_run(run_id, "completed")

    versions = db.versions_for_identifier("A")
    assert len(versions) == 1
    assert versions[0]["outcome"] == "created"

    snapshot = db.snapshot_for_run(run_id)
    assert snapshot is not None
    identifiers = [record["identifier"] for record in snapshot.records]
    assert identifiers == ["A", "B"]
    assert len(identifiers) == len(set(identifiers))
    assert snapshot.record_count == 2

    changes = db.changes_for_run(run_id)
    assert [change["identifier"] for change in changes] == ["A", "B"]


def test_reprocessed_page_with_corrected_content_updates_snapshot(tmp_path: Path):
    """A genuinely corrected page supersedes this run's earlier attempt."""
    db = Database(tmp_path / "corrected.sqlite3")

    run_id = _run(db)
    _store(db, run_id, [_record("A", "first-attempt")])
    # The checkpoint page is fetched again with corrected content.
    assert _store(db, run_id, [_record("A", "corrected")]) == ["updated"]
    db.finish_run(run_id, "completed")

    snapshot = db.snapshot_for_run(run_id)
    assert snapshot is not None
    assert snapshot.record_count == 1
    assert snapshot.records[0]["title"] == "corrected"
    assert snapshot.records[0]["run_outcome"] == "updated"
    changes = db.changes_for_run(run_id)
    assert len(changes) == 1
    assert changes[0]["title"] == "corrected"
    assert changes[0]["outcome"] == "updated"


def test_interrupted_and_failed_runs_are_not_complete_snapshots(tmp_path: Path):
    db = Database(tmp_path / "status.sqlite3")

    interrupted = _run(db)
    _store(db, interrupted, [_record("A", "a1")])
    db.finish_run(interrupted, "interrupted")
    interrupted_snapshot = db.snapshot_for_run(interrupted)
    assert interrupted_snapshot is not None
    assert interrupted_snapshot.complete is False
    assert interrupted_snapshot.status == "partial"
    # Even a partial run still exposes what it provably had.
    assert [record["identifier"] for record in interrupted_snapshot.records] == ["A"]

    failed = _run(db)
    _store(db, failed, [_record("A", "a1"), _record("B", "b1")])
    db.finish_run(failed, "failed", last_error="boom")
    failed_snapshot = db.snapshot_for_run(failed)
    assert failed_snapshot is not None
    assert failed_snapshot.complete is False
    assert failed_snapshot.status == "partial"

    # A running run has no frozen snapshot at all.
    running = _run(db)
    _store(db, running, [_record("C", "c1")])
    running_snapshot = db.snapshot_for_run(running)
    assert running_snapshot is not None
    assert running_snapshot.complete is False
    assert running_snapshot.status == "running"
    run_row = db.get_run(running)
    assert run_row is not None
    assert run_row["snapshot_watermark"] is None
    assert run_row["snapshot_status"] == "none"


def test_completed_run_snapshot_survives_empty_incremental_run(tmp_path: Path):
    db = Database(tmp_path / "empty.sqlite3")

    full = _run(db)
    _store(db, full, [_record("A", "a1"), _record("B", "b1")])
    db.finish_run(full, "completed")

    empty = _run(db)
    db.finish_run(empty, "completed")

    assert _snapshot_map(db, empty).keys() == {"A", "B"}
    assert db.changes_for_run(empty) == []
    snapshot = db.snapshot_for_run(empty)
    assert snapshot is not None
    assert snapshot.complete is True
    assert snapshot.record_count == 2


# ---------------------------------------------------------------------------
# Harvester integration: two real incremental runs, delete, failure then resume
# ---------------------------------------------------------------------------
def _seed_with_two_versions(tmp_path: Path, mock_config) -> Path:
    """Write a synthetic seed whose record changes between harvests."""
    from metaharvest.mock_data import load_seed

    base = load_seed(mock_config.seed_file)
    original = next(
        record for record in base.records if record.identifier.endswith("theses/0001")
    )
    path = tmp_path / "seed_versions.yml"
    path.write_text(
        "\n".join(
            [
                "sets:",
                '  - spec: "theses"',
                '    name: "示例学位论文"',
                '  - spec: "articles"',
                '    name: "示例期刊文章"',
                '  - spec: "datasets"',
                '    name: "示例研究数据集"',
                "records:",
                f'  - identifier: "{original.identifier}"',
                '    datestamp: "2024-02-01T09:00:00Z"',
                '    sets: ["theses"]',
                "    dc:",
                '      title: "第一版标题"',
                '      creator: ["作者甲"]',
                '  - identifier: "oai:demo.invalid:datasets/0006"',
                '    datestamp: "2024-04-15T16:20:00Z"',
                "    sets: [\"datasets\"]",
                "    deleted: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_harvester_full_then_incremental_reports_snapshot_vs_changes(
    harvest_config_factory, tmp_path: Path, mock_config
):
    seed_path = _seed_with_two_versions(tmp_path, mock_config)
    from metaharvest.mock_runtime import serve_mock

    instance = serve_mock(replace(mock_config, seed_file=seed_path))
    try:
        config = harvest_config_factory(base_url=instance.endpoint)
        full = Harvester(config).start()
        assert full.status == "completed"
        assert full.snapshot_complete is True
        assert full.snapshot_status == "complete"

        db = Database(config.storage.db_path)
        full_snapshot = db.snapshot_for_run(full.run_id)
        assert full_snapshot is not None
        assert full_snapshot.complete is True
        assert full_snapshot.record_count == 2
        assert full_snapshot.deleted_count == 1

        # Second run in the same window: no record changes at all.
        repeat = Harvester(config).start()
        assert repeat.unchanged_count == 2
        assert repeat.snapshot_complete is True
        repeat_snapshot = db.snapshot_for_run(repeat.run_id)
        assert repeat_snapshot is not None
        # Unchanged records still appear in the complete snapshot...
        assert repeat_snapshot.record_count == 2
        # ...but not in the change list.
        assert db.changes_for_run(repeat.run_id) == []
        # And the snapshot inherits the earlier versions.
        repeat_map = {r["identifier"]: r for r in repeat_snapshot.records}
        full_map = {r["identifier"]: r for r in full_snapshot.records}
        for identifier, record in repeat_map.items():
            assert record["record_hash"] == full_map[identifier]["record_hash"]
            assert record["version_run_id"] == full.run_id
            assert record["changed_in_run"] is False
    finally:
        instance.stop()


def test_failed_run_then_resume_snapshot_is_partial_then_complete(
    harvest_config_factory, live_mock, monkeypatch
):
    config = harvest_config_factory()
    first = Harvester(config).start(max_pages=1)
    assert first.status == "interrupted"
    assert first.snapshot_status == "partial"
    assert first.snapshot_complete is False

    db = Database(config.storage.db_path)
    partial = db.snapshot_for_run(first.run_id)
    assert partial is not None
    assert partial.complete is False
    assert partial.record_count == 4  # exactly the first page

    resumed = Harvester(config).resume(first.run_id)
    assert resumed.status == "completed"
    assert resumed.snapshot_status == "complete"
    assert resumed.snapshot_complete is True

    complete = db.snapshot_for_run(first.run_id)
    assert complete is not None
    assert complete.complete is True
    assert complete.record_count == 12
    # No duplicates from replaying the checkpoint page.
    identifiers = [record["identifier"] for record in complete.records]
    assert len(identifiers) == len(set(identifiers))


def test_failed_network_run_is_partial_and_retryable(
    harvest_config_factory, monkeypatch
):
    config = harvest_config_factory()
    Harvester(config).start()

    # Force a failure after the first page of a subsequent run.
    from metaharvest import harvester as harvester_module

    original = harvester_module.parse_record_page
    calls = {"count": 0}

    def flaky(root):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated mid-run failure")
        return original(root)

    monkeypatch.setattr(harvester_module, "parse_record_page", flaky)
    from metaharvest.oai_errors import HarvestError

    with pytest.raises(HarvestError) as caught:
        Harvester(config).start()
    run_id = caught.value.run_id
    assert run_id is not None

    db = Database(config.storage.db_path)
    run = db.get_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["snapshot_status"] == "partial"
    snapshot = db.snapshot_for_run(run_id)
    assert snapshot is not None
    assert snapshot.complete is False

    # Resuming the failed run must not be treated as already complete and must
    # produce a complete snapshot without duplicated history.
    monkeypatch.setattr(harvester_module, "parse_record_page", original)
    resumed = Harvester(config).resume(run_id)
    assert resumed.status == "completed"
    assert resumed.snapshot_complete is True
    final = db.snapshot_for_run(run_id)
    assert final is not None
    identifiers = [record["identifier"] for record in final.records]
    assert len(identifiers) == len(set(identifiers))


# ---------------------------------------------------------------------------
# CLI / report distinction
# ---------------------------------------------------------------------------
def test_cli_snapshot_and_changes_views(harvest_config_factory, capsys, tmp_path: Path):
    from metaharvest.cli import main

    config = harvest_config_factory()
    first = Harvester(config).start()
    assert main(["snapshot", "--db", str(config.storage.db_path), "--run-id", str(first.run_id)]) == 0
    snapshot_payload = json.loads(capsys.readouterr().out)
    assert snapshot_payload["view"] == "snapshot"
    assert snapshot_payload["complete"] is True
    assert snapshot_payload["status"] == "complete"
    assert len(snapshot_payload["records"]) == 12
    assert any(record["deleted"] for record in snapshot_payload["records"])

    # A repeat incremental run: snapshot has 12, changes has 0.
    incremental = harvest_config_factory(from_date="2024-05-01T00:00:00Z")
    second = Harvester(incremental).start()
    assert main(["snapshot", "--db", str(config.storage.db_path), "--run-id", str(second.run_id)]) == 0
    second_snapshot = json.loads(capsys.readouterr().out)
    assert len(second_snapshot["records"]) == 12

    assert (
        main(
            [
                "snapshot",
                "--db",
                str(config.storage.db_path),
                "--run-id",
                str(second.run_id),
                "--view",
                "changes",
            ]
        )
        == 0
    )
    changes_payload = json.loads(capsys.readouterr().out)
    assert changes_payload["view"] == "changes"
    assert changes_payload["record_count"] == 0
    assert changes_payload["records"] == []

    output = tmp_path / "snapshot.json"
    assert (
        main(
            [
                "snapshot",
                "--db",
                str(config.storage.db_path),
                "--run-id",
                str(first.run_id),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert json.loads(output.read_text(encoding="utf-8"))["record_count"] == 12


def test_cli_snapshot_partial_run_exits_nonzero(
    harvest_config_factory, capsys
):
    from metaharvest.cli import main

    config = harvest_config_factory()
    first = Harvester(config).start(max_pages=1)
    assert first.status == "interrupted"
    assert (
        main(
            ["snapshot", "--db", str(config.storage.db_path), "--run-id", str(first.run_id)]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is False
    assert payload["status"] == "partial"


def test_report_separates_snapshot_from_changes(harvest_config_factory):
    from metaharvest.report import build_report, write_reports

    config = harvest_config_factory()
    Harvester(config).start()
    second = Harvester(config).start()
    db = Database(config.storage.db_path)

    report = build_report(db, second.run_id)
    assert report["schema_version"] == 2
    assert report["view"]["complete"] is True
    assert report["view"]["snapshot_record_count"] == 12
    assert report["view"]["changed_record_count"] == 0
    assert len(report["records"]) == 12
    assert report["changes"] == []

    output = write_reports(db, second.run_id, config.storage.report_dir)
    markdown = output.files["markdown"].read_text(encoding="utf-8")
    assert "完整快照" in markdown
    assert "本次变更清单" in markdown
    assert "未变记录不会出现在这里" in markdown


# ---------------------------------------------------------------------------
# Legacy database migration
# ---------------------------------------------------------------------------
def _build_v1_database(
    path: Path,
    *,
    runs: list[dict],
    records: list[dict],
) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA_V1_SQL)
        for run in runs:
            connection.execute(
                """
                INSERT INTO harvest_runs (
                    id, repository_url, metadata_prefix, set_spec, from_date,
                    until_date, archive_root, config_hash, status, started_at,
                    finished_at, page_count, record_count, created_count,
                    updated_count, unchanged_count, deleted_count
                ) VALUES (?, 'http://127.0.0.1/oai', 'oai_dc', ?, ?, NULL,
                          '/tmp/archive', 'h', ?, ?, ?, 1, ?, 0, 0, 0, 0)
                """,
                (
                    run["id"],
                    run.get("set_spec"),
                    run.get("from_date"),
                    run["status"],
                    "2024-01-01T00:00:00Z",
                    "2024-01-01T00:01:00Z",
                    run.get("record_count", 0),
                ),
            )
        for record in records:
            connection.execute(
                """
                INSERT INTO records (
                    identifier, datestamp, set_specs, is_deleted, title,
                    record_hash, first_harvested_at, last_harvested_at,
                    last_run_id, last_response_path
                ) VALUES (?, '2024-01-01T00:00:00Z', '[]', 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["identifier"],
                    record["title"],
                    record["record_hash"],
                    "2024-01-01T00:00:00Z",
                    "2024-01-01T00:00:00Z",
                    record["last_run_id"],
                    "run-000001/page-0001.xml",
                ),
            )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def test_legacy_v1_database_migrates_without_fabricating_history(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    _build_v1_database(
        path,
        runs=[
            {"id": 1, "status": "completed", "record_count": 2},
            {"id": 2, "status": "completed", "record_count": 1},
        ],
        records=[
            # A was last touched by run 2; B by run 1.  A's run-1 content is
            # gone forever, so run 1 cannot be reconstructed exactly.
            {
                "identifier": "A",
                "title": "a-latest",
                "record_hash": "hash-a2",
                "last_run_id": 2,
            },
            {
                "identifier": "B",
                "title": "b-final",
                "record_hash": "hash-b1",
                "last_run_id": 1,
            },
        ],
    )

    db = Database(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"record_versions", "run_observations"} <= tables
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(harvest_runs)")
        }
        assert {
            "snapshot_status",
            "snapshot_watermark",
            "snapshot_base_watermark",
            "snapshot_record_count",
            "snapshot_frozen_at",
        } <= columns
        versions = connection.execute(
            "SELECT run_id, identifier, outcome FROM record_versions ORDER BY identifier"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in versions] == [
            (2, "A", "baseline"),
            (1, "B", "baseline"),
        ]
    finally:
        connection.close()

    # Existing data is preserved.
    assert db.records.get("A")["title"] == "a-latest"
    assert db.records.get("B")["title"] == "b-final"

    # Run 2 cannot be proven complete (its full set is unknown), neither can
    # run 1: both are flagged rather than silently presented as complete.
    for run_id in (1, 2):
        snapshot = db.snapshot_for_run(run_id)
        assert snapshot is not None
        assert snapshot.complete is False
        assert snapshot.status == "incomplete"
        assert snapshot.records == ()

    # New work after migration is fully versioned and complete.
    new_run = _run(db)
    assert _store(db, new_run, [_record("C", "c-new")]) == ["created"]
    db.finish_run(new_run, "completed")
    snapshot = db.snapshot_for_run(new_run)
    assert snapshot is not None
    assert snapshot.complete is True
    assert {record["identifier"] for record in snapshot.records} == {"A", "B", "C"}
    # A and B are carried forward from their baselines.
    mapping = {record["identifier"]: record for record in snapshot.records}
    assert mapping["A"]["title"] == "a-latest"
    assert mapping["B"]["title"] == "b-final"
    assert mapping["C"]["changed_in_run"] is True


def test_legacy_migration_is_idempotent(tmp_path: Path):
    path = tmp_path / "legacy-twice.sqlite3"
    _build_v1_database(
        path,
        runs=[{"id": 1, "status": "completed", "record_count": 1}],
        records=[
            {
                "identifier": "A",
                "title": "a",
                "record_hash": "hash-a",
                "last_run_id": 1,
            }
        ],
    )
    Database(path)
    connection = sqlite3.connect(path)
    try:
        first_count = connection.execute(
            "SELECT COUNT(*) FROM record_versions"
        ).fetchone()[0]
    finally:
        connection.close()

    Database(path)  # second open must not duplicate baselines
    connection = sqlite3.connect(path)
    try:
        second_count = connection.execute(
            "SELECT COUNT(*) FROM record_versions"
        ).fetchone()[0]
        assert first_count == second_count == 1
    finally:
        connection.close()


def test_legacy_full_harvest_still_last_observer_is_complete(tmp_path: Path):
    """A v1 full harvest that still owns every surviving row replays exactly."""
    path = tmp_path / "legacy-full.sqlite3"
    _build_v1_database(
        path,
        runs=[{"id": 1, "status": "completed", "record_count": 2}],
        records=[
            {
                "identifier": "A",
                "title": "a",
                "record_hash": "hash-a",
                "last_run_id": 1,
            },
            {
                "identifier": "B",
                "title": "b",
                "record_hash": "hash-b",
                "last_run_id": 1,
            },
        ],
    )
    db = Database(path)
    snapshot = db.snapshot_for_run(1)
    assert snapshot is not None
    assert snapshot.complete is True
    assert snapshot.status == "complete"
    assert {record["identifier"] for record in snapshot.records} == {"A", "B"}
    # Baselines are not reported as changes made by that run.
    assert all(record["changed_in_run"] is False for record in snapshot.records)
    assert db.changes_for_run(1) == []
