"""Dublin Core field normalization and stable content hashing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .language import LocalizedValue, localized_to_json_values
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
    # Ordered language-qualified values per Dublin Core field name.  This is
    # the source of truth for display-language selection; the scalar/list
    # fields above are compatibility views derived from it.
    localized: Mapping[str, tuple[LocalizedValue, ...]]

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
            # Ordered (field, text, language) identity: a language-tag-only
            # source change must be hashed as a content update.
            "localized": localized_to_json_values(dict(self.localized)),
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

# Dublin Core field name -> NormalizedRecord attribute, for every field the
# pipeline surfaces in reports.  Used to derive compatibility display values
# from localized entries.
DC_FIELD_ATTRIBUTES: dict[str, str] = {**_SCALARS, **_MULTIS}

# Dublin Core fields displayed as a single scalar value (the rest are lists).
DC_SCALAR_FIELDS: frozenset[str] = frozenset(_SCALARS)


def _dedup_localized(
    entries: tuple[LocalizedValue, ...],
) -> tuple[LocalizedValue, ...]:
    """De-duplicate on (text, language) compared case-insensitively.

    Two values with identical text but different languages are distinct and
    both survive; only true repeats (same text, same language) collapse.
    The first occurrence's original tag spelling is kept for evidence.
    """
    seen: set[tuple[str, str | None]] = set()
    result: list[LocalizedValue] = []
    for entry in entries:
        text = collapse_ws(entry.text)
        if not text:
            continue
        language = collapse_ws(entry.language) or None
        key = (text.casefold(), language.casefold() if language else None)
        if key in seen:
            continue
        seen.add(key)
        result.append(LocalizedValue(text, language))
    return tuple(result)


def _first_text(entries: tuple[LocalizedValue, ...]) -> str:
    return entries[0].text if entries else ""


def legacy_localized(
    record: Mapping[str, Any],
) -> dict[str, tuple[LocalizedValue, ...]]:
    """Rebuild localized entries from legacy scalar/list values as untagged.

    Used by the schema migration for databases written before language tags
    were preserved: existing values are kept verbatim and marked untagged;
    no language label is invented and discarded tags are not reconstructed.
    """
    mapping: dict[str, tuple[LocalizedValue, ...]] = {}
    for dc_field, attribute in _SCALARS.items():
        text = collapse_ws(record.get(attribute))
        if text:
            mapping[dc_field] = (LocalizedValue(text, None),)
    for dc_field, attribute in _MULTIS.items():
        entries = tuple(
            LocalizedValue(collapse_ws(value), None)
            for value in record.get(attribute) or ()
            if collapse_ws(value)
        )
        if entries:
            mapping[dc_field] = entries
    return mapping


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
            localized={},
        )

    localized: dict[str, tuple[LocalizedValue, ...]] = {}
    for dc_field, entries in parsed.metadata.items():
        deduped = _dedup_localized(tuple(entries))
        if deduped:
            localized[dc_field] = deduped

    scalar_values = {
        attribute: _first_text(localized.get(dc_field, ()))
        for dc_field, attribute in _SCALARS.items()
    }
    multi_values = {
        attribute: tuple(value.text for value in localized.get(dc_field, ()))
        for dc_field, attribute in _MULTIS.items()
    }
    return NormalizedRecord(
        identifier=identifier,
        datestamp=datestamp,
        set_specs=tuple(sorted(unique_clean(parsed.set_specs))),
        deleted=False,
        localized=localized,
        **scalar_values,
        **multi_values,
    )
