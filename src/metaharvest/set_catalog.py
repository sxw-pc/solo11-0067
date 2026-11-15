"""Repository-scoped OAI-PMH set catalog and harvest admission.

The catalog is deliberately separate from the harvest ledger. A ``ListSets``
enumeration is only published as a new *active generation* after every page
has been fetched with no network, XML, or protocol failure. Each generation is
scoped to one exact repository identity (the OAI endpoint URL), so the same
``setSpec`` string from a different repository can never satisfy admission.

Within a generation every ``setSpec`` is reduced to one member with an explicit
state:

``available``
    Every occurrence of the spec in this generation agrees on ``setName`` and
    ``setDescription``. New runs may use it.
``conflict``
    The spec was seen on two or more pages with differing name or description.
    Its identity is ambiguous. It may only be used by a new run when the caller
    explicitly pins one description version; that pinned description is copied
    into the run's catalog snapshot so the harvested scope stays explainable.
``invalid``
    The repository returned a ``set`` element without a usable ``setSpec``.
    It can never be selected.

Failures never replace the previous active generation: availability is decided
only at publish time, so a partial or failed enumeration leaves the prior
active directory untouched. ``ListSets`` ``noRecordsMatch`` is the one
successful-but-empty case and does publish an empty generation; that is a
complete directory with zero sets and is distinct from a ``ListRecords``
``noRecordsMatch``, which remains an ordinary empty harvest result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .db import Database
from .http_client import HTTPClient
from .oai_client import OAIClient
from .oai_errors import (
    OAIProtocolError,
    SetAdmissionError,
    SetCatalogError,
)
from .parser import ParsedSet, ResumptionToken, parse_list_sets
from .util import hash_payload, iso_z, sha256_bytes

STATE_AVAILABLE = "available"
STATE_CONFLICT = "conflict"
STATE_INVALID = "invalid"

GENERATION_ACTIVE = "active"
GENERATION_SUPERSEDED = "superseded"
GENERATION_REJECTED = "rejected"

# Admission statuses recorded on harvest_runs.set_catalog_status
ADMISSION_VERIFIED = "verified"
ADMISSION_PINNED_CONFLICT = "pinned_conflict"
ADMISSION_UNVERIFIED = "unverified"
ADMISSION_NOT_APPLICABLE = "not_applicable"


def _description_version(set_name: str, description: str) -> str:
    return hash_payload({"setName": set_name, "setDescription": description})


@dataclass(frozen=True)
class SetOccurrence:
    set_spec: str
    page_number: int
    set_name: str
    description: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "set_spec": self.set_spec,
            "page_number": self.page_number,
            "set_name": self.set_name,
            "description": self.description,
        }


@dataclass(frozen=True)
class CatalogMember:
    set_spec: str
    set_name: str
    description: str
    state: str
    occurrences: int
    first_page: int | None
    last_page: int | None
    versions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "set_spec": self.set_spec,
            "set_name": self.set_name,
            "description": self.description,
            "state": self.state,
            "occurrences": self.occurrences,
            "first_page": self.first_page,
            "last_page": self.last_page,
            "versions": list(self.versions),
        }


@dataclass(frozen=True)
class CatalogGeneration:
    id: int
    repository_url: str
    generation: int
    status: str
    set_count: int
    conflict_count: int
    page_count: int
    fetched_at: str
    activated_at: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_url": self.repository_url,
            "generation": self.generation,
            "status": self.status,
            "set_count": self.set_count,
            "conflict_count": self.conflict_count,
            "page_count": self.page_count,
            "fetched_at": self.fetched_at,
            "activated_at": self.activated_at,
            "error": self.error,
        }


@dataclass(frozen=True)
class AdmissionDecision:
    set_spec: str | None
    repository_url: str
    status: str
    generation_id: int | None = None
    generation: int | None = None
    set_name: str | None = None
    description: str | None = None
    state: str | None = None
    version_id: str | None = None
    reason: str = ""

    @property
    def bypassed(self) -> bool:
        return self.status == ADMISSION_UNVERIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "set_spec": self.set_spec,
            "repository_url": self.repository_url,
            "status": self.status,
            "generation_id": self.generation_id,
            "generation": self.generation,
            "set_name": self.set_name,
            "description": self.description,
            "state": self.state,
            "version_id": self.version_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CatalogPublishResult:
    generation: CatalogGeneration
    admitted: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation.as_dict(),
            "admitted": self.admitted,
            "reason": self.reason,
        }


@dataclass
class _Accumulator:
    """In-memory aggregation of every page in one enumeration."""

    occurrences: list[SetOccurrence] = field(default_factory=list)

    def add_page(self, page_number: int, sets: Iterable[ParsedSet]) -> None:
        for entry in sets:
            self.occurrences.append(
                SetOccurrence(
                    set_spec=entry.spec,
                    page_number=page_number,
                    set_name=entry.name,
                    description=entry.description,
                )
            )


def _aggregate(occurrences: Iterable[SetOccurrence]) -> dict[str, CatalogMember]:
    grouped: dict[str, list[SetOccurrence]] = {}
    for occurrence in occurrences:
        grouped.setdefault(occurrence.set_spec, []).append(occurrence)

    members: dict[str, CatalogMember] = {}
    for spec, entries in grouped.items():
        pages = [entry.page_number for entry in entries]
        if not spec.strip():
            members[spec] = CatalogMember(
                set_spec=spec,
                set_name="",
                description="",
                state=STATE_INVALID,
                occurrences=len(entries),
                first_page=min(pages),
                last_page=max(pages),
            )
            continue
        versions = tuple(
            dict.fromkeys(
                _description_version(entry.set_name, entry.description)
                for entry in entries
            )
        )
        state = STATE_AVAILABLE if len(versions) == 1 else STATE_CONFLICT
        # Keep the first observed description as the representative identity;
        # conflicts retain every version so a caller can pin one explicitly.
        representative = entries[0]
        members[spec] = CatalogMember(
            set_spec=spec,
            set_name=representative.set_name,
            description=representative.description,
            state=state,
            occurrences=len(entries),
            first_page=min(pages),
            last_page=max(pages),
            versions=versions if state == STATE_CONFLICT else (),
        )
    return members


class SetCatalog:
    """Fetch, publish, query, and consume repository set catalogs."""

    def __init__(
        self,
        db: Database,
        client: OAIClient | None = None,
        *,
        conflict_policy: str = "strict",
    ) -> None:
        if conflict_policy not in {"strict", "active"}:
            raise SetCatalogError(f"未知的目录冲突策略: {conflict_policy}")
        self.db = db
        self.client = client
        self.conflict_policy = conflict_policy

    # -- refresh -----------------------------------------------------------

    def refresh(self) -> CatalogPublishResult:
        """Fetch every ``ListSets`` page and publish one generation atomically."""
        if self.client is None:
            raise SetCatalogError("未配置 OAI 客户端，无法抓取集合目录")
        repository_url = self.client.base_url
        accumulator = _Accumulator()
        pages: list[dict[str, Any]] = []
        token: ResumptionToken | None = None
        seen_tokens: set[str] = set()
        page_number = 0
        try:
            while True:
                response = self.client.list_sets(
                    resumption_token=token.value if token else None
                )
                page_number += 1
                try:
                    parsed = parse_list_sets(response.content)
                except OAIProtocolError as exc:
                    if exc.code == "noRecordsMatch" and page_number == 1:
                        # A complete, empty directory. It is safe to publish.
                        pages.append(
                            self._page_evidence(page_number, response, set_count=0)
                        )
                        token = None
                        break
                    raise SetCatalogError(
                        f"ListSets 返回协议错误 {exc.code}: {exc.message}"
                    ) from exc
                accumulator.add_page(page_number, parsed.sets)
                pages.append(
                    self._page_evidence(
                        page_number, response, set_count=len(parsed.sets)
                    )
                )
                token = parsed.token
                if token is None:
                    break
                if token.value in seen_tokens:
                    raise SetCatalogError(
                        "ListSets 分页续传令牌重复，无法确认目录完整性"
                    )
                seen_tokens.add(token.value)
        except SetCatalogError:
            raise
        except Exception as exc:
            # Network, XML, or any unexpected failure: never publish.
            raise SetCatalogError(f"ListSets 分页未完成，目录未发布: {exc}") from exc

        members = _aggregate(accumulator.occurrences)
        return self._publish(
            repository_url, members, accumulator.occurrences, pages
        )

    @staticmethod
    def _page_evidence(
        page_number: int, response: Any, *, set_count: int
    ) -> dict[str, Any]:
        content = response.content or b""
        return {
            "page_number": page_number,
            "request_url": response.request_url,
            "fetched_at": response.fetched_at,
            "http_status": response.status_code,
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
            "set_count": set_count,
        }

    def _publish(
        self,
        repository_url: str,
        members: Mapping[str, CatalogMember],
        occurrences: list[SetOccurrence],
        pages: list[dict[str, Any]],
    ) -> CatalogPublishResult:
        conflict_count = sum(
            1 for member in members.values() if member.state == STATE_CONFLICT
        )
        if self.conflict_policy == "strict" and conflict_count:
            # Route A: keep the previous active generation for queries and
            # admission; record the rejected attempt for evidence.
            generation = self._write_generation(
                repository_url,
                members,
                occurrences,
                pages,
                status=GENERATION_REJECTED,
                error=(
                    f"{conflict_count} 个 set 在分页间身份不一致，严格策略下整代不激活"
                ),
            )
            return CatalogPublishResult(
                generation=generation,
                admitted=False,
                reason=generation.error or "",
            )
        # Route B: activate the generation. Conflicts stay isolated as
        # non-selectable members; every other member is immediately usable.
        generation = self._write_generation(
            repository_url,
            members,
            occurrences,
            pages,
            status=GENERATION_ACTIVE,
            error=None,
        )
        return CatalogPublishResult(generation=generation, admitted=True)

    def _write_generation(
        self,
        repository_url: str,
        members: Mapping[str, CatalogMember],
        occurrences: list[SetOccurrence],
        pages: list[dict[str, Any]],
        *,
        status: str,
        error: str | None,
    ) -> CatalogGeneration:
        conflict_count = sum(
            1 for member in members.values() if member.state == STATE_CONFLICT
        )
        fetched_at = iso_z()
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(generation), 0) AS max_generation
                FROM set_catalog_generations WHERE repository_url = ?
                """,
                (repository_url,),
            ).fetchone()
            generation_number = int(row["max_generation"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO set_catalog_generations (
                    repository_url, generation, status, conflict_policy,
                    set_count, conflict_count, page_count, fetched_at,
                    activated_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_url,
                    generation_number,
                    status,
                    self.conflict_policy,
                    len(members),
                    conflict_count,
                    len(pages),
                    fetched_at,
                    fetched_at if status == GENERATION_ACTIVE else None,
                    error,
                ),
            )
            generation_id = int(cursor.lastrowid)
            for page in pages:
                connection.execute(
                    """
                    INSERT INTO set_catalog_pages (
                        generation_id, page_number, request_url, fetched_at,
                        http_status, sha256, size_bytes, set_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation_id,
                        page["page_number"],
                        page["request_url"],
                        page["fetched_at"],
                        page["http_status"],
                        page["sha256"],
                        page["size_bytes"],
                        page["set_count"],
                    ),
                )
            for member in members.values():
                connection.execute(
                    """
                    INSERT INTO set_catalog_members (
                        generation_id, set_spec, set_name, description, state,
                        occurrences, first_page, last_page
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation_id,
                        member.set_spec,
                        member.set_name,
                        member.description,
                        member.state,
                        member.occurrences,
                        member.first_page,
                        member.last_page,
                    ),
                )
            if status == GENERATION_ACTIVE:
                connection.execute(
                    """
                    UPDATE set_catalog_generations
                    SET status = 'superseded'
                    WHERE repository_url = ? AND status = 'active' AND id != ?
                    """,
                    (repository_url, generation_id),
                )
            # Persist every raw occurrence for conflict evidence.
            connection.executemany(
                """
                INSERT INTO set_catalog_occurrences (
                    generation_id, set_spec, page_number, set_name, description
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        generation_id,
                        occurrence.set_spec,
                        occurrence.page_number,
                        occurrence.set_name,
                        occurrence.description,
                    )
                    for occurrence in occurrences
                ],
            )
            connection.commit()
        return CatalogGeneration(
            id=generation_id,
            repository_url=repository_url,
            generation=generation_number,
            status=status,
            set_count=len(members),
            conflict_count=conflict_count,
            page_count=len(pages),
            fetched_at=fetched_at,
            activated_at=fetched_at if status == GENERATION_ACTIVE else None,
            error=error,
        )

    # -- queries -----------------------------------------------------------

    def active_generation(self, repository_url: str) -> CatalogGeneration | None:
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM set_catalog_generations
                WHERE repository_url = ? AND status = 'active'
                ORDER BY generation DESC LIMIT 1
                """,
                (repository_url,),
            ).fetchone()
        return self._generation_from_row(row)

    def latest_generation(self, repository_url: str) -> CatalogGeneration | None:
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM set_catalog_generations
                WHERE repository_url = ?
                ORDER BY generation DESC LIMIT 1
                """,
                (repository_url,),
            ).fetchone()
        return self._generation_from_row(row)

    def generations(self, repository_url: str, limit: int = 20) -> list[CatalogGeneration]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM set_catalog_generations
                WHERE repository_url = ?
                ORDER BY generation DESC LIMIT ?
                """,
                (repository_url, max(1, int(limit))),
            ).fetchall()
        return [self._generation_from_row(row) for row in rows]

    def members(self, generation_id: int) -> list[CatalogMember]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM set_catalog_members
                WHERE generation_id = ? ORDER BY set_spec
                """,
                (generation_id,),
            ).fetchall()
            version_rows = connection.execute(
                """
                SELECT set_spec, set_name, description
                FROM set_catalog_occurrences WHERE generation_id = ?
                ORDER BY set_spec, page_number, id
                """,
                (generation_id,),
            ).fetchall()
        versions: dict[str, list[str]] = {}
        for row in version_rows:
            versions.setdefault(row["set_spec"], []).append(
                _description_version(row["set_name"], row["description"])
            )
        return [
            CatalogMember(
                set_spec=row["set_spec"],
                set_name=row["set_name"],
                description=row["description"],
                state=row["state"],
                occurrences=int(row["occurrences"]),
                first_page=row["first_page"],
                last_page=row["last_page"],
                versions=tuple(dict.fromkeys(versions.get(row["set_spec"], []))),
            )
            for row in rows
        ]

    def member(self, generation_id: int, set_spec: str) -> CatalogMember | None:
        for entry in self.members(generation_id):
            if entry.set_spec == set_spec:
                return entry
        return None

    def occurrences(self, generation_id: int) -> list[SetOccurrence]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM set_catalog_occurrences
                WHERE generation_id = ? ORDER BY set_spec, page_number, id
                """,
                (generation_id,),
            ).fetchall()
        return [
            SetOccurrence(
                set_spec=row["set_spec"],
                page_number=int(row["page_number"]),
                set_name=row["set_name"],
                description=row["description"],
            )
            for row in rows
        ]

    def pages(self, generation_id: int) -> list[dict[str, Any]]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM set_catalog_pages
                WHERE generation_id = ? ORDER BY page_number
                """,
                (generation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _generation_from_row(row: Any) -> CatalogGeneration | None:
        if row is None:
            return None
        return CatalogGeneration(
            id=int(row["id"]),
            repository_url=row["repository_url"],
            generation=int(row["generation"]),
            status=row["status"],
            set_count=int(row["set_count"]),
            conflict_count=int(row["conflict_count"]),
            page_count=int(row["page_count"]),
            fetched_at=row["fetched_at"],
            activated_at=row["activated_at"],
            error=row["error"],
        )

    # -- admission ---------------------------------------------------------

    def admit(
        self,
        repository_url: str,
        set_spec: str | None,
        *,
        pinned_set_name: str | None = None,
        pinned_description: str | None = None,
        pinned_version_id: str | None = None,
        allow_unverified: bool = False,
    ) -> AdmissionDecision:
        """Decide whether a new run may use ``set_spec`` for this repository."""
        if set_spec is None:
            return AdmissionDecision(
                set_spec=None,
                repository_url=repository_url,
                status=ADMISSION_NOT_APPLICABLE,
                reason="未指定 set，按仓储全量采集",
            )
        generation = self.active_generation(repository_url)
        if generation is None:
            if allow_unverified:
                return AdmissionDecision(
                    set_spec=set_spec,
                    repository_url=repository_url,
                    status=ADMISSION_UNVERIFIED,
                    reason="目录未同步，用户显式绕过准入；过滤条件未经验证",
                )
            raise SetAdmissionError(
                f"仓储 {repository_url} 尚无已发布的完整集合目录；"
                "请先运行 catalog refresh，或用 --allow-unverified-set 显式绕过"
            )
        member = self.member(generation.id, set_spec)
        if member is None:
            if allow_unverified:
                return AdmissionDecision(
                    set_spec=set_spec,
                    repository_url=repository_url,
                    generation_id=generation.id,
                    generation=generation.generation,
                    status=ADMISSION_UNVERIFIED,
                    reason="set 不在 active 目录中，用户显式绕过准入",
                )
            raise SetAdmissionError(
                f"set {set_spec!r} 不属于仓储 {repository_url} 的 active 目录"
                f"（第 {generation.generation} 代）"
            )
        if member.state == STATE_INVALID:
            raise SetAdmissionError(
                f"set {set_spec!r} 在目录中缺少有效 setSpec，不能用于采集"
            )
        if member.state == STATE_CONFLICT:
            return self._admit_conflict(
                repository_url,
                generation,
                member,
                pinned_set_name=pinned_set_name,
                pinned_description=pinned_description,
                pinned_version_id=pinned_version_id,
                allow_unverified=allow_unverified,
            )
        return AdmissionDecision(
            set_spec=set_spec,
            repository_url=repository_url,
            status=ADMISSION_VERIFIED,
            generation_id=generation.id,
            generation=generation.generation,
            set_name=member.set_name,
            description=member.description,
            state=member.state,
            version_id=_description_version(member.set_name, member.description),
            reason="set 属于 active 目录且身份唯一",
        )

    def _admit_conflict(
        self,
        repository_url: str,
        generation: CatalogGeneration,
        member: CatalogMember,
        *,
        pinned_set_name: str | None,
        pinned_description: str | None,
        pinned_version_id: str | None,
        allow_unverified: bool,
    ) -> AdmissionDecision:
        candidates = self._conflict_versions(generation.id, member.set_spec)
        pinned = None
        if pinned_version_id:
            pinned = next(
                (entry for entry in candidates if entry[2] == pinned_version_id), None
            )
        elif pinned_set_name is not None and pinned_description is not None:
            pinned = next(
                (
                    entry
                    for entry in candidates
                    if entry[0] == pinned_set_name
                    and entry[1] == pinned_description
                ),
                None,
            )
        elif pinned_set_name is not None:
            name_matches = [
                entry for entry in candidates if entry[0] == pinned_set_name
            ]
            pinned = name_matches[0] if len(name_matches) == 1 else None
        elif pinned_description is not None:
            description_matches = [
                entry for entry in candidates if entry[1] == pinned_description
            ]
            pinned = description_matches[0] if len(description_matches) == 1 else None
        if pinned is None:
            if allow_unverified:
                return AdmissionDecision(
                    set_spec=member.set_spec,
                    repository_url=repository_url,
                    generation_id=generation.id,
                    generation=generation.generation,
                    status=ADMISSION_UNVERIFIED,
                    state=member.state,
                    reason="冲突 set 未指定说明版本，用户显式绕过准入",
                )
            raise SetAdmissionError(
                f"set {member.set_spec!r} 在目录分页间身份不一致（{len(candidates)} 个版本）；"
                "必须显式指定 --set-name/--set-description 或 --set-version 固定一个说明版本"
            )
        name, description, version_id = pinned
        return AdmissionDecision(
            set_spec=member.set_spec,
            repository_url=repository_url,
            status=ADMISSION_PINNED_CONFLICT,
            generation_id=generation.id,
            generation=generation.generation,
            set_name=name,
            description=description,
            state=member.state,
            version_id=version_id,
            reason="冲突 set 已由用户固定说明版本",
        )

    def _conflict_versions(
        self, generation_id: int, set_spec: str
    ) -> list[tuple[str, str, str]]:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT set_name, description FROM set_catalog_occurrences
                WHERE generation_id = ? AND set_spec = ?
                ORDER BY page_number, id
                """,
                (generation_id, set_spec),
            ).fetchall()
        result: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for row in rows:
            version_id = _description_version(row["set_name"], row["description"])
            if version_id in seen:
                continue
            seen.add(version_id)
            result.append((row["set_name"], row["description"], version_id))
        return result


