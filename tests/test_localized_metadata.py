"""Language-qualified Dublin Core values: parsing, hashing, migration, reports."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest

from metaharvest.cli import main
from metaharvest.db import Database
from metaharvest.language import (
    LocalizedValue,
    select_localized,
)
from metaharvest.normalize import normalize_record
from metaharvest.parser import parse_oai_response, parse_record_page
from metaharvest.report import build_report, write_reports
from metaharvest.util import hash_payload

DC_OPEN = (
    '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)
DC_CLOSE = "</oai_dc:dc>"


def harvest_page(records_xml: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2024-08-01T10:00:00Z</responseDate>
  <request verb="ListRecords" metadataPrefix="oai_dc">http://127.0.0.1/oai</request>
  <ListRecords>{records_xml}</ListRecords>
</OAI-PMH>""".encode("utf-8")


def record_xml(
    identifier: str, metadata_inner: str | None, set_specs: tuple[str, ...] = ()
) -> str:
    header = (
        f"<identifier>{identifier}</identifier>"
        "<datestamp>2024-07-01T00:00:00Z</datestamp>"
        + "".join(f"<setSpec>{spec}</setSpec>" for spec in set_specs)
    )
    if metadata_inner is None:
        return f'<record><header status="deleted">{header}</header></record>'
    return (
        f"<record><header>{header}</header>"
        f"<metadata>{metadata_inner}</metadata></record>"
    )


def dc_block(inner: str, attrs: str = "") -> str:
    return f"{DC_OPEN}{f' {attrs}' if attrs else ''}>{inner}{DC_CLOSE}"


def parse_records(*records_xml: str):
    envelope = parse_oai_response(harvest_page("".join(records_xml)))
    return parse_record_page(envelope.root).records


def normalize_xml(*records_xml: str):
    return [normalize_record(parsed) for parsed in parse_records(*records_xml)]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state" / "harvest.sqlite3")


def make_run(database: Database, tmp_path: Path) -> int:
    return database.create_run(
        repository_url="http://127.0.0.1/oai",
        metadata_prefix="oai_dc",
        set_spec=None,
        from_date=None,
        until_date=None,
        archive_root=tmp_path,
        config_hash="test-config",
    )


# ---------------------------------------------------------------------------
# Parser: xml:lang inheritance, override, reset, absence
# ---------------------------------------------------------------------------


def test_lang_inherited_from_oai_dc_root_and_wrapper() -> None:
    (parsed,) = parse_records(
        record_xml(
            "oai:test:1",
            dc_block(
                '<dc:title>Harbor Guide</dc:title>'
                '<box xmlns="urn:example" xml:lang="fr">'
                "<dc:subject>Phares</dc:subject>"
                "</box>",
                'xml:lang="en"',
            ),
        )
    )
    assert parsed.metadata["title"] == (LocalizedValue("Harbor Guide", "en"),)
    assert parsed.metadata["subject"] == (LocalizedValue("Phares", "fr"),)


def test_lang_declared_on_metadata_element_and_child_override() -> None:
    (parsed,) = parse_records(
        record_xml(
            "oai:test:2",
            dc_block(
                '<dc:title xml:lang="zh">仓储指南</dc:title>'
                "<dc:title>Harbor Guide</dc:title>"
            ),
        ).replace("<metadata>", '<metadata xml:lang="en">')
    )
    assert parsed.metadata["title"] == (
        LocalizedValue("仓储指南", "zh"),
        LocalizedValue("Harbor Guide", "en"),
    )


def test_empty_xml_lang_resets_inherited_language() -> None:
    # The counterexample: root en, one child zh, one sibling explicitly reset.
    (parsed,) = parse_records(
        record_xml(
            "oai:test:3",
            dc_block(
                '<dc:title xml:lang="zh">仓储指南</dc:title>'
                '<dc:title xml:lang="">Harbor Guide</dc:title>'
                "<dc:creator>Harbor Team</dc:creator>",
                'xml:lang="en"',
            ),
        )
    )
    assert parsed.metadata["title"] == (
        LocalizedValue("仓储指南", "zh"),
        LocalizedValue("Harbor Guide", None),
    )
    assert parsed.metadata["creator"] == (LocalizedValue("Harbor Team", "en"),)


def test_absent_xml_lang_is_untagged_and_case_is_preserved() -> None:
    (parsed,) = parse_records(
        record_xml(
            "oai:test:4",
            dc_block(
                "<dc:title>Plain</dc:title>"
                '<dc:creator xml:lang="ZH-cmn-Hans-CN">某作者</dc:creator>'
            ),
        )
    )
    assert parsed.metadata["title"] == (LocalizedValue("Plain", None),)
    # Original tag spelling is kept for evidence.
    assert parsed.metadata["creator"] == (
        LocalizedValue("某作者", "ZH-cmn-Hans-CN"),
    )


