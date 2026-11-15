"""Flask-based, loopback-only mock OAI-PMH repository."""

from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request

from .config import MockConfig, load_mock_config
from .mock_data import SeedRecord, SeedRepository, load_seed
from .mock_protocol import (
    BadResumptionToken,
    TokenDisplay,
    TokenManager,
    build_error,
    build_get_record,
    build_identify,
    build_metadata_formats,
    build_record_page,
    build_sets,
)
from .util import parse_datestamp

LIST_VERBS = frozenset({"ListRecords", "ListIdentifiers"})
ALLOWED_ARGS = {
    "Identify": set(),
    "ListMetadataFormats": {"identifier"},
    "ListSets": {"resumptionToken"},
    "ListIdentifiers": {"metadataPrefix", "from", "until", "set", "resumptionToken"},
    "ListRecords": {"metadataPrefix", "from", "until", "set", "resumptionToken"},
    "GetRecord": {"identifier", "metadataPrefix"},
}


class ProtocolFault(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MockRepository:
    def __init__(self, config: MockConfig, seed: SeedRepository | None = None) -> None:
        self.config = config
        self.seed = seed or load_seed(config.seed_file)
        self.tokens = TokenManager(config.token_secret, config.token_ttl_seconds)
        self.base_url = config.repository.base_url

    def handle(self, raw_params: Mapping[str, list[str]]) -> bytes:
        duplicates = sorted(key for key, values in raw_params.items() if len(values) > 1)
        params = {key: values[0] for key, values in raw_params.items() if values}
        if duplicates:
            return build_error(
                self.base_url, params, "badArgument", f"参数重复: {', '.join(duplicates)}"
            )
        try:
            return self._dispatch(params)
        except ProtocolFault as exc:
            return build_error(self.base_url, params, exc.code, exc.message)
        except BadResumptionToken as exc:
            return build_error(self.base_url, params, "badResumptionToken", str(exc))

    def _dispatch(self, params: dict[str, str]) -> bytes:
        verb = params.get("verb", "").strip()
        if verb not in ALLOWED_ARGS:
            raise ProtocolFault("badVerb", f"不支持的 verb: {verb or '(空)'}")
        extra = sorted(set(params) - {"verb"} - ALLOWED_ARGS[verb])
        if extra:
            raise ProtocolFault("badArgument", f"不支持的参数: {', '.join(extra)}")

        if verb == "Identify":
            return build_identify(self.config, self.config.repository.earliest_datestamp)
        if verb == "ListSets":
            return self._list_sets(params)
        if verb == "ListMetadataFormats":
            return self._metadata_formats(params)
        if verb == "GetRecord":
            return self._get_record(params)
        if "resumptionToken" in params:
            return self._resume(verb, params)
        return self._first_page(verb, params)

    def _metadata_formats(self, params: dict[str, str]) -> bytes:
        identifier = params.get("identifier")
        if identifier and not self._find(identifier):
            raise ProtocolFault("idDoesNotExist", f"未知 identifier: {identifier}")
        return build_metadata_formats(self.base_url, params)

    def _list_sets(self, params: dict[str, str]) -> bytes:
        sets = self.seed.sets
        if "resumptionToken" in params:
            payload = self.tokens.decode(params["resumptionToken"])
            if payload.get("verb") != "ListSets":
                raise BadResumptionToken("resumptionToken 与 verb 不匹配")
            try:
                offset = int(payload.get("offset", -1))
            except (TypeError, ValueError) as exc:
                raise BadResumptionToken("resumptionToken offset 无效") from exc
            if offset <= 0 or offset >= len(sets):
                raise BadResumptionToken("resumptionToken 游标越界")
            request_params = {
                "verb": "ListSets",
                "resumptionToken": params["resumptionToken"],
            }
        else:
            if not sets:
                raise ProtocolFault("noRecordsMatch", "仓储没有集合")
            offset = 0
            request_params = dict(params)
        page = tuple(sets[offset : offset + self.config.page_size])
        next_offset = offset + len(page)
        token = None
        if next_offset < len(sets):
            token_value = self.tokens.encode(
                {"v": 1, "verb": "ListSets", "offset": next_offset}
            )
            token = TokenDisplay(
                value=token_value,
                expiration_date=None,
                cursor=offset,
                complete_size=len(sets),
            )
        return build_sets(
            base_url=self.base_url,
            params=request_params,
            sets=page,
            token=token,
        )

    def _get_record(self, params: dict[str, str]) -> bytes:
        identifier = params.get("identifier", "").strip()
        if not identifier:
            raise ProtocolFault("badArgument", "GetRecord 缺少 identifier")
        self._require_prefix(params)
        record = self._find(identifier)
        if record is None:
            raise ProtocolFault("idDoesNotExist", f"未知 identifier: {identifier}")
        return build_get_record(base_url=self.base_url, params=params, record=record)

    def _find(self, identifier: str) -> SeedRecord | None:
        return next(
            (record for record in self.seed.records if record.identifier == identifier),
            None,
        )

    @staticmethod
    def _require_prefix(params: dict[str, str]) -> str:
        prefix = params.get("metadataPrefix", "").strip()
        if not prefix:
            raise ProtocolFault("badArgument", "缺少 metadataPrefix")
        if prefix != "oai_dc":
            raise ProtocolFault(
                "cannotDisseminateFormat", f"不支持的 metadataPrefix: {prefix}"
            )
        return prefix

    def _filters(self, params: dict[str, str]) -> dict[str, str | None]:
        prefix = self._require_prefix(params)
        try:
            start = (
                parse_datestamp(params["from"]) if params.get("from") else None
            )
            end = (
                parse_datestamp(params["until"]) if params.get("until") else None
            )
        except ValueError as exc:
            raise ProtocolFault("badArgument", f"日期参数非法: {exc}") from exc
        if start and end and start > end:
            raise ProtocolFault("badArgument", "from 晚于 until")
        return {
            "metadata_prefix": prefix,
            "from_date": params.get("from"),
            "until_date": params.get("until"),
            "set_spec": params.get("set"),
            "_from_dt": start,
            "_until_dt": end,
        }

    def _select(self, filters: Mapping[str, object]) -> list[SeedRecord]:
        start = filters.get("_from_dt")
        end = filters.get("_until_dt")
        set_spec = filters.get("set_spec")
        selected: list[SeedRecord] = []
        for record in self.seed.records:
            observed = parse_datestamp(record.datestamp)
            if isinstance(start, datetime) and observed < start:
                continue
            if isinstance(end, datetime):
                if len(str(filters.get("until_date", ""))) == 10:
                    end = datetime.combine(end.date(), time.max, tzinfo=timezone.utc)
                if observed > end:
                    continue
            if set_spec and set_spec not in record.set_specs:
                continue
            selected.append(record)
        return selected

    def _first_page(self, verb: str, params: dict[str, str]) -> bytes:
        filters = self._filters(params)
        selected = self._select(filters)
        if not selected:
            raise ProtocolFault("noRecordsMatch", "指定条件下没有记录")
        return self._page(
            verb=verb,
            request_params=params,
            filters=filters,
            selected=selected,
            offset=0,
        )

    def _resume(self, verb: str, params: dict[str, str]) -> bytes:
        if verb not in LIST_VERBS:
            raise ProtocolFault("badArgument", "resumptionToken 仅适用于列表 verb")
        extras = sorted(set(params) - {"verb", "resumptionToken"})
        if extras:
            raise ProtocolFault(
                "badArgument", f"使用 resumptionToken 时不得附加参数: {', '.join(extras)}"
            )
        payload = self.tokens.decode(params["resumptionToken"])
        if payload.get("verb") != verb:
            raise BadResumptionToken("resumptionToken 与 verb 不匹配")
        prefix = str(payload.get("metadata_prefix") or "")
        if prefix != "oai_dc":
            raise BadResumptionToken("resumptionToken 的 metadataPrefix 无效")
        filters: dict[str, object] = {
            "metadata_prefix": prefix,
            "from_date": payload.get("from_date"),
            "until_date": payload.get("until_date"),
            "set_spec": payload.get("set_spec"),
            "_from_dt": (
                parse_datestamp(str(payload["from_date"]))
                if payload.get("from_date")
                else None
            ),
            "_until_dt": (
                parse_datestamp(str(payload["until_date"]))
                if payload.get("until_date")
                else None
            ),
        }
        try:
            offset = int(payload.get("offset", -1))
        except (TypeError, ValueError) as exc:
            raise BadResumptionToken("resumptionToken offset 无效") from exc
        selected = self._select(filters)
        if offset <= 0 or offset >= len(selected):
            raise BadResumptionToken("resumptionToken 游标越界")
        return self._page(
            verb=verb,
            request_params={"verb": verb, "resumptionToken": params["resumptionToken"]},
            filters=filters,
            selected=selected,
            offset=offset,
        )

    def _page(
        self,
        *,
        verb: str,
        request_params: dict[str, str],
        filters: Mapping[str, object],
        selected: list[SeedRecord],
        offset: int,
    ) -> bytes:
        page = tuple(selected[offset : offset + self.config.page_size])
        next_offset = offset + len(page)
        token = None
        if next_offset < len(selected):
            token_value = self.tokens.encode(
                {
                    "v": 1,
                    "verb": verb,
                    "metadata_prefix": filters["metadata_prefix"],
                    "from_date": filters["from_date"],
                    "until_date": filters["until_date"],
                    "set_spec": filters["set_spec"],
                    "offset": next_offset,
                }
            )
            token = TokenDisplay(
                value=token_value,
                expiration_date=None,
                cursor=offset,
                complete_size=len(selected),
            )
        return build_record_page(
            base_url=self.base_url,
            params=request_params,
            verb=verb,
            records=page,
            include_metadata=verb == "ListRecords",
            token=token,
        )


def create_app(config: MockConfig, seed: SeedRepository | None = None) -> Flask:
    app = Flask(__name__)
    repository = MockRepository(config, seed)
    endpoint = urlsplit(config.repository.base_url).path or "/oai"

    @app.get("/")
    def index() -> Response:
        return jsonify(
            {
                "name": config.repository.name,
                "oai_endpoint": endpoint,
                "mode": "offline-mock",
                "local_only": True,
            }
        )

    @app.route(endpoint, methods=["GET", "POST"])
    def oai_endpoint() -> Response:
        raw_params = (
            request.form.to_dict(flat=False)
            if request.method == "POST" and request.form
            else request.args.to_dict(flat=False)
        )
        body = repository.handle(raw_params)
        return Response(body, mimetype="text/xml")

    return app


def create_app_from_path(
    config_path: str | Path, seed_path: str | Path | None = None
) -> Flask:
    config = load_mock_config(config_path)
    seed = load_seed(seed_path) if seed_path else load_seed(config.seed_file)
    return create_app(config, seed)
