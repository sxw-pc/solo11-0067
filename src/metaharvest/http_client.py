"""Local-only HTTP client with pre-request host validation and audit logging."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import requests

from .constants import (
    ALLOWED_SCHEMES,
    LOCAL_HOSTS,
    MAX_RESPONSE_BYTES,
    REDIRECT_LIMIT,
    RETRYABLE_HTTP_STATUSES,
)
from .oai_errors import HttpRequestError, ResponseTooLarge, SecurityViolation
from .util import iso_z


def validate_local_url(url: str) -> str:
    """Reject non-local or malformed targets before any network operation."""
    if not isinstance(url, str) or not url.strip():
        raise SecurityViolation("请求 URL 为空", url=str(url))
    if any(character in url for character in ("\r", "\n", "\t")):
        raise SecurityViolation("请求 URL 含有控制字符", url=url)
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise SecurityViolation(
            f"不支持的协议: {parts.scheme or '(空)'}", url=url, host=parts.hostname
        )
    if not parts.hostname:
        raise SecurityViolation("请求 URL 缺少主机", url=url)
    if parts.username is not None or parts.password is not None:
        raise SecurityViolation("请求 URL 不得包含用户凭据", url=url, host=parts.hostname)
    host = parts.hostname.lower()
    if host not in LOCAL_HOSTS:
        raise SecurityViolation(
            f"仅允许访问本机目标，已拒绝: {host}", url=url, host=host
        )
    try:
        _ = parts.port
    except ValueError as exc:
        raise SecurityViolation(f"请求端口非法: {exc}", url=url, host=host) from exc
    return url


def is_transient_network_error(exc: requests.RequestException) -> bool:
    """Classify connection-level failures that are safe to retry for GET.

    Only timeouts and connection resets/refusals qualify. Anything else (for
    example an invalid URL, a redirect loop or a TLS configuration error) is
    treated as final. ``ConnectionError`` is in the allowlist because its
    cause is by definition a connection-level failure; the request was never
    answered, so replaying the same GET cannot double-process a page.
    """
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    chain = exc.__cause__ or exc.__context__
    return isinstance(
        chain, (TimeoutError, ConnectionError, ConnectionResetError, BrokenPipeError)
    )


class AuditLogger:
    """Append-only JSON Lines audit trail for every request attempt."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        outcome: str,
        url: str,
        *,
        host: str | None = None,
        reason: str = "",
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        entry = {
            "timestamp": iso_z(),
            "outcome": outcome,
            "host": host or urlsplit(url).hostname,
            "url": url,
            "reason": reason,
            "status_code": status_code,
        }
        if retry_after is not None:
            entry["retry_after"] = retry_after
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status_code: int
    content: bytes
    headers: dict[str, str]
    fetched_at: str


class HTTPClient:
    """Minimal GET client that can only reach loopback and follows local redirects."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = "metaharvest/0.1",
        audit_log: Path,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.audit = AuditLogger(audit_log)
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/xml, text/xml;q=0.9, */*;q=0.1",
            }
        )

    def get(self, url: str, params: Mapping[str, Any] | None = None) -> HttpResponse:
        current_url = url
        current_params = dict(params or {})
        redirects = 0
        while True:
            try:
                validate_local_url(current_url)
            except SecurityViolation as exc:
                self.audit.record(
                    "blocked",
                    current_url,
                    host=exc.host,
                    reason=str(exc),
                )
                raise
            parts = urlsplit(current_url)
            self.audit.record("request", current_url, host=parts.hostname)
            try:
                response = self.session.get(
                    current_url,
                    params=current_params,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                self.audit.record(
                    "network_error", current_url, reason=str(exc), host=parts.hostname
                )
                raise HttpRequestError(
                    f"访问本机仓储失败: {exc}",
                    url=current_url,
                    params=current_params,
                    retryable=is_transient_network_error(exc),
                ) from exc

            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise HttpRequestError(
                            "仓储返回重定向但缺少 Location",
                            url=current_url,
                            status_code=response.status_code,
                        )
                    redirects += 1
                    if redirects > REDIRECT_LIMIT:
                        self.audit.record(
                            "blocked_redirect",
                            current_url,
                            host=parts.hostname,
                            reason="重定向次数超过限制",
                        )
                        raise SecurityViolation(
                            "重定向次数超过本机访问限制",
                            url=current_url,
                            host=parts.hostname,
                        )
                    next_url = urljoin(current_url, location)
                    try:
                        validate_local_url(next_url)
                    except SecurityViolation as exc:
                        self.audit.record(
                            "blocked_redirect",
                            next_url,
                            host=exc.host,
                            reason=str(exc),
                        )
                        raise
                    self.audit.record(
                        "redirect",
                        next_url,
                        host=urlsplit(next_url).hostname,
                        status_code=response.status_code,
                    )
                    current_url = next_url
                    current_params = {}
                    continue

                content = self._read_limited(response, current_url)
                headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status_code >= 400:
                    retryable = response.status_code in RETRYABLE_HTTP_STATUSES
                    retry_after = headers.get("retry-after")
                    self.audit.record(
                        "http_error",
                        response.url or current_url,
                        host=parts.hostname,
                        reason=f"HTTP {response.status_code}",
                        status_code=response.status_code,
                        retry_after=retry_after,
                    )
                    raise HttpRequestError(
                        f"HTTP {response.status_code}",
                        url=response.url or current_url,
                        status_code=response.status_code,
                        headers=headers,
                        params=current_params,
                        retryable=retryable,
                    )
                self.audit.record(
                    "success",
                    response.url or current_url,
                    host=parts.hostname,
                    status_code=response.status_code,
                )
                return HttpResponse(
                    url=response.url or current_url,
                    status_code=response.status_code,
                    content=content,
                    headers=headers,
                    fetched_at=iso_z(),
                )
            finally:
                response.close()

    @staticmethod
    def _read_limited(response: requests.Response, url: str) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLarge(
                        f"响应超过 {MAX_RESPONSE_BYTES} 字节上限", url=url
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ResponseTooLarge(
                        f"响应超过 {MAX_RESPONSE_BYTES} 字节上限", url=url
                    )
                chunks.append(chunk)
        except requests.RequestException as exc:
            raise HttpRequestError(f"读取响应失败: {exc}", url=url) from exc
        return b"".join(chunks)
