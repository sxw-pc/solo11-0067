"""Synthetic seed records for the embedded mock repository."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .constants import DC_FIELD_ORDER
from .oai_errors import ConfigError
from .util import as_string_list, normalize_datestamp


@dataclass(frozen=True)
class SeedSet:
    spec: str
    name: str


@dataclass(frozen=True)
class SeedRecord:
    identifier: str
    datestamp: str
    set_specs: tuple[str, ...]
    deleted: bool
    dc: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class SeedRepository:
    sets: tuple[SeedSet, ...]
    records: tuple[SeedRecord, ...]

    @property
    def earliest_datestamp(self) -> str:
        return min((record.datestamp for record in self.records), default="2024-01-01T00:00:00Z")


def _clean_list(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(as_string_list(value)))


def _load_dc(raw: Any, identifier: str) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"记录 {identifier} 的 dc 必须是映射")
    result: dict[str, tuple[str, ...]] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key).strip().lower()
        if key not in DC_FIELD_ORDER:
            raise ConfigError(f"记录 {identifier} 使用了不支持的 DC 字段: {key}")
        values = _clean_list(raw_value)
        if values:
            result[key] = values
    return result


def load_seed(path: str | Path) -> SeedRepository:
    seed_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"seed 文件不存在: {seed_path}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取 seed YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("seed 根节点必须是映射")

    sets: list[SeedSet] = []
    raw_sets = raw.get("sets") or []
    if not isinstance(raw_sets, list):
        raise ConfigError("seed.sets 必须是列表")
    known_specs: set[str] = set()
    for entry in raw_sets:
        if not isinstance(entry, dict):
            raise ConfigError("seed.sets 中的条目必须是映射")
        spec = str(entry.get("spec", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not spec or not name:
            raise ConfigError("每个 set 必须有 spec 和 name")
        if spec in known_specs:
            raise ConfigError(f"重复的 set spec: {spec}")
        known_specs.add(spec)
        sets.append(SeedSet(spec=spec, name=name))

    records: list[SeedRecord] = []
    raw_records = raw.get("records") or []
    if not isinstance(raw_records, list):
        raise ConfigError("seed.records 必须是列表")
    identifiers: set[str] = set()
    for entry in raw_records:
        if not isinstance(entry, dict):
            raise ConfigError("seed.records 中的条目必须是映射")
        identifier = str(entry.get("identifier", "")).strip()
        if not identifier:
            raise ConfigError("seed record 缺少 identifier")
        if identifier in identifiers:
            raise ConfigError(f"重复的 record identifier: {identifier}")
        identifiers.add(identifier)
        try:
            datestamp = normalize_datestamp(str(entry.get("datestamp", "")))
        except ValueError as exc:
            raise ConfigError(f"记录 {identifier} 的 datestamp 非法") from exc
        set_specs = _clean_list(entry.get("sets"))
        unknown_sets = set(set_specs) - known_specs
        if unknown_sets:
            raise ConfigError(f"记录 {identifier} 引用了未知 set: {sorted(unknown_sets)}")
        deleted = bool(entry.get("deleted", False))
        dc = _load_dc(entry.get("dc"), identifier)
        if not deleted and not dc:
            raise ConfigError(f"记录 {identifier} 未删除但缺少 dc 字段")
        records.append(
            SeedRecord(
                identifier=identifier,
                datestamp=datestamp,
                set_specs=set_specs,
                deleted=deleted,
                dc=dc,
            )
        )

    records.sort(key=lambda record: (record.datestamp, record.identifier))
    return SeedRepository(sets=tuple(sets), records=tuple(records))
