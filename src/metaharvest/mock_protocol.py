"""OAI-PMH XML builders and tamper-evident resumption tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .config import MockConfig
from .constants import (
    DC_FIELD_ORDER,
    DC_NAMESPACE,
    OAI_DC_NAMESPACE,
    OAI_DC_SCHEMA,
    OAI_NAMESPACE,
    XSI_NAMESPACE,
)
from .mock_data import SeedRecord, SeedSet
from .util import canonical_json, iso_z

ET.register_namespace("", OAI_NAMESPACE)
ET.register_namespace("oai_dc", OAI_DC_NAMESPACE)
ET.register_namespace("dc", DC_NAMESPACE)
ET.register_namespace("xsi", XSI_NAMESPACE)


def q(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


class BadResumptionToken(ValueError):
    pass


class TokenManager:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def encode(self, payload: dict[str, object]) -> str:
        body = dict(payload)
        body["exp"] = int(time.time()) + self.ttl_seconds
        raw = canonical_json(body).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return f"{encoded}.{encoded_signature}"

    def decode(self, token: str) -> dict[str, object]:
        try:
            encoded, encoded_signature = token.split(".", 1)
            raw = base64.urlsafe_b64decode(_pad(encoded))
            signature = base64.urlsafe_b64decode(_pad(encoded_signature))
        except (ValueError, TypeError) as exc:
            raise BadResumptionToken("无法解析 resumptionToken") from exc
        expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise BadResumptionToken("resumptionToken 签名无效")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadResumptionToken("resumptionToken 内容无效") from exc
        if not isinstance(payload, dict):
            raise BadResumptionToken("resumptionToken 内容无效")
        try:
            expires_at = int(payload.get("exp", 0))
        except (TypeError, ValueError) as exc:
            raise BadResumptionToken("resumptionToken 有效期无效") from exc
        if expires_at < int(time.time()):
            raise BadResumptionToken("resumptionToken 已过期")
        return payload


def _pad(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("ascii")


@dataclass(frozen=True)
class TokenDisplay:
    value: str
    expiration_date: str | None
    cursor: int | None
    complete_size: int | None


def _new_response(base_url: str, params: dict[str, str]) -> ET.Element:
    root = ET.Element(q(OAI_NAMESPACE, "OAI-PMH"))
    root.set(
        q(XSI_NAMESPACE, "schemaLocation"),
        f"{OAI_NAMESPACE} http://www.openarchives.org/OAI/2.0/OAI-PMH.xsd",
    )
    ET.SubElement(root, q(OAI_NAMESPACE, "responseDate")).text = iso_z()
    request = ET.SubElement(root, q(OAI_NAMESPACE, "request"))
    for key, value in params.items():
        if value is not None:
            request.set(key, str(value))
    request.text = base_url
    return root


def serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_error(
    base_url: str, params: dict[str, str], code: str, message: str
) -> bytes:
    echo = {key: value for key, value in params.items() if value is not None}
    root = _new_response(base_url, echo or {"verb": params.get("verb", "")})
    error = ET.SubElement(root, q(OAI_NAMESPACE, "error"), {"code": code})
    error.text = message
    return serialize(root)


def build_identify(config: MockConfig, earliest_datestamp: str) -> bytes:
    root = _new_response(config.repository.base_url, {"verb": "Identify"})
    identify = ET.SubElement(root, q(OAI_NAMESPACE, "Identify"))
    values = {
        "repositoryName": config.repository.name,
        "baseURL": config.repository.base_url,
        "protocolVersion": "2.0",
        "adminEmail": config.repository.admin_email,
        "earliestDatestamp": earliest_datestamp,
        "deletedRecord": "persistent",
        "granularity": "YYYY-MM-DDThh:mm:ssZ",
    }
    for key, value in values.items():
        ET.SubElement(identify, q(OAI_NAMESPACE, key)).text = value
    description = ET.SubElement(identify, q(OAI_NAMESPACE, "description"))
    description.text = config.repository.description
    return serialize(root)


def build_metadata_formats(base_url: str, params: dict[str, str]) -> bytes:
    root = _new_response(base_url, params)
    parent = ET.SubElement(root, q(OAI_NAMESPACE, "ListMetadataFormats"))
    format_node = ET.SubElement(parent, q(OAI_NAMESPACE, "metadataFormat"))
    ET.SubElement(format_node, q(OAI_NAMESPACE, "metadataPrefix")).text = "oai_dc"
    ET.SubElement(format_node, q(OAI_NAMESPACE, "schema")).text = OAI_DC_SCHEMA
    ET.SubElement(
        format_node, q(OAI_NAMESPACE, "metadataNamespace")
    ).text = OAI_DC_NAMESPACE
    return serialize(root)


def build_sets(base_url: str, params: dict[str, str], sets: tuple[SeedSet, ...]) -> bytes:
    root = _new_response(base_url, params)
    parent = ET.SubElement(root, q(OAI_NAMESPACE, "ListSets"))
    for entry in sets:
        set_node = ET.SubElement(parent, q(OAI_NAMESPACE, "set"))
        ET.SubElement(set_node, q(OAI_NAMESPACE, "setSpec")).text = entry.spec
        ET.SubElement(set_node, q(OAI_NAMESPACE, "setName")).text = entry.name
    return serialize(root)


def _header(record: SeedRecord) -> ET.Element:
    header = ET.Element(q(OAI_NAMESPACE, "header"))
    if record.deleted:
        header.set("status", "deleted")
    ET.SubElement(header, q(OAI_NAMESPACE, "identifier")).text = record.identifier
    ET.SubElement(header, q(OAI_NAMESPACE, "datestamp")).text = record.datestamp
    for set_spec in record.set_specs:
        ET.SubElement(header, q(OAI_NAMESPACE, "setSpec")).text = set_spec
    return header


def _metadata(record: SeedRecord) -> ET.Element:
    metadata = ET.Element(q(OAI_NAMESPACE, "metadata"))
    dc_node = ET.SubElement(metadata, q(OAI_DC_NAMESPACE, "dc"))
    dc_node.set(
        q(XSI_NAMESPACE, "schemaLocation"),
        f"{OAI_DC_NAMESPACE} {OAI_DC_SCHEMA}",
    )
    for field in DC_FIELD_ORDER:
        for value in record.dc.get(field, ()):
            ET.SubElement(dc_node, q(DC_NAMESPACE, field)).text = value
    return metadata


def _record_node(record: SeedRecord, *, include_metadata: bool) -> ET.Element:
    node = ET.Element(q(OAI_NAMESPACE, "record"))
    node.append(_header(record))
    if include_metadata and not record.deleted:
        node.append(_metadata(record))
    return node


def build_record_page(
    *,
    base_url: str,
    params: dict[str, str],
    verb: str,
    records: tuple[SeedRecord, ...],
    include_metadata: bool,
    token: TokenDisplay | None,
) -> bytes:
    container_name = "ListRecords" if include_metadata else "ListIdentifiers"
    root = _new_response(base_url, params)
    parent = ET.SubElement(root, q(OAI_NAMESPACE, container_name))
    for record in records:
        if include_metadata:
            parent.append(_record_node(record, include_metadata=True))
        else:
            parent.append(_header(record))
    if token is not None:
        token_node = ET.SubElement(parent, q(OAI_NAMESPACE, "resumptionToken"))
        if token.expiration_date:
            token_node.set("expirationDate", token.expiration_date)
        if token.cursor is not None:
            token_node.set("cursor", str(token.cursor))
        if token.complete_size is not None:
            token_node.set("completeListSize", str(token.complete_size))
        token_node.text = token.value
    return serialize(root)


def build_get_record(
    *, base_url: str, params: dict[str, str], record: SeedRecord
) -> bytes:
    root = _new_response(base_url, params)
    parent = ET.SubElement(root, q(OAI_NAMESPACE, "GetRecord"))
    parent.append(_record_node(record, include_metadata=True))
    return serialize(root)
