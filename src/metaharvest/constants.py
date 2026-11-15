"""Shared constants for the harvester and mock repository."""

from __future__ import annotations

APP_NAME = "metaharvest"
APP_TITLE = "元仓收割台"
DEFAULT_USER_AGENT = "metaharvest/0.1 (offline OAI-PMH workbench)"

OAI_NAMESPACE = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NAMESPACE = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
OAI_DC_SCHEMA = "http://www.openarchives.org/OAI/2.0/oai_dc.xsd"

OAI_VERBS = frozenset(
    {
        "Identify",
        "ListMetadataFormats",
        "ListSets",
        "ListIdentifiers",
        "ListRecords",
        "GetRecord",
    }
)
SUPPORTED_METADATA_PREFIXES = ("oai_dc",)

ALLOWED_SCHEMES = frozenset({"http", "https"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REDIRECT_LIMIT = 3
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# Explicit allowlist of HTTP statuses that may be retried. Everything else
# (including 4xx client errors and HTTP 200 OAI-PMH protocol errors) is final.
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 502, 503, 504})
RETRY_POLICIES = ("inline", "deferred")

RUN_STATUSES = ("running", "completed", "interrupted", "failed")
TERMINAL_RUN_STATUSES = frozenset({"completed", "interrupted", "failed"})

DC_FIELD_ORDER = (
    "title",
    "creator",
    "subject",
    "description",
    "publisher",
    "date",
    "type",
    "format",
    "identifier",
    "source",
    "language",
    "relation",
    "coverage",
    "rights",
)
