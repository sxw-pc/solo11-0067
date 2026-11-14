"""Dublin Core field normalization and stable content hashing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .oai_errors import RecordParseError
from .parser import ParsedRecord
from .util import collapse_ws, hash_payload, normalize_datestamp, unique_clean


@dataclass(frozen=True)
class NormalizedRecord:
    identifier: str
    datestamp: str
    set_specs: tuple[str, ...]
    deleted: bool
    title: str
    creators: tuple[str, ...]
    subjects: tuple[str, ...]
    description: str
    publisher: str
    contributors: tuple[str, ...]
    date: str
    resource_type: str
    format: str
    identifiers: tuple[str, ...]
    sources: tuple[str, ...]
    language: str
    relations: tuple[str, ...]
    coverage: tuple[str, ...]
    rights: str

    def content_dict(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "set_specs": list(self.set_specs),
            "deleted": self.deleted,
            "title": self.title,
            "creators": list(self.creators),
            "subjects": list(self.subjects),
            "description": self.description,
            "publisher": self.publisher,
            "contributors": list(self.contributors),
            "date": self.date,
            "resource_type": self.resource_type,
            "format": self.format,
            "identifiers": list(self.identifiers),
            "sources": list(self.sources),
            "language": self.language,
            "relations": list(self.relations),
            "coverage": list(self.coverage),
            "rights": self.rights,
        }

    @property
    def record_hash(self) -> str:
        return hash_payload(self.content_dict())

    def report_dict(self) -> dict[str, object]:
        value = self.content_dict()
        value["datestamp"] = self.datestamp
        value["record_hash"] = self.record_hash
        return value


_SCALARS = {
    "title": "title",
    "description": "description",
    "publisher": "publisher",
    "date": "date",
    "type": "resource_type",
    "format": "format",
    "language": "language",
    "rights": "rights",
}

_MULTIS = {
    "creator": "creators",
    "subject": "subjects",
    "contributor": "contributors",
    "identifier": "identifiers",
    "source": "sources",
    "relation": "relations",
    "coverage": "coverage",
}


def _first(metadata: Mapping[str, tuple[str, ...]], key: str) -> str:
    for value in metadata.get(key, ()):
        cleaned = collapse_ws(value)
        if cleaned:
            return cleaned
    return ""


def _many(metadata: Mapping[str, tuple[str, ...]], key: str) -> tuple[str, ...]:
    return unique_clean(metadata.get(key, ()))


def normalize_record(parsed: ParsedRecord) -> NormalizedRecord:
    identifier = collapse_ws(parsed.identifier)
    if not identifier:
        raise RecordParseError("记录缺少 OAI identifier")
    try:
        datestamp = normalize_datestamp(parsed.datestamp)
    except ValueError as exc:
        raise RecordParseError(f"记录 {identifier} 的 datestamp 非法") from exc

    if parsed.deleted:
        return NormalizedRecord(
            identifier=identifier,
            datestamp=datestamp,
            set_specs=tuple(sorted(unique_clean(parsed.set_specs))),
            deleted=True,
            title="",
            creators=(),
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

    scalar_values = {
        attribute: _first(parsed.metadata, dc_field)
        for dc_field, attribute in _SCALARS.items()
    }
    multi_values = {
        attribute: _many(parsed.metadata, dc_field)
        for dc_field, attribute in _MULTIS.items()
    }
    return NormalizedRecord(
        identifier=identifier,
        datestamp=datestamp,
        set_specs=tuple(sorted(unique_clean(parsed.set_specs))),
        deleted=False,
        **scalar_values,
        **multi_values,
    )