# ---------------------------------------------------------------------------
# Normalization: duplicate semantics, compatibility fields, hash identity
# ---------------------------------------------------------------------------


def test_same_text_in_different_languages_is_not_collapsed() -> None:
    (record,) = normalize_xml(
        record_xml(
            "oai:test:5",
            dc_block(
                '<dc:creator xml:lang="en">Alice</dc:creator>'
                '<dc:creator xml:lang="zh">Alice</dc:creator>'
                '<dc:creator xml:lang="EN">Alice</dc:creator>'
                "<dc:creator>alice</dc:creator>"
            ),
        )
    )
    # en + zh survive; the EN repeat of the en value collapses (tags compare
    # case-insensitively) and so does the untagged repeat of the same text.
    assert record.localized["creator"] == (
        LocalizedValue("Alice", "en"),
        LocalizedValue("Alice", "zh"),
        LocalizedValue("alice", None),
    )
    assert record.creators == ("Alice", "Alice", "alice")


def test_untagged_normalization_matches_legacy_flattening() -> None:
    (record,) = normalize_xml(
        record_xml(
            "oai:test:6",
            dc_block(
                "<dc:title>  A   title </dc:title><dc:title>Second</dc:title>"
                "<dc:subject>Metadata</dc:subject><dc:subject> metadata </dc:subject>"
            ),
        )
    )
    assert record.title == "A title"
    assert record.subjects == ("Metadata",)
    assert record.localized["title"] == (
        LocalizedValue("A title", None),
        LocalizedValue("Second", None),
    )


def test_record_hash_tracks_language_tag_only_changes() -> None:
    inner_zh = dc_block('<dc:title xml:lang="zh">仓储指南</dc:title>')
    inner_ja = dc_block('<dc:title xml:lang="ja">仓储指南</dc:title>')
    inner_untagged = dc_block("<dc:title>仓储指南</dc:title>")
    (zh_record,) = normalize_xml(record_xml("oai:test:7", inner_zh))
    (ja_record,) = normalize_xml(record_xml("oai:test:7", inner_ja))
    (plain_record,) = normalize_xml(record_xml("oai:test:7", inner_untagged))
    (zh_again,) = normalize_xml(record_xml("oai:test:7", inner_zh))

    assert zh_record.title == ja_record.title == plain_record.title == "仓储指南"
    assert zh_record.record_hash == zh_again.record_hash
    assert zh_record.record_hash != ja_record.record_hash
    assert zh_record.record_hash != plain_record.record_hash


def test_upsert_treats_tag_only_change_as_update(db: Database, tmp_path: Path) -> None:
    run_id = make_run(db, tmp_path)
    (created_record,) = normalize_xml(
        record_xml("oai:test:8", dc_block('<dc:title xml:lang="zh">仓储指南</dc:title>'))
    )
    assert db.records.upsert(created_record, run_id=run_id, response_path="p1") == "created"
    assert db.records.upsert(created_record, run_id=run_id, response_path="p1") == "unchanged"

    (retagged,) = normalize_xml(
        record_xml("oai:test:8", dc_block('<dc:title xml:lang="ja">仓储指南</dc:title>'))
    )
    assert db.records.upsert(retagged, run_id=run_id, response_path="p2") == "updated"
    stored = db.records.get("oai:test:8")
    assert stored is not None
    assert stored["localized"] == {"title": [["仓储指南", "ja"]]}


def test_deleted_record_has_no_localized_values(db: Database, tmp_path: Path) -> None:
    run_id = make_run(db, tmp_path)
    (record,) = normalize_xml(record_xml("oai:test:9", None))
    assert record.deleted is True
    assert record.localized == {}
    assert db.records.upsert(record, run_id=run_id, response_path="p1") == "deleted"
    stored = db.records.get("oai:test:9")
    assert stored is not None
    assert stored["localized"] == {}
    assert stored["title"] == ""


# ---------------------------------------------------------------------------
# Shared language resolver
# ---------------------------------------------------------------------------


def test_resolver_exact_policy_falls_back_to_first_source_value() -> None:
    entries = [("Harbor Guide", "en"), ("仓储指南", "zh")]
    chosen = select_localized(entries, "zh-CN", "exact")
    assert chosen.texts == ("Harbor Guide", "仓储指南")
    assert chosen.matched == "first"


