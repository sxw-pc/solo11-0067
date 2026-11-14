"""Secure OAI-PMH XML parsing and Dublin Core extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from .constants import DC_NAMESPACE, OAI_DC_NAMESPACE, OAI_NAMESPACE
from .oai_errors import OAIProtocolError, RecordParseError, XMLParseError
from .util import collapse_ws


def q(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


@dataclass(frozen=True)
class ResumptionToken:
    value: str
    expiration_date: str | None = None
    complete_list_size: int | None = None
    cursor: int | None = None


@dataclass(frozen=True)
class ParsedRecord:
    identifier: str
    datestamp: str
    set_specs: tuple[str, ...]
    deleted: bool
    metadata: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class RecordIssue:
    identifier: str | None
    code: str
    message: str


@dataclass(frozen=True)
class RecordPage:
    records: tuple[ParsedRecord, ...]
    issues: tuple[RecordIssue, ...]
    token: ResumptionToken | None


@dataclass(frozen=True)
class OAIEnvelope:
    response_date: str
    request_verb: str
    request_attributes: Mapping[str, str]
    request_url: str
    root: object


def parse_xml(content: bytes) -> object:
    if not content or not content.strip():
        raise XMLParseError("响应体为空")
    try:
        return SafeET.fromstring(content)
    except DefusedXmlException as exc:
        raise XMLParseError(f"响应 XML 被安全策略拒绝: {exc}") from exc
    except (SafeET.ParseError, ValueError) as exc:
        raise XMLParseError(f"响应 XML 不合法: {exc}") from exc


def parse_oai_response(content: bytes, *, request_url: str = "") -> OAIEnvelope:
    root = parse_xml(content)
    if root.tag != q(OAI_NAMESPACE, "OAI-PMH"):
        raise XMLParseError(f"根元素不是 OAI-PMH: {root.tag}")
    errors = root.findall(q(OAI_NAMESPACE, "error"))
    if errors:
        first = errors[0]
        raise OAIProtocolError(
            first.get("code", "unknown"),
            collapse_ws("".join(first.itertext())) or "仓储返回未说明的错误",
            request_url=request_url,
        )
    response_node = root.find(q(OAI_NAMESPACE, "responseDate"))
    request_node = root.find(q(OAI_NAMESPACE, "request"))
    if request_node is None:
        raise XMLParseError("OAI-PMH 响应缺少 request 元素")
    return OAIEnvelope(
        response_date=collapse_ws(response_node.text if response_node is not None else ""),
        request_verb=request_node.get("verb", ""),
        request_attributes=dict(request_node.attrib),
        request_url=collapse_ws("".join(request_node.itertext())),
        root=root,
    )


def _extract_dc(metadata_node: object) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    for element in metadata_node.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith(
            f"{{{DC_NAMESPACE}}}"
        ):
            continue
        local_name = element.tag.split("}", 1)[1]
        value = collapse_ws("".join(element.itertext()))
        if value:
            fields.setdefault(local_name, []).append(value)
    return {key: tuple(values) for key, values in fields.items()}


def _record_issue_identifier(record_node: object) -> str | None:
    header = record_node.find(q(OAI_NAMESPACE, "header"))
    if header is None:
        return None
    identifier = header.find(q(OAI_NAMESPACE, "identifier"))
    if identifier is None:
        return None
    return collapse_ws("".join(identifier.itertext())) or None


def parse_record_element(record_node: object) -> ParsedRecord:
    header = record_node.find(q(OAI_NAMESPACE, "header"))
    if header is None:
        raise RecordParseError("record 缺少 header")
    identifier_node = header.find(q(OAI_NAMESPACE, "identifier"))
    datestamp_node = header.find(q(OAI_NAMESPACE, "datestamp"))
    identifier = collapse_ws(
        "".join(identifier_node.itertext()) if identifier_node is not None else ""
    )
    datestamp = collapse_ws(
        "".join(datestamp_node.itertext()) if datestamp_node is not None else ""
    )
    if not identifier:
        raise RecordParseError("header 缺少 identifier")
    if not datestamp:
        raise RecordParseError(f"记录 {identifier} 的 header 缺少 datestamp")
    sets = tuple(
        value
        for value in (
            collapse_ws("".join(node.itertext()))
            for node in header.findall(q(OAI_NAMESPACE, "setSpec"))
        )
        if value
    )
    deleted = (header.get("status") or "").lower() == "deleted"
    metadata_node = record_node.find(q(OAI_NAMESPACE, "metadata"))
    if deleted:
        metadata: dict[str, tuple[str, ...]] = {}
    else:
        if metadata_node is None:
            raise RecordParseError(f"记录 {identifier} 缺少 metadata")
        metadata = _extract_dc(metadata_node)
        if not metadata:
            raise RecordParseError(f"记录 {identifier} 没有 Dublin Core 字段")
    return ParsedRecord(
        identifier=identifier,
        datestamp=datestamp,
        set_specs=sets,
        deleted=deleted,
        metadata=metadata,
    )


def _parse_token(list_node: object) -> ResumptionToken | None:
    token_node = list_node.find(q(OAI_NAMESPACE, "resumptionToken"))
    if token_node is None:
        return None
    value = collapse_ws("".join(token_node.itertext()))
    if not value:
        return None

    def optional_int(attribute: str) -> int | None:
        try:
            return int(token_node.get(attribute, ""))
        except (TypeError, ValueError):
            return None

    return ResumptionToken(
        value=value,
        expiration_date=token_node.get("expirationDate"),
        complete_list_size=optional_int("completeListSize"),
        cursor=optional_int("cursor"),
    )


def parse_record_page(root: object) -> RecordPage:
    list_node = root.find(q(OAI_NAMESPACE, "ListRecords"))
    if list_node is None:
        raise XMLParseError("响应中缺少 ListRecords")
    records: list[ParsedRecord] = []
    issues: list[RecordIssue] = []
    for record_node in list_node.findall(q(OAI_NAMESPACE, "record")):
        try:
            records.append(parse_record_element(record_node))
        except RecordParseError as exc:
            issues.append(
                RecordIssue(
                    identifier=_record_issue_identifier(record_node),
                    code="malformedRecord",
                    message=str(exc),
                )
            )
    return RecordPage(
        records=tuple(records),
        issues=tuple(issues),
        token=_parse_token(list_node),
    )


def parse_identify(content: bytes) -> dict[str, object]:
    envelope = parse_oai_response(content)
    node = envelope.root.find(q(OAI_NAMESPACE, "Identify"))
    if node is None:
        raise XMLParseError("响应中缺少 Identify")
    result: dict[str, object] = {}
    for child in list(node):
        local_name = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
        value = collapse_ws("".join(child.itertext()))
        if local_name in result:
            existing = result[local_name]
            result[local_name] = (
                [existing, value] if not isinstance(existing, list) else existing + [value]
            )
        else:
            result[local_name] = value
    return result


def parse_list_sets(content: bytes) -> tuple[dict[str, str], ...]:
    envelope = parse_oai_response(content)
    list_node = envelope.root.find(q(OAI_NAMESPACE, "ListSets"))
    if list_node is None:
        raise XMLParseError("响应中缺少 ListSets")
    sets: list[dict[str, str]] = []
    for set_node in list_node.findall(q(OAI_NAMESPACE, "set")):
        values = {
            child.tag.split("}", 1)[1]: collapse_ws("".join(child.itertext()))
            for child in set_node
        }
        sets.append(values)
    return tuple(sets)


def parse_metadata_formats(content: bytes) -> tuple[dict[str, str], ...]:
    envelope = parse_oai_response(content)
    parent = envelope.root.find(q(OAI_NAMESPACE, "ListMetadataFormats"))
    if parent is None:
        raise XMLParseError("响应中缺少 ListMetadataFormats")
    formats: list[dict[str, str]] = []
    for format_node in parent.findall(q(OAI_NAMESPACE, "metadataFormat")):
        formats.append(
            {
                child.tag.split("}", 1)[1]: collapse_ws("".join(child.itertext()))
                for child in format_node
            }
        )
    return tuple(formats)


__all__ = [
    "OAI_DC_NAMESPACE",
    "OAIEnvelope",
    "ParsedRecord",
    "RecordIssue",
    "RecordPage",
    "ResumptionToken",
    "parse_identify",
    "parse_list_sets",
    "parse_metadata_formats",
    "parse_oai_response",
    "parse_record_element",
    "parse_record_page",
    "parse_xml",
]
