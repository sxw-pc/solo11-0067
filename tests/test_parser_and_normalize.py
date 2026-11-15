from __future__ import annotations

from dataclasses import replace

import pytest

from metaharvest.language import LocalizedValue
from metaharvest.normalize import normalize_record
from metaharvest.oai_errors import OAIProtocolError, RecordParseError, XMLParseError
from metaharvest.parser import (
    parse_oai_response,
    parse_record_page,
    parse_xml,
)

from conftest import PROJECT_ROOT

PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2024-08-01T10:00:00Z</responseDate>
  <request verb="ListRecords" metadataPrefix="oai_dc">http://127.0.0.1/oai</request>
  <ListRecords>
    <record>
      <header>
        <identifier>oai:demo.invalid:test/1</identifier>
        <datestamp>2024-07-01T00:00:00Z</datestamp>
        <setSpec>beta</setSpec>
        <setSpec>alpha</setSpec>
      </header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>  A   synthetic   title </dc:title>
          <dc:creator>Sample Author</dc:creator>
          <dc:creator> sample author </dc:creator>
          <dc:creator>Second Author</dc:creator>
          <dc:subject>Metadata</dc:subject>
          <dc:subject> metadata </dc:subject>
          <dc:description>Demo &amp; test</dc:description>
          <dc:identifier>urn:demo:1</dc:identifier>
        </oai_dc:dc>
      </metadata>
    </record>
    <record>
      <header status="deleted">
        <identifier>oai:demo.invalid:test/2</identifier>
        <datestamp>2024-07-02T00:00:00Z</datestamp>
      </header>
    </record>
    <record>
      <header>
        <datestamp>2024-07-03T00:00:00Z</datestamp>
      </header>
      <metadata>
        <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title>No identifier</dc:title>
        </oai_dc:dc>
      </metadata>
    </record>
    <resumptionToken cursor="1" completeListSize="5">next-token</resumptionToken>
  </ListRecords>
</OAI-PMH>
"""

ERROR_XML = """<?xml version="1.0"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2024-08-01T10:00:00Z</responseDate>
  <request verb="ListRecords" />
  <error code="noRecordsMatch">nothing here</error>
</OAI-PMH>
"""


def test_secure_parser_rejects_external_entities():
    fixture = (PROJECT_ROOT / "fixtures" / "malformed_record.xml").read_bytes()
    with pytest.raises(XMLParseError, match="安全策略"):
        parse_xml(fixture)


def test_record_page_extraction_issues_and_token():
    envelope = parse_oai_response(PAGE_XML.encode("utf-8"), request_url="local")
    page = parse_record_page(envelope.root)
    assert envelope.request_verb == "ListRecords"
    assert len(page.records) == 2
    assert len(page.issues) == 1
    assert page.issues[0].identifier is None
    assert page.token is not None
    assert page.token.value == "next-token"
    assert page.token.cursor == 1
    assert page.token.complete_list_size == 5

    active = page.records[0]
    assert active.identifier == "oai:demo.invalid:test/1"
    assert active.metadata["title"] == (LocalizedValue("A synthetic title", None),)
    assert active.metadata["description"] == (LocalizedValue("Demo & test", None),)
    assert active.set_specs == ("beta", "alpha")
    assert active.deleted is False
    assert page.records[1].deleted is True
    assert page.records[1].metadata == {}


def test_normalization_is_deterministic_and_idempotent():
    envelope = parse_oai_response(PAGE_XML.encode("utf-8"))
    parsed = parse_record_page(envelope.root).records[0]
    record = normalize_record(parsed)
    assert record.title == "A synthetic title"
    assert record.creators == ("Sample Author", "Second Author")
    assert record.subjects == ("Metadata",)
    assert record.set_specs == ("alpha", "beta")
    assert record.identifiers == ("urn:demo:1",)
    assert record.datestamp == "2024-07-01T00:00:00Z"
    assert normalize_record(parsed).record_hash == record.record_hash

    later = normalize_record(replace(parsed, datestamp="2024-09-09T09:09:09Z"))
    assert later.datestamp != record.datestamp
    assert later.record_hash == record.record_hash


def test_deleted_normalization_and_invalid_datestamp():
    envelope = parse_oai_response(PAGE_XML.encode("utf-8"))
    deleted = normalize_record(parse_record_page(envelope.root).records[1])
    assert deleted.deleted is True
    assert deleted.title == ""
    assert deleted.set_specs == ()

    active = parse_record_page(envelope.root).records[0]
    with pytest.raises(RecordParseError, match="datestamp"):
        normalize_record(replace(active, datestamp="not-a-date"))


def test_protocol_error_and_missing_request():
    with pytest.raises(OAIProtocolError) as caught:
        parse_oai_response(ERROR_XML.encode("utf-8"))
    assert caught.value.code == "noRecordsMatch"
    assert caught.value.message == "nothing here"

    missing = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"/>'
    with pytest.raises(XMLParseError, match="request"):
        parse_oai_response(missing)

    with pytest.raises(XMLParseError, match="根元素"):
        parse_oai_response(b"<root/>")
