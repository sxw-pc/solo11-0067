"""Tests for repository-scoped set catalogs and harvest admission."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from metaharvest.cli import main
from metaharvest.config import HarvestConfig, StorageConfig, load_harvest_config
from metaharvest.db import Database
from metaharvest.harvester import Harvester
from metaharvest.http_client import HTTPClient
from metaharvest.oai_client import OAIClient, OAIResponse
from metaharvest.oai_errors import (
    OAIProtocolError,
    SetAdmissionError,
    SetCatalogError,
)
from metaharvest.set_catalog import (
    ADMISSION_NOT_APPLICABLE,
    ADMISSION_PINNED_CONFLICT,
    ADMISSION_UNVERIFIED,
    ADMISSION_VERIFIED,
    STATE_AVAILABLE,
    STATE_CONFLICT,
    STATE_INVALID,
    SetCatalog,
)

from conftest import HARVEST_CONFIG_PATH, PROJECT_ROOT


# --------------------------------------------------------------------------
# A scripted ListSets repository: each response is (content, request_url)
# --------------------------------------------------------------------------


def set_page_xml(
    entries: list[tuple[str, str, str]],
    *,
    token: str | None = None,
    error: str | None = None,
) -> bytes:
    node = ""
    for spec, name, description in entries:
        parts = [f"<setSpec>{spec}</setSpec>", f"<setName>{name}</setName>"]
        if description:
            parts.append(f"<setDescription>{description}</setDescription>")
        node += "<set>" + "".join(parts) + "</set>"
    error_node = (
        f'<error code="{error}">no match</error>' if error else ""
    )
    token_node = f"<resumptionToken>{token}</resumptionToken>" if token else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<responseDate>2024-08-01T10:00:00Z</responseDate>"
        '<request verb="ListSets">http://repo.invalid/oai</request>'
        f"{error_node}<ListSets>{node}{token_node}</ListSets>"
        "</OAI-PMH>"
    ).encode("utf-8")


class ScriptedClient:
    """Minimal OAIClient stand-in that replays scripted ListSets pages."""

    def __init__(self, base_url: str, pages: list[bytes], *, fail_after: int | None = None):
        self.base_url = base_url
        self._pages = list(pages)
        self._fail_after = fail_after
        self.calls: list[str | None] = []

    def list_sets(self, *, resumption_token: str | None = None) -> OAIResponse:
        self.calls.append(resumption_token)
        if self._fail_after is not None and len(self.calls) > self._fail_after:
            raise ConnectionError("simulated network failure")
        if not self._pages:
            raise ConnectionError("script exhausted")
        content = self._pages.pop(0)
        return OAIResponse(
            verb="ListSets",
            request_url=f"{self.base_url}?verb=ListSets",
            status_code=200,
            content=content,
            headers={},
            fetched_at="2024-08-01T10:00:00Z",
            params={"verb": "ListSets"},
        )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "catalog.sqlite3")


def make_catalog(db: Database, pages, *, policy="strict", base_url="http://repo.invalid/oai", fail_after=None):
    client = ScriptedClient(base_url, pages, fail_after=fail_after)
    return SetCatalog(db, client, conflict_policy=policy), client


# --------------------------------------------------------------------------
# Parser / client surface
# --------------------------------------------------------------------------


def test_parser_returns_resumption_token_and_descriptions():
    from metaharvest.parser import parse_list_sets

    page = parse_list_sets(
        set_page_xml(
            [("a", "Set A", "desc-a"), ("a/b", "Child", "")], token="tok-2"
        )
    )
    assert [entry.spec for entry in page.sets] == ["a", "a/b"]
    assert page.sets[0].description == "desc-a"
    assert page.sets[1].name == "Child"
    assert page.token is not None and page.token.value == "tok-2"


def test_oai_client_threads_resumption_token_through_list_sets():
    calls: list[dict[str, str]] = []

    class FakeHTTP:
        def get(self, url, params):
            calls.append(dict(params))
            from metaharvest.http_client import HttpResponse

            return HttpResponse(
                url=url, status_code=200, content=b"", headers={}, fetched_at="t"
            )

    client = OAIClient("http://127.0.0.1/oai", FakeHTTP())
    client.list_sets()
    client.list_sets(resumption_token="abc")
    assert calls == [{"verb": "ListSets"}, {"verb": "ListSets", "resumptionToken": "abc"}]


# --------------------------------------------------------------------------
# Multi-page, duplicates, hierarchy, empty directory
# --------------------------------------------------------------------------


def test_multi_page_enumeration_dedupes_and_marks_available(db):
    catalog, client = make_catalog(
        db,
        [
            set_page_xml([("root", "Root", ""), ("root/child", "Child", "")], token="t2"),
            set_page_xml([("root", "Root", ""), ("root/child", "Child", "")]),
        ],
    )
    result = catalog.refresh()
    assert result.admitted is True
    assert client.calls == [None, "t2"]
    assert result.generation.status == "active"
    assert result.generation.page_count == 2
    assert result.generation.set_count == 2
    assert result.generation.conflict_count == 0

    members = {member.set_spec: member for member in catalog.members(result.generation.id)}
    assert members["root"].state == STATE_AVAILABLE
    assert members["root"].occurrences == 2
    assert members["root/child"].occurrences == 2


def test_cross_page_duplicate_with_identical_identity_is_available(db):
    catalog, _ = make_catalog(
        db,
        [
            set_page_xml([("same", "Same", "d")], token="t2"),
            set_page_xml([("same", "Same", "d")]),
        ],
    )
    result = catalog.refresh()
    member = catalog.member(result.generation.id, "same")
    assert member is not None
    assert member.state == STATE_AVAILABLE
    assert result.admitted is True


def test_cross_page_conflict_is_marked_conflict(db):
    catalog, _ = make_catalog(
        db,
        [
            set_page_xml([("dup", "First Name", "first")], token="t2"),
            set_page_xml([("dup", "Second Name", "second")]),
        ],
        policy="active",
    )
    result = catalog.refresh()
    assert result.admitted is True  # route B activates the generation
    member = catalog.member(result.generation.id, "dup")
    assert member is not None
    assert member.state == STATE_CONFLICT
    assert len(member.versions) == 2
    assert result.generation.conflict_count == 1
    # Every page occurrence is retained as evidence.
    occurrences = catalog.occurrences(result.generation.id)
    assert [(item.page_number, item.set_name) for item in occurrences] == [
        (1, "First Name"),
        (2, "Second Name"),
    ]


def test_hierarchical_spec_with_absent_parent_is_still_cataloged(db):
    catalog, _ = make_catalog(
        db, [set_page_xml([("parent/child/grand", "Grand", "")])]
    )
    result = catalog.refresh()
    member = catalog.member(result.generation.id, "parent/child/grand")
    assert member is not None
    assert member.state == STATE_AVAILABLE
    assert catalog.admit("http://repo.invalid/oai", "parent/child/grand").status == (
        ADMISSION_VERIFIED
    )


def test_listsets_no_records_match_publishes_empty_active_directory(db):
    catalog, _ = make_catalog(db, [set_page_xml([], error="noRecordsMatch")])
    result = catalog.refresh()
    assert result.admitted is True
    assert result.generation.set_count == 0
    assert result.generation.page_count == 1
    assert catalog.members(result.generation.id) == []
    with pytest.raises(SetAdmissionError):
        catalog.admit("http://repo.invalid/oai", "anything")
    # An empty directory is still a complete directory: bypass works but marks it.
    decision = catalog.admit(
        "http://repo.invalid/oai", "anything", allow_unverified=True
    )
    assert decision.status == ADMISSION_UNVERIFIED


def test_missing_setspec_is_invalid_and_never_selectable(db):
    catalog, _ = make_catalog(db, [set_page_xml([("", "No Spec", "")])])
    result = catalog.refresh()
    member = catalog.member(result.generation.id, "")
    assert member is not None
    assert member.state == STATE_INVALID
    with pytest.raises(SetAdmissionError, match="setSpec"):
        catalog.admit("http://repo.invalid/oai", "")


# --------------------------------------------------------------------------
# Failure handling: never replace the previous active generation
# --------------------------------------------------------------------------


def test_failed_page_keeps_previous_active_generation(db):
    catalog, _ = make_catalog(db, [set_page_xml([("stable", "Stable", "")])])
    first = catalog.refresh()
    assert first.generation.status == "active"

    failing, _ = make_catalog(
        db,
        [set_page_xml([("stable", "Stable", "")], token="t2")],
        fail_after=1,
    )
    with pytest.raises(SetCatalogError, match="未发布"):
        failing.refresh()

    active = catalog.active_generation("http://repo.invalid/oai")
    assert active is not None
    assert active.id == first.generation.id
    assert active.generation == first.generation.generation
    # The set still admits against the old generation.
    assert catalog.admit("http://repo.invalid/oai", "stable").status == ADMISSION_VERIFIED
    members = {member.set_spec for member in catalog.members(active.id)}
    assert members == {"stable"}


def test_malformed_xml_page_keeps_previous_active_generation(db):
    catalog, _ = make_catalog(db, [set_page_xml([("stable", "Stable", "")])])
    first = catalog.refresh()
    bad, _ = make_catalog(db, [b"<not-oai/>"])
    with pytest.raises(SetCatalogError):
        bad.refresh()
    assert catalog.active_generation("http://repo.invalid/oai").id == first.generation.id


def test_repeated_resumption_token_is_rejected_without_publishing(db):
    catalog, _ = make_catalog(db, [set_page_xml([("x", "X", "")])])
    first = catalog.refresh()
    looping, _ = make_catalog(
        db,
        [
            set_page_xml([("x", "X", "")], token="same"),
            set_page_xml([("x", "X", "")], token="same"),
        ],
    )
    with pytest.raises(SetCatalogError, match="重复"):
        looping.refresh()
    assert catalog.active_generation("http://repo.invalid/oai").id == first.generation.id


# --------------------------------------------------------------------------
# Route A (strict) vs Route B (active) conflict admission
# --------------------------------------------------------------------------


def _conflicting_pages():
    return [
        set_page_xml([("dup", "A", "desc-a"), ("ok", "OK", "")], token="t2"),
        set_page_xml([("dup", "B", "desc-b")]),
    ]


def test_strict_policy_rejects_conflicted_generation_and_keeps_old_active(db):
    good, _ = make_catalog(db, [set_page_xml([("ok", "OK", "")])])
    base = good.refresh()

    strict, _ = make_catalog(db, _conflicting_pages(), policy="strict")
    result = strict.refresh()
    assert result.admitted is False
    assert result.generation.status == "rejected"
    assert "身份不一致" in result.reason

    active = strict.active_generation("http://repo.invalid/oai")
    assert active is not None and active.id == base.generation.id
    # The new generation exists for evidence but is not active.
    latest = strict.latest_generation("http://repo.invalid/oai")
    assert latest is not None and latest.status == "rejected"
    # Unambiguous sets still selectable via the old active generation.
    assert strict.admit("http://repo.invalid/oai", "ok").status == ADMISSION_VERIFIED


def test_active_policy_isolates_conflict_and_admits_clean_members(db):
    catalog, _ = make_catalog(db, _conflicting_pages(), policy="active")
    result = catalog.refresh()
    assert result.admitted is True
    assert result.generation.status == "active"

    # Clean member is immediately usable.
    assert catalog.admit("http://repo.invalid/oai", "ok").status == ADMISSION_VERIFIED
    # Conflict member requires a pinned description version.
    with pytest.raises(SetAdmissionError, match="身份不一致"):
        catalog.admit("http://repo.invalid/oai", "dup")

    pinned = catalog.admit(
        "http://repo.invalid/oai", "dup", pinned_set_name="A", pinned_description="desc-a"
    )
    assert pinned.status == ADMISSION_PINNED_CONFLICT
    assert pinned.set_name == "A"
    assert pinned.description == "desc-a"
    assert pinned.version_id

    wrong = "deadbeef"
    with pytest.raises(SetAdmissionError):
        catalog.admit("http://repo.invalid/oai", "dup", pinned_version_id=wrong)

    by_name = catalog.admit("http://repo.invalid/oai", "dup", pinned_set_name="B")
    assert by_name.status == ADMISSION_PINNED_CONFLICT
    assert by_name.description == "desc-b"
    assert by_name.version_id


def test_active_policy_ambiguity_in_partial_pin_is_rejected(db):
    catalog, _ = make_catalog(
        db,
        [
            set_page_xml([("dup", "Same", "one")], token="t2"),
            set_page_xml([("dup", "Same", "two")]),
        ],
        policy="active",
    )
    catalog.refresh()
    # The name is shared by both versions, so it alone cannot identify one.
    with pytest.raises(SetAdmissionError, match="身份不一致"):
        catalog.admit("http://repo.invalid/oai", "dup", pinned_set_name="Same")


def test_active_policy_pins_conflict_by_version_id(db):
    catalog, _ = make_catalog(db, _conflicting_pages(), policy="active")
    result = catalog.refresh()
    member = catalog.member(result.generation.id, "dup")
    assert member is not None and len(member.versions) == 2
    for version_id in member.versions:
        decision = catalog.admit(
            "http://repo.invalid/oai", "dup", pinned_version_id=version_id
        )
        assert decision.status == ADMISSION_PINNED_CONFLICT
        assert decision.version_id == version_id


# --------------------------------------------------------------------------
# Repository isolation
# --------------------------------------------------------------------------


def test_same_set_spec_in_other_repository_does_not_admit(db):
    catalog, _ = make_catalog(
        db, [set_page_xml([("shared", "Shared", "")])], base_url="http://a.invalid/oai"
    )
    catalog.refresh()
    decision = catalog.admit("http://a.invalid/oai", "shared")
    assert decision.status == ADMISSION_VERIFIED

    # Repository B has a complete catalog, but it does not contain "shared".
    other, _ = make_catalog(
        db, [set_page_xml([("other", "Other", "")])], base_url="http://b.invalid/oai"
    )
    other.refresh()
    with pytest.raises(SetAdmissionError, match="不属于"):
        other.admit("http://b.invalid/oai", "shared")

    # Without any catalog at all the failure is "no catalog", not "absent set".
    with pytest.raises(SetAdmissionError, match="尚无已发布的完整集合目录"):
        catalog.admit("http://c.invalid/oai", "shared", allow_unverified=False)

    # Bypass for another repository is explicitly recorded as unverified.
    bypassed = catalog.admit(
        "http://c.invalid/oai", "shared", allow_unverified=True
    )
    assert bypassed.status == ADMISSION_UNVERIFIED


# --------------------------------------------------------------------------
# Deleted sets: gone from options, old runs keep their snapshot
# --------------------------------------------------------------------------


def test_deleted_set_rejected_for_new_run_but_old_run_keeps_explanation(db):
    catalog, _ = make_catalog(
        db,
        [
            set_page_xml(
                [("theses", "Theses", "thesis set"), ("articles", "Articles", "")]
            )
        ],
    )
    first = catalog.refresh()
    decision = catalog.admit("http://repo.invalid/oai", "theses")
    assert decision.status == ADMISSION_VERIFIED

    # Simulate a completed run that stored its catalog snapshot.
    run_id = db.create_run(
        repository_url=decision.repository_url,
        metadata_prefix="oai_dc",
        set_spec=decision.set_spec,
        from_date=None,
        until_date=None,
        archive_root=Path("/tmp/archive"),
        config_hash="h",
        set_catalog_generation_id=decision.generation_id,
        set_catalog_generation=decision.generation,
        set_catalog_status=decision.status,
        set_catalog_set_name=decision.set_name,
        set_catalog_set_description=decision.description,
        set_catalog_set_state=decision.state,
        set_catalog_version_id=decision.version_id,
    )
    db.finish_run(run_id, "completed")

    # The set disappears from the repository.
    removal, _ = make_catalog(db, [set_page_xml([("articles", "Articles", "")])])
    removal.refresh()

    with pytest.raises(SetAdmissionError, match="不属于"):
        removal.admit("http://repo.invalid/oai", "theses")
    assert {member.set_spec for member in removal.members(
        removal.active_generation("http://repo.invalid/oai").id
    )} == {"articles"}

    # The old run still explains exactly what it harvested.
    run = db.get_run(run_id)
    assert run["set_catalog_generation"] == first.generation.generation
    assert run["set_catalog_set_name"] == "Theses"
    assert run["set_catalog_set_description"] == "thesis set"
    assert run["set_catalog_status"] == ADMISSION_VERIFIED


# --------------------------------------------------------------------------
# End to end through the CLI and the mock repository
# --------------------------------------------------------------------------


def _cli_config(tmp_path: Path, endpoint: str, **repository_overrides) -> Path:
    """Write a harvest config pointing at an ephemeral mock and return its path."""
    template = load_harvest_config(HARVEST_CONFIG_PATH)
    repository = replace(
        template.repository, base_url=endpoint, **repository_overrides
    )
    config = replace(
        template,
        repository=repository,
        storage=StorageConfig(
            db_path=tmp_path / "cli.sqlite3",
            archive_dir=tmp_path / "archive",
            report_dir=tmp_path / "reports",
            audit_log=tmp_path / "audit" / "requests.jsonl",
        ),
        max_pages=None,
    )
    path = tmp_path / "harvest.yml"
    path.write_text(
        "repository:\n"
        f'  base_url: "{config.repository.base_url}"\n'
        f'  metadata_prefix: "{config.repository.metadata_prefix}"\n'
        f"  set_spec: {json.dumps(config.repository.set_spec)}\n"
        "storage:\n"
        f'  db_path: "{config.storage.db_path}"\n'
        f'  archive_dir: "{config.storage.archive_dir}"\n'
        f'  report_dir: "{config.storage.report_dir}"\n'
        f'  audit_log: "{config.storage.audit_log}"\n',
        encoding="utf-8",
    )
    return path


def test_cli_harvest_requires_catalog_before_set_run(tmp_path: Path, live_mock, capsys):
    config_path = _cli_config(tmp_path, live_mock.endpoint)
    assert main(["harvest", "--config", str(config_path), "--set", "theses"]) == 1
    assert "集合目录" in capsys.readouterr().err
    assert Database(tmp_path / "cli.sqlite3").list_runs() == []

    assert main(["catalog", "refresh", "--config", str(config_path)]) == 0
    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["admitted"] is True
    assert refreshed["generation"]["set_count"] == 3
    assert refreshed["generation"]["status"] == "active"

    assert main(["harvest", "--config", str(config_path), "--set", "theses"]) == 0
    harvested = json.loads(capsys.readouterr().out)
    assert harvested["status"] == "completed"
    assert harvested["record_count"] == 4
    assert harvested["set_catalog"]["status"] == ADMISSION_VERIFIED
    assert harvested["set_catalog"]["set_name"] == "示例学位论文"

    assert (
        main(
            [
                "report",
                "--config",
                str(config_path),
                "--run-id",
                str(harvested["run_id"]),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    report_body = json.loads(Path(report["files"]["json"]).read_text(encoding="utf-8"))
    assert report_body["set_catalog"]["status"] == ADMISSION_VERIFIED
    assert report_body["set_catalog"]["set_name"] == "示例学位论文"
    assert report_body["set_catalog"]["generation"] == 1


def test_cli_catalog_refresh_pages_and_show_reports_members(tmp_path: Path, live_mock, capsys):
    config_path = _cli_config(tmp_path, live_mock.endpoint)
    assert main(["catalog", "refresh", "--config", str(config_path)]) == 0
    refreshed = json.loads(capsys.readouterr().out)
    # The mock page_size is 4 and there are 3 sets, so a single page suffices
    # for this seed; paging itself is covered by the scripted tests.
    assert refreshed["generation"]["page_count"] >= 1

    assert main(["catalog", "show", "--config", str(config_path)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["generation"]["status"] == "active"
    specs = {member["set_spec"] for member in shown["members"]}
    assert specs == {"theses", "articles", "datasets"}
    assert all(member["state"] == STATE_AVAILABLE for member in shown["members"])
    assert shown["pages"]


def test_cli_unknown_set_is_blocked_before_creating_a_run(tmp_path: Path, live_mock, capsys):
    config_path = _cli_config(tmp_path, live_mock.endpoint)
    assert main(["catalog", "refresh", "--config", str(config_path)]) == 0
    capsys.readouterr()
    assert main(["harvest", "--config", str(config_path), "--set", "nope"]) == 1
    capsys.readouterr()
    db = Database(tmp_path / "cli.sqlite3")
    assert db.list_runs() == []


def test_cli_bypass_marks_run_unverified(tmp_path: Path, live_mock, capsys):
    config_path = _cli_config(tmp_path, live_mock.endpoint)
    assert (
        main(
            [
                "harvest",
                "--config",
                str(config_path),
                "--set",
                "theses",
                "--allow-unverified-set",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["set_catalog"]["status"] == ADMISSION_UNVERIFIED
    db = Database(tmp_path / "cli.sqlite3")
    run = db.get_run(payload["run_id"])
    assert run["set_catalog_status"] == ADMISSION_UNVERIFIED
    assert run["set_catalog_generation_id"] is None


def test_cli_no_set_run_records_not_applicable(tmp_path: Path, live_mock, capsys):
    config_path = _cli_config(tmp_path, live_mock.endpoint)
    assert main(["harvest", "--config", str(config_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["set_catalog"]["status"] == ADMISSION_NOT_APPLICABLE


def test_listrecords_no_records_match_is_still_a_completed_empty_run(tmp_path: Path, live_mock):
    from metaharvest.set_catalog import resolve_catalog_client

    template = load_harvest_config(HARVEST_CONFIG_PATH)
    config = replace(
        template,
        repository=replace(
            template.repository,
            base_url=live_mock.endpoint,
            set_spec="theses",
            from_date="2030-01-01T00:00:00Z",
        ),
        storage=StorageConfig(
            db_path=tmp_path / "no-records.sqlite3",
            archive_dir=tmp_path / "archive",
            report_dir=tmp_path / "reports",
            audit_log=tmp_path / "audit" / "requests.jsonl",
        ),
    )
    db = Database(config.storage.db_path)
    SetCatalog(db, resolve_catalog_client(config)).refresh()
    harvester = Harvester(config)
    admission = harvester.admit_set()
    result = harvester.start(admission=admission)
    assert result.status == "completed"
    assert result.record_count == 0
    assert result.page_count == 1
    assert db.errors_for_run(result.run_id) == []
    events = {event["event"] for event in db.events_for_run(result.run_id)}
    assert "no_records_match" in events
    # The set admission still succeeded; this is not an empty catalog.
    run = db.get_run(result.run_id)
    assert run["set_catalog_status"] == ADMISSION_VERIFIED
    assert run["set_catalog_generation"] is not None


def test_library_start_enforces_admission_for_set_runs(tmp_path: Path, live_mock):
    from metaharvest.set_catalog import resolve_catalog_client

    template = load_harvest_config(HARVEST_CONFIG_PATH)
    config = replace(
        template,
        repository=replace(
            template.repository, base_url=live_mock.endpoint, set_spec="theses"
        ),
        storage=StorageConfig(
            db_path=tmp_path / "lib.sqlite3",
            archive_dir=tmp_path / "archive",
            report_dir=tmp_path / "reports",
            audit_log=tmp_path / "audit" / "requests.jsonl",
        ),
    )
    db = Database(config.storage.db_path)
    with pytest.raises(SetAdmissionError):
        Harvester(config).start()
    assert db.list_runs() == []

    SetCatalog(db, resolve_catalog_client(config)).refresh()
    result = Harvester(config).start()
    assert result.status == "completed"
    assert db.get_run(result.run_id)["set_catalog_status"] == ADMISSION_VERIFIED

    # A caller that opts out still gets an explicit unverified snapshot.
    bypass = Harvester(
        replace(config, storage=replace(config.storage, db_path=tmp_path / "bypass.sqlite3"))
    ).start(allow_unverified_set=True)
    bypass_db = Database(tmp_path / "bypass.sqlite3")
    assert bypass_db.get_run(bypass.run_id)["set_catalog_status"] == ADMISSION_UNVERIFIED


def test_pinned_conflict_is_recorded_in_the_run_snapshot(tmp_path: Path, live_mock):
    template = load_harvest_config(HARVEST_CONFIG_PATH)
    config = replace(
        template,
        repository=replace(
            template.repository, base_url=live_mock.endpoint, set_spec="theses"
        ),
        storage=StorageConfig(
            db_path=tmp_path / "pinned.sqlite3",
            archive_dir=tmp_path / "archive",
            report_dir=tmp_path / "reports",
            audit_log=tmp_path / "audit" / "requests.jsonl",
        ),
        set_catalog_conflict_policy="active",
    )
    db = Database(config.storage.db_path)
    # The scripted catalog claims a conflicting identity for "theses" while
    # pointing at the live mock, so the harvest itself still returns records.
    client = ScriptedClient(
        live_mock.endpoint,
        [
            set_page_xml([("theses", "Theses", "first")], token="t2"),
            set_page_xml([("theses", "Theses", "second")]),
        ],
    )
    SetCatalog(db, client, conflict_policy="active").refresh()

    harvester = Harvester(config)
    with pytest.raises(SetAdmissionError):
        harvester.start()

    harvester = Harvester(config)
    admission = harvester.admit_set(
        pinned_set_name="Theses", pinned_set_description="second"
    )
    assert admission.status == ADMISSION_PINNED_CONFLICT
    result = harvester.start(admission=admission)
    assert result.status == "completed"
    run = db.get_run(result.run_id)
    assert run["set_catalog_status"] == ADMISSION_PINNED_CONFLICT
    assert run["set_catalog_set_state"] == STATE_CONFLICT
    assert run["set_catalog_set_description"] == "second"
    assert run["set_catalog_version_id"] == admission.version_id


def test_run_snapshot_survives_catalog_change(tmp_path: Path, live_mock):
    from metaharvest.set_catalog import resolve_catalog_client

    template = load_harvest_config(HARVEST_CONFIG_PATH)
    config = replace(
        template,
        repository=replace(
            template.repository, base_url=live_mock.endpoint, set_spec="theses"
        ),
        storage=StorageConfig(
            db_path=tmp_path / "snapshot.sqlite3",
            archive_dir=tmp_path / "archive",
            report_dir=tmp_path / "reports",
            audit_log=tmp_path / "audit" / "requests.jsonl",
        ),
    )
    db = Database(config.storage.db_path)
    catalog = SetCatalog(db, resolve_catalog_client(config))
    catalog.refresh()
    harvester = Harvester(config)
    result = harvester.start(admission=harvester.admit_set())
    run = db.get_run(result.run_id)
    generation_id = run["set_catalog_generation_id"]
    assert run["set_catalog_set_name"] == "示例学位论文"
    assert run["set_catalog_set_description"] == ""

    # A later refresh supersedes the generation, but the run snapshot persists.
    catalog.refresh()
    later = db.get_run(result.run_id)
    assert later["set_catalog_generation_id"] == generation_id
    assert later["set_catalog_set_name"] == "示例学位论文"


def test_real_mock_listsets_pagination_over_http(tmp_path: Path, mock_config):
    """Page size 1 over the mock forces multiple ListSets requests with tokens."""
    from dataclasses import replace as dc_replace

    from metaharvest.mock_runtime import serve_mock
    from metaharvest.set_catalog import resolve_catalog_client

    paged_config = dc_replace(mock_config, page_size=1)
    instance = serve_mock(paged_config)
    try:
        template = load_harvest_config(HARVEST_CONFIG_PATH)
        config = replace(
            template,
            repository=replace(template.repository, base_url=instance.endpoint),
            storage=StorageConfig(
                db_path=tmp_path / "paged.sqlite3",
                archive_dir=tmp_path / "archive",
                report_dir=tmp_path / "reports",
                audit_log=tmp_path / "audit" / "requests.jsonl",
            ),
        )
        db = Database(config.storage.db_path)
        catalog = SetCatalog(db, resolve_catalog_client(config))
        result = catalog.refresh()
        assert result.admitted is True
        assert result.generation.page_count == 3
        assert result.generation.set_count == 3
        assert len(catalog.pages(result.generation.id)) == 3
        specs = {member.set_spec for member in catalog.members(result.generation.id)}
        assert specs == {"theses", "articles", "datasets"}
        assert catalog.admit(instance.endpoint, "articles").status == ADMISSION_VERIFIED
    finally:
        instance.stop()


def test_catalog_config_options_are_validated(tmp_path: Path):
    from metaharvest.config import ConfigError, load_harvest_config

    template = (PROJECT_ROOT / "configs" / "harvest_demo.yml").read_text(encoding="utf-8")

    good = tmp_path / "good.yml"
    good.write_text(
        template.replace('conflict_policy: "strict"', 'conflict_policy: "active"'),
        encoding="utf-8",
    )
    config = load_harvest_config(good)
    assert config.set_catalog_conflict_policy == "active"
    assert config.verify_sets is True

    bad = tmp_path / "bad.yml"
    bad.write_text(
        template.replace('conflict_policy: "strict"', 'conflict_policy: "loose"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="conflict_policy"):
        load_harvest_config(bad)

    bad_verify = tmp_path / "bad_verify.yml"
    bad_verify.write_text(
        template.replace("verify_sets: true", "verify_sets: yes please"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="verify_sets"):
        load_harvest_config(bad_verify)


def test_existing_v1_database_is_migrated(tmp_path: Path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
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
        PRAGMA user_version = 1;
        """
    )
    connection.commit()
    connection.close()

    db = Database(path)
    run_id = db.create_run(
        repository_url="http://repo.invalid/oai",
        metadata_prefix="oai_dc",
        set_spec="s",
        from_date=None,
        until_date=None,
        archive_root=tmp_path / "archive",
        config_hash="h",
        set_catalog_generation=7,
        set_catalog_status=ADMISSION_VERIFIED,
        set_catalog_set_name="Legacy",
    )
    run = db.get_run(run_id)
    assert run["set_catalog_generation"] == 7
    assert run["set_catalog_set_name"] == "Legacy"
    with sqlite3.connect(path) as check:
        version = check.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
