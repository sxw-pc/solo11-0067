"""Thin OAI-PMH request builder; XML interpretation lives in parser.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .http_client import HTTPClient
from .oai_errors import ConfigError, HttpRequestError


@dataclass(frozen=True)
class OAIResponse:
    verb: str
    request_url: str
    status_code: int
    content: bytes
    headers: dict[str, str]
    fetched_at: str
    params: dict[str, str]


def build_params(verb: str, parameters: dict[str, Any]) -> dict[str, str]:
    """Normalize verb + keyword arguments into the exact query parameters.

    Shared by the client and the harvester's attempt evidence so both agree on
    the wire shape (notably: a continuation request carries only ``verb`` and
    ``resumptionToken``).
    """
    params = {"verb": verb}
    for key, value in parameters.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            params[key] = text
    return params


class OAIClient:
    def __init__(self, base_url: str, http_client: HTTPClient) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigError(f"无效的 OAI-PMH base_url: {base_url}")
        if parts.query or parts.fragment:
            raise ConfigError("OAI-PMH base_url 不得包含查询串或片段")
        self.base_url = base_url
        self.http = http_client

    def _fetch(self, verb: str, **parameters: Any) -> OAIResponse:
        params = build_params(verb, parameters)
        try:
            response = self.http.get(self.base_url, params=params)
        except HttpRequestError as exc:
            # Preserve the exact request shape (notably resumptionToken) so the
            # retry layer can replay this request verbatim instead of
            # restarting from the first page.
            if not exc.params:
                exc.params = dict(params)
            raise
        return OAIResponse(
            verb=verb,
            request_url=response.url,
            status_code=response.status_code,
            content=response.content,
            headers=response.headers,
            fetched_at=response.fetched_at,
            params=params,
        )

    def identify(self) -> OAIResponse:
        return self._fetch("Identify")

    def list_metadata_formats(self, identifier: str | None = None) -> OAIResponse:
        return self._fetch("ListMetadataFormats", identifier=identifier)

    def list_sets(self) -> OAIResponse:
        return self._fetch("ListSets")

    def list_identifiers(
        self,
        *,
        metadata_prefix: str,
        from_date: str | None = None,
        until_date: str | None = None,
        set_spec: str | None = None,
        resumption_token: str | None = None,
    ) -> OAIResponse:
        if resumption_token:
            return self._fetch("ListIdentifiers", resumptionToken=resumption_token)
        return self._fetch(
            "ListIdentifiers",
            metadataPrefix=metadata_prefix,
            **{"from": from_date, "until": until_date, "set": set_spec},
        )

    def list_records(
        self,
        *,
        metadata_prefix: str,
        from_date: str | None = None,
        until_date: str | None = None,
        set_spec: str | None = None,
        resumption_token: str | None = None,
    ) -> OAIResponse:
        if resumption_token:
            return self._fetch("ListRecords", resumptionToken=resumption_token)
        return self._fetch(
            "ListRecords",
            metadataPrefix=metadata_prefix,
            **{"from": from_date, "until": until_date, "set": set_spec},
        )

    def get_record(self, *, identifier: str, metadata_prefix: str = "oai_dc") -> OAIResponse:
        return self._fetch(
            "GetRecord", identifier=identifier, metadataPrefix=metadata_prefix
        )