def test_resolver_language_range_matches_primary_subtag() -> None:
    entries = [("Harbor Guide", "en"), ("仓储指南", "zh")]
    chosen = select_localized(entries, "zh-CN", "language-range")
    assert chosen.texts == ("仓储指南",)
    assert chosen.matched == "primary-subtag"
    assert chosen.languages == ("zh",)


def test_resolver_tiers_and_case_insensitive_tags() -> None:
    entries = [("Guide", "EN"), ("指南", "zh"), ("Untagged", None)]
    exact = select_localized(entries, "en", "exact")
    assert exact.texts == ("Guide",) and exact.matched == "exact"

    untagged = select_localized([("指南", "zh"), ("Untagged", None)], "fr", "exact")
    assert untagged.texts == ("Untagged",) and untagged.matched == "untagged"

    empty = select_localized([], "en", "exact")
    assert empty.texts == () and empty.matched == "none"

    with pytest.raises(ValueError):
        select_localized(entries, "en", "bogus-policy")


# ---------------------------------------------------------------------------
# SQLite migration from the v1 schema
# ---------------------------------------------------------------------------

V1_SCHEMA = """
CREATE TABLE harvest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_url TEXT NOT NULL,
    metadata_prefix TEXT NOT NULL,
    set_spec TEXT,
    from_date TEXT,
    until_date TEXT,
    archive_root TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    checkpoint_token TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE records (
    identifier TEXT PRIMARY KEY,
    datestamp TEXT NOT NULL,
    set_specs TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    creators TEXT NOT NULL DEFAULT '[]',
    subjects TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    contributors TEXT NOT NULL DEFAULT '[]',
    date TEXT NOT NULL DEFAULT '',
    resource_type TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL DEFAULT '',
    identifiers TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT '',
    relations TEXT NOT NULL DEFAULT '[]',
    coverage TEXT NOT NULL DEFAULT '[]',
    rights TEXT NOT NULL DEFAULT '',
    record_hash TEXT NOT NULL,
    first_harvested_at TEXT NOT NULL,
    last_harvested_at TEXT NOT NULL,
    last_run_id INTEGER NOT NULL,
    last_response_path TEXT
);
"""

LEGACY_CONTENT = {
    "identifier": "oai:legacy:1",
    "set_specs": ["theses"],
    "deleted": False,
    "title": "Legacy Title",
    "creators": ["Alice", "Bob"],
    "subjects": ["Metadata"],
    "description": "Legacy description",
    "publisher": "",
    "contributors": [],
    "date": "2024",
    "resource_type": "",
    "format": "",
    "identifiers": ["urn:legacy:1"],
    "sources": [],
    "language": "en",
    "relations": [],
    "coverage": [],
    "rights": "",
}


def build_v1_database(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_SCHEMA)
        connection.execute("PRAGMA user_version = 1")
        cursor = connection.execute(
            "INSERT INTO harvest_runs (repository_url, metadata_prefix, archive_root,"
            " config_hash, status, started_at) VALUES ('http://127.0.0.1/oai',"
            " 'oai_dc', '/tmp/archive', 'cfg', 'completed', '2024-08-01T00:00:00Z')"
        )
        legacy_hash = hash_payload(LEGACY_CONTENT)
        connection.execute(
            """
            INSERT INTO records (
                identifier, datestamp, set_specs, is_deleted, title, creators,
                subjects, description, publisher, contributors, date,
                resource_type, format, identifiers, sources, language,
                relations, coverage, rights, record_hash,
                first_harvested_at, last_harvested_at, last_run_id,
                last_response_path
            ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, '', '[]', ?, '', '', ?, '[]', ?,
                      '[]', '[]', '', ?, '2024-08-01T00:00:00Z',
                      '2024-08-01T00:00:00Z', ?, 'run-1/page-1.xml')
            """,
            (
                "oai:legacy:1",
                "2024-07-01T00:00:00Z",
                '["theses"]',
                "Legacy Title",
                '["Alice","Bob"]',
                '["Metadata"]',
                "Legacy description",
                "2024",
                '["urn:legacy:1"]',
                "en",
                legacy_hash,
                int(cursor.lastrowid),
            ),
        )
        connection.execute(
            """
            INSERT INTO records (
                identifier, datestamp, set_specs, is_deleted, record_hash,
                first_harvested_at, last_harvested_at, last_run_id
            ) VALUES ('oai:legacy:deleted', '2024-07-02T00:00:00Z', '[]', 1,
                      'legacy-deleted-hash', '2024-08-01T00:00:00Z',
                      '2024-08-01T00:00:00Z', ?)
            """,
            (int(cursor.lastrowid),),
        )
        connection.commit()
    finally:
        connection.close()
    return legacy_hash


