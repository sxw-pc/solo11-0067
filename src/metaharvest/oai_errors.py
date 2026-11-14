"""Exception types with enough context for audit records and reports."""

from __future__ import annotations


class MetaHarvestError(Exception):
    """Base class for expected application failures."""


class ConfigError(MetaHarvestError):
    """A configuration file is missing or invalid."""


class SecurityViolation(MetaHarvestError):
    """A request target is outside the local-only allowlist."""

    def __init__(self, message: str, *, url: str, host: str | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.host = host


class HttpRequestError(MetaHarvestError):
    """An HTTP exchange failed before a valid OAI response was obtained."""

    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class ResponseTooLarge(HttpRequestError):
    """The response exceeded the configured local safety boundary."""


class XMLParseError(MetaHarvestError):
    """The response body was empty, unsafe, or not well-formed XML."""


class OAIProtocolError(MetaHarvestError):
    """The repository returned a protocol-level OAI-PMH error."""

    def __init__(self, code: str, message: str, *, request_url: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.request_url = request_url


class RecordParseError(MetaHarvestError):
    """One record inside an otherwise valid OAI-PMH page is malformed."""


class HarvestError(MetaHarvestError):
    """A harvest run could not be started or resumed."""

    def __init__(self, message: str, *, run_id: int | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class ArchiveError(MetaHarvestError):
    """Raw response evidence could not be stored or resolved safely."""


class ReportError(MetaHarvestError):
    """A harvest report could not be built or written."""