def resolve_catalog_client(config: Any) -> OAIClient:
    """Build an OAI client for catalog refresh from a harvest config."""
    http = HTTPClient(
        timeout_seconds=config.http.timeout_seconds,
        user_agent=config.http.user_agent,
        audit_log=config.storage.audit_log,
    )
    return OAIClient(config.repository.base_url, http)


def catalog_snapshot_for_run(
    decision: AdmissionDecision,
) -> dict[str, Any]:
    """Flatten an admission decision into harvest_runs catalog columns."""
    return {
        "set_catalog_generation_id": decision.generation_id,
        "set_catalog_generation": decision.generation,
        "set_catalog_status": decision.status,
        "set_catalog_set_name": decision.set_name,
        "set_catalog_set_description": decision.description,
        "set_catalog_set_state": decision.state,
        "set_catalog_version_id": decision.version_id,
    }


__all__ = [
    "ADMISSION_NOT_APPLICABLE",
    "ADMISSION_PINNED_CONFLICT",
    "ADMISSION_UNVERIFIED",
    "ADMISSION_VERIFIED",
    "AdmissionDecision",
    "CatalogGeneration",
    "CatalogMember",
    "CatalogPublishResult",
    "GENERATION_ACTIVE",
    "GENERATION_REJECTED",
    "GENERATION_SUPERSEDED",
    "SetCatalog",
    "SetCatalogError",
    "SetOccurrence",
    "STATE_AVAILABLE",
    "STATE_CONFLICT",
    "STATE_INVALID",
    "catalog_snapshot_for_run",
    "resolve_catalog_client",
]
