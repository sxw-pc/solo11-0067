from __future__ import annotations

from pathlib import Path

import pytest

from metaharvest.config import ConfigError, load_harvest_config, load_mock_config
from metaharvest.oai_errors import OAIProtocolError
from metaharvest.parser import (
    parse_identify,
    parse_list_sets,
    parse_metadata_formats,
    parse_oai_response,
    parse_record_element,
    parse_record_page,
    q,
)
from metaharvest.constants import OAI_NAMESPACE

from conftest import HARVEST_CONFIG_PATH, MOCK_CONFIG_PATH, PROJECT_ROOT


def get_bytes(client, **params) -> bytes:
    response = client.get("/oai", query_string=params)
    assert response.status_code == 200
    assert response.content_type.startswith("text/xml")
    return response.data


def expect_error(content: bytes, code: str) -> None:
    with pytest.raises(OAIProtocolError) as caught:
        parse_oai_response(content)
    assert caught.value.code == code


def collect_pages(client, **params):
    records = []
    token = None
    while True:
        query = (
            {"verb": params["verb"], "resumptionToken": token}
            if token
            else dict(params)
        )
        envelope = parse_oai_response(get_bytes(client, **query))
        page = parse_record_page(envelope.root)
        records.extend(page.records)
        token = page.token.value if page.token else None
        if not token:
            return records


def test_config_paths_are_relative_to_yaml():
    harvest = load_harvest_config(HARVEST_CONFIG_PATH)
    mock = load_mock_config(MOCK_CONFIG_PATH)
    assert harvest.storage.db_path == PROJECT_ROOT / "var" / "metaharvest.sqlite3"
    assert harvest.storage.archive_dir == PROJECT_ROOT / "var" / "archive"
    assert harvest.repository.metadata_prefix == "oai_dc"
    assert mock.seed_file == PROJECT_ROOT / "fixtures" / "oai_seed.yml"
    assert mock.page_size == 4


def test_mock_config_rejects_non_loopback_binding(tmp_path: Path):
    config = tmp_path / "bad.yml"
    config.write_text(
        """
server: {host: "0.0.0.0", port: 8787}
repository:
  name: "x"
  base_url: "http://0.0.0.0:8787/oai"
  admin_email: "admin@demo.invalid"
  earliest_datestamp: "2024-01-01T00:00:00Z"
seed: {path: "missing.yml"}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="只允许绑定"):
        load_mock_config(config)


def test_identify_formats_and_sets(api_client):
    identify = parse_identify(get_bytes(api_client, verb="Identify"))
    assert identify["protocolVersion"] == "2.0"
    assert identify["repositoryName"] == "示例机构仓储（离线演示）"
    assert identify["adminEmail"] == "admin@demo.invalid"
    assert identify["deletedRecord"] == "persistent"

    formats = parse_metadata_formats(get_bytes(api_client, verb="ListMetadataFormats"))
    assert formats[0]["metadataPrefix"] == "oai_dc"

    sets = parse_list_sets(get_bytes(api_client, verb="ListSets"))
    assert {entry["setSpec"] for entry in sets} == {"theses", "articles", "datasets"}


def test_pagination_and_deleted_header(api_client):
    records = collect_pages(api_client, verb="ListRecords", metadataPrefix="oai_dc")
    assert len(records) == 12
    assert len({record.identifier for record in records}) == 12
    deleted = [record for record in records if record.deleted]
    assert [record.identifier for record in deleted] == ["oai:demo.invalid:datasets/0006"]
    first = parse_oai_response(
        get_bytes(api_client, verb="ListRecords", metadataPrefix="oai_dc")
    )
    page = parse_record_page(first.root)
    assert len(page.records) == 4
    assert page.token is not None
    assert page.token.cursor == 0
    assert page.token.complete_list_size == 12


def test_filters_and_date_only_until_are_inclusive(api_client):
    records = collect_pages(
        api_client,
        verb="ListRecords",
        metadataPrefix="oai_dc",
        **{"from": "2024-05-20", "until": "2024-05-20"},
    )
    assert [record.identifier for record in records] == ["oai:demo.invalid:articles/0008"]

    theses = collect_pages(
        api_client, verb="ListRecords", metadataPrefix="oai_dc", set="theses"
    )
    assert len(theses) == 4
    assert all("theses" in record.set_specs for record in theses)


def test_get_record_identifiers_and_post(api_client):
    body = get_bytes(
        api_client,
        verb="GetRecord",
        identifier="oai:demo.invalid:theses/0001",
        metadataPrefix="oai_dc",
    )
    envelope = parse_oai_response(body)
    record_node = envelope.root.find(q(OAI_NAMESPACE, "GetRecord")).find(
        q(OAI_NAMESPACE, "record")
    )
    record = parse_record_element(record_node)
    assert record.identifier == "oai:demo.invalid:theses/0001"
    assert record.metadata["title"][0].startswith("面向小型机构仓储")

    identifiers = api_client.get(
        "/oai?verb=ListIdentifiers&metadataPrefix=oai_dc"
    ).data
    assert b"<metadata>" not in identifiers
    assert b"<identifier>oai:demo.invalid:theses/0001</identifier>" in identifiers

    posted = api_client.post("/oai", data={"verb": "Identify"})
    assert posted.status_code == 200
    assert parse_identify(posted.data)["protocolVersion"] == "2.0"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"verb": "Nope"}, "badVerb"),
        ({"verb": "ListRecords"}, "badArgument"),
        ({"verb": "ListRecords", "metadataPrefix": "marc21"}, "cannotDisseminateFormat"),
        ({"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": "unknown"}, "noRecordsMatch"),
        ({"verb": "ListRecords", "metadataPrefix": "oai_dc", "from": "not-a-date"}, "badArgument"),
        ({"verb": "GetRecord", "metadataPrefix": "oai_dc"}, "badArgument"),
        ({"verb": "GetRecord", "identifier": "missing", "metadataPrefix": "oai_dc"}, "idDoesNotExist"),
        ({"verb": "Identify", "extra": "1"}, "badArgument"),
    ],
)
def test_oai_errors(api_client, params, expected):
    expect_error(get_bytes(api_client, **params), expected)


def test_duplicate_arguments_and_tampered_token(api_client):
    duplicate = api_client.get("/oai?verb=Identify&verb=ListSets")
    expect_error(duplicate.data, "badArgument")
    first = parse_oai_response(
        get_bytes(api_client, verb="ListRecords", metadataPrefix="oai_dc")
    )
    token = parse_record_page(first.root).token
    assert token is not None
    tampered = token.value[:-1] + ("A" if token.value[-1] != "A" else "B")
    expect_error(
        get_bytes(
            api_client,
            verb="ListRecords",
            resumptionToken=tampered,
        ),
        "badResumptionToken",
    )
    expect_error(
        get_bytes(
            api_client,
            verb="ListRecords",
            metadataPrefix="oai_dc",
            resumptionToken=token.value,
        ),
        "badArgument",
    )