def test_v1_database_migrates_values_as_untagged(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    legacy_hash = build_v1_database(db_path)

    database = Database(db_path)
    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == 2

    record = database.records.get("oai:legacy:1")
    assert record is not None
    # Legacy values migrated as untagged; no language label invented.
    assert record["localized"] == {
        "title": [["Legacy Title", None]],
        "creator": [["Alice", None], ["Bob", None]],
        "subject": [["Metadata", None]],
        "description": [["Legacy description", None]],
        "date": [["2024", None]],
        "identifier": [["urn:legacy:1", None]],
        "language": [["en", None]],
    }
    # Hash recomputed with the language-aware formula.
    assert record["record_hash"] != legacy_hash

    deleted = database.records.get("oai:legacy:deleted")
    assert deleted is not None
    assert deleted["deleted"] is True
    assert deleted["localized"] == {}

    # A re-harvest of the same untagged content is recognized as unchanged.
    run_id = make_run(database, tmp_path)
    (fresh,) = normalize_xml(
        record_xml(
            "oai:legacy:1",
            dc_block(
                "<dc:title>Legacy Title</dc:title>"
                "<dc:creator>Alice</dc:creator><dc:creator>Bob</dc:creator>"
                "<dc:subject>Metadata</dc:subject>"
                "<dc:description>Legacy description</dc:description>"
                "<dc:date>2024</dc:date>"
                "<dc:identifier>urn:legacy:1</dc:identifier>"
                "<dc:language>en</dc:language>"
            ),
            ("theses",),
        )
    )
    assert fresh.record_hash == record["record_hash"]
    assert (
        database.records.upsert(fresh, run_id=run_id, response_path="p1")
        == "unchanged"
    )

    # Reopening does not re-migrate or corrupt localized values.
    again = Database(db_path)
    assert again.records.get("oai:legacy:1")["localized"] == record["localized"]


def test_migrated_database_keeps_default_report_shape(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    build_v1_database(db_path)
    database = Database(db_path)
    run_id = 1  # inserted by build_v1_database

    report = build_report(database, run_id)
    assert "language_preference" not in report
    legacy_records = {item["identifier"]: item for item in report["records"]}
    active = legacy_records["oai:legacy:1"]
    assert "localized" not in active
    assert "language_selection" not in active
    assert active["title"] == "Legacy Title"
    assert active["creators"] == ["Alice", "Bob"]
    assert legacy_records["oai:legacy:deleted"]["title"] == ""


# ---------------------------------------------------------------------------
# Reports: explicit preferred-language selection across JSON/CSV/Markdown
# ---------------------------------------------------------------------------

LOCALIZED_PAGE = record_xml(
    "oai:report:1",
    dc_block(
        '<dc:title xml:lang="en">Harbor Guide</dc:title>'
        '<dc:title xml:lang="zh">仓储指南</dc:title>'
        '<dc:creator xml:lang="en">Harbor Team</dc:creator>'
        '<dc:creator xml:lang="zh">仓储团队</dc:creator>'
        "<dc:subject>metadata</dc:subject>"
    ),
) + record_xml(
    "oai:report:2",
    dc_block("<dc:title>Plain Title</dc:title><dc:creator>Alice</dc:creator>"),
) + record_xml("oai:report:3", None)


@pytest.fixture()
def localized_db(db: Database, tmp_path: Path) -> tuple[Database, int]:
    run_id = make_run(db, tmp_path)
    for record in normalize_xml(LOCALIZED_PAGE):
        db.records.upsert(record, run_id=run_id, response_path="run/page-1.xml")
    return db, run_id


def test_default_report_ignores_language_tags(
    localized_db: tuple[Database, int], tmp_path: Path
) -> None:
    database, run_id = localized_db
    output = write_reports(database, run_id, tmp_path / "reports")

    payload = json.loads(output.files["json"].read_text(encoding="utf-8"))
    assert "language_preference" not in payload
    records = {item["identifier"]: item for item in payload["records"]}
    first = records["oai:report:1"]
    assert "localized" not in first
    assert "language_selection" not in first
    assert first["title"] == "Harbor Guide"
    assert first["creators"] == ["Harbor Team", "仓储团队"]
    deleted = records["oai:report:3"]
    assert deleted["title"] == "" and deleted["deleted"] is True

    rows = list(
        csv.DictReader(io.StringIO(output.files["csv"].read_text(encoding="utf-8")))
    )
    assert rows[0]["identifier"] == "oai:report:1"
    assert rows[0]["title"] == "Harbor Guide"
    assert rows[0]["creators"] == "Harbor Team; 仓储团队"
    markdown = output.files["markdown"].read_text(encoding="utf-8")
    assert "Harbor Guide" in markdown


def test_exact_policy_falls_back_to_first_source_value(
    localized_db: tuple[Database, int], tmp_path: Path
) -> None:
    database, run_id = localized_db
    output = write_reports(
        database,
        run_id,
        tmp_path / "reports",
        preferred_language="zh-CN",
        policy="exact",
    )
    payload = json.loads(output.files["json"].read_text(encoding="utf-8"))
    assert payload["language_preference"] == {"requested": "zh-CN", "policy": "exact"}

    records = {item["identifier"]: item for item in payload["records"]}
    first = records["oai:report:1"]
    # No zh-CN or untagged title: the exact policy keeps the first source value.
    assert first["title"] == "Harbor Guide"
    assert first["language_selection"]["title"] == {
        "matched": "first",
        "languages": ["en", "zh"],
    }
    assert first["language_selection"]["subject"] == {
        "matched": "untagged",
        "languages": [None],
    }
    # Untagged record resolves through the untagged tier, display unchanged.
    plain = records["oai:report:2"]
    assert plain["title"] == "Plain Title"
    assert plain["language_selection"]["title"]["matched"] == "untagged"
    # Deleted records stay empty.
    deleted = records["oai:report:3"]
    assert deleted["title"] == "" and deleted["language_selection"] == {}

    rows = list(
        csv.DictReader(io.StringIO(output.files["csv"].read_text(encoding="utf-8")))
    )
    assert rows[0]["title"] == "Harbor Guide"
    assert rows[0]["creators"] == "Harbor Team; 仓储团队"
    markdown = output.files["markdown"].read_text(encoding="utf-8")
    assert "Harbor Guide" in markdown


def test_language_range_policy_selects_primary_subtag(
    localized_db: tuple[Database, int], tmp_path: Path
) -> None:
    database, run_id = localized_db
    output = write_reports(
        database,
        run_id,
        tmp_path / "reports",
        preferred_language="zh-CN",
        policy="language-range",
    )
    payload = json.loads(output.files["json"].read_text(encoding="utf-8"))
    assert payload["language_preference"] == {
        "requested": "zh-CN",
        "policy": "language-range",
    }

    records = {item["identifier"]: item for item in payload["records"]}
    first = records["oai:report:1"]
    assert first["title"] == "仓储指南"
    assert first["creators"] == ["仓储团队"]
    assert first["language_selection"]["title"] == {
        "matched": "primary-subtag",
        "languages": ["zh"],
    }
    # Localized evidence stays available in the JSON report.
    assert first["localized"]["title"] == [["Harbor Guide", "en"], ["仓储指南", "zh"]]

    # CSV and Markdown show the same selected values as JSON.
    rows = list(
        csv.DictReader(io.StringIO(output.files["csv"].read_text(encoding="utf-8")))
    )
    assert rows[0]["title"] == "仓储指南"
    assert rows[0]["creators"] == "仓储团队"
    assert rows[1]["title"] == "Plain Title"
    markdown = output.files["markdown"].read_text(encoding="utf-8")
    assert "仓储指南" in markdown
    assert "Harbor Guide" not in markdown.split("## 记录")[1]


def test_cli_report_preferred_language(
    localized_db: tuple[Database, int], tmp_path: Path, capsys
) -> None:
    database, run_id = localized_db
    db_path = database.path
    report_dir = tmp_path / "cli-reports"
    assert (
        main(
            [
                "report",
                "--db",
                str(db_path),
                "--run-id",
                str(run_id),
                "--output-dir",
                str(report_dir),
                "--format",
                "json",
                "--preferred-language",
                "zh-CN",
                "--language-policy",
                "language-range",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = json.loads((report_dir / f"harvest-run-{run_id:06d}.json").read_text())
    assert payload["language_preference"]["policy"] == "language-range"
    titles = {item["identifier"]: item["title"] for item in payload["records"]}
    assert titles["oai:report:1"] == "仓储指南"

    # --language-policy without --preferred-language is rejected.
    assert (
        main(
            [
                "report",
                "--db",
                str(db_path),
                "--run-id",
                str(run_id),
                "--output-dir",
                str(report_dir),
                "--language-policy",
                "exact",
            ]
        )
        == 1
    )
