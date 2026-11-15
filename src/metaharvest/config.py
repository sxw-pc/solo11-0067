"""Configuration loading and validation for client and mock repository."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .constants import DEFAULT_USER_AGENT, LOCAL_HOSTS, RETRY_POLICIES
from .oai_errors import ConfigError
from .util import parse_datestamp


@dataclass(frozen=True)
class RepositoryConfig:
    base_url: str
    metadata_prefix: str = "oai_dc"
    set_spec: str | None = None
    from_date: str | None = None
    until_date: str | None = None


@dataclass(frozen=True)
class HTTPConfig:
    timeout_seconds: float = 15.0
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path
    archive_dir: Path
    report_dir: Path
    audit_log: Path


@dataclass(frozen=True)
class RetryConfig:
    """Bounded retry behaviour for idempotent OAI GET requests.

    ``inline`` sleeps while the server's due time fits inside
    ``total_wait_seconds``. ``deferred`` never sleeps longer than
    ``deferred_wait_seconds``; anything longer persists ``retry_not_before``
    and interrupts the run for an operator/scheduler resume.
    """

    policy: str = "deferred"
    max_attempts: int = 3
    default_wait_seconds: float = 5.0
    max_retry_after_seconds: float = 120.0
    total_wait_seconds: float = 300.0
    deferred_wait_seconds: float = 5.0


@dataclass(frozen=True)
class HarvestConfig:
    repository: RepositoryConfig
    http: HTTPConfig
    storage: StorageConfig
    max_pages: int | None = None
    retry: RetryConfig = RetryConfig()


@dataclass(frozen=True)
class MockRepositoryInfo:
    name: str
    base_url: str
    admin_email: str
    earliest_datestamp: str
    description: str


@dataclass(frozen=True)
class MockConfig:
    host: str
    port: int
    seed_file: Path
    repository: MockRepositoryInfo
    page_size: int
    token_ttl_seconds: int
    token_secret: str


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取 YAML 配置 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置根节点必须是映射: {path}")
    return raw


def _mapping(parent: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    value = parent.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置项 {key} 必须是映射")
    return value


def _text(mapping: dict[str, Any], key: str, default: str | None = None) -> str:
    value = mapping.get(key, default)
    if value is None or str(value).strip() == "":
        if default is None:
            raise ConfigError(f"配置项 {key} 不能为空")
        return default
    return str(value).strip()


def _optional_text(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _positive_int(mapping: dict[str, Any], key: str, default: int) -> int:
    try:
        result = int(mapping.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 必须是整数") from exc
    if result <= 0:
        raise ConfigError(f"配置项 {key} 必须大于 0")
    return result


def _non_negative_float(mapping: dict[str, Any], key: str, default: float) -> float:
    try:
        result = float(mapping.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {key} 必须是数字") from exc
    if result < 0:
        raise ConfigError(f"配置项 {key} 不得为负数")
    return result


def _path(base_dir: Path, mapping: dict[str, Any], key: str) -> Path:
    raw = _text(mapping, key)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _validate_base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"仓储 base_url 不是有效 HTTP(S) 地址: {url}")
    if parts.username or parts.password:
        raise ConfigError("仓储 base_url 不得包含用户名或密码")
    return url


def _validate_window(from_date: str | None, until_date: str | None) -> None:
    try:
        start = parse_datestamp(from_date) if from_date else None
        end = parse_datestamp(until_date) if until_date else None
    except ValueError as exc:
        raise ConfigError(f"from_date/until_date 不是合法 OAI 日期: {exc}") from exc
    if start and end and start > end:
        raise ConfigError("from_date 不得晚于 until_date")


def load_harvest_config(path: str | Path) -> HarvestConfig:
    config_path = Path(path).expanduser().resolve()
    raw = _load_yaml(config_path)
    base_dir = config_path.parent
    repository_raw = _mapping(raw, "repository")
    http_raw = _mapping(raw, "http", required=False)
    storage_raw = _mapping(raw, "storage")
    harvest_raw = _mapping(raw, "harvest", required=False)
    retry_raw = _mapping(raw, "retry", required=False)

    from_date = _optional_text(repository_raw, "from_date")
    until_date = _optional_text(repository_raw, "until_date")
    _validate_window(from_date, until_date)
    try:
        timeout = float(http_raw.get("timeout_seconds", 15.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("http.timeout_seconds 必须是数字") from exc
    if timeout <= 0:
        raise ConfigError("http.timeout_seconds 必须大于 0")

    max_pages_raw = harvest_raw.get("max_pages")
    max_pages: int | None = None
    if max_pages_raw is not None:
        max_pages = _positive_int(harvest_raw, "max_pages", 0)

    retry = _retry_config(retry_raw)

    return HarvestConfig(
        repository=RepositoryConfig(
            base_url=_validate_base_url(_text(repository_raw, "base_url")),
            metadata_prefix=_text(repository_raw, "metadata_prefix", "oai_dc"),
            set_spec=_optional_text(repository_raw, "set_spec"),
            from_date=from_date,
            until_date=until_date,
        ),
        http=HTTPConfig(
            timeout_seconds=timeout,
            user_agent=_text(http_raw, "user_agent", DEFAULT_USER_AGENT),
        ),
        storage=StorageConfig(
            db_path=_path(base_dir, storage_raw, "db_path"),
            archive_dir=_path(base_dir, storage_raw, "archive_dir"),
            report_dir=_path(base_dir, storage_raw, "report_dir"),
            audit_log=_path(base_dir, storage_raw, "audit_log"),
        ),
        max_pages=max_pages,
        retry=retry,
    )


def _retry_config(raw: dict[str, Any]) -> RetryConfig:
    policy = _text(raw, "policy", "deferred").lower()
    if policy not in RETRY_POLICIES:
        raise ConfigError(
            f"retry.policy 必须是 {' 或 '.join(RETRY_POLICIES)}，当前为 {policy}"
        )
    max_attempts = _positive_int(raw, "max_attempts", 3)
    default_wait = _non_negative_float(raw, "default_wait_seconds", 5.0)
    max_retry_after = _non_negative_float(raw, "max_retry_after_seconds", 120.0)
    total_wait = _non_negative_float(raw, "total_wait_seconds", 300.0)
    deferred_wait = _non_negative_float(raw, "deferred_wait_seconds", 5.0)
    if deferred_wait > total_wait:
        raise ConfigError("retry.deferred_wait_seconds 不得大于 total_wait_seconds")
    if max_retry_after > total_wait:
        raise ConfigError("retry.max_retry_after_seconds 不得大于 total_wait_seconds")
    if default_wait > max_retry_after:
        raise ConfigError("retry.default_wait_seconds 不得大于 max_retry_after_seconds")
    return RetryConfig(
        policy=policy,
        max_attempts=max_attempts,
        default_wait_seconds=default_wait,
        max_retry_after_seconds=max_retry_after,
        total_wait_seconds=total_wait,
        deferred_wait_seconds=deferred_wait,
    )


def load_mock_config(path: str | Path) -> MockConfig:
    config_path = Path(path).expanduser().resolve()
    raw = _load_yaml(config_path)
    base_dir = config_path.parent
    server = _mapping(raw, "server")
    repository = _mapping(raw, "repository")
    seed = _mapping(raw, "seed")
    pagination = _mapping(raw, "pagination", required=False)

    host = _text(server, "host", "127.0.0.1")
    if host not in LOCAL_HOSTS:
        raise ConfigError("mock 服务只允许绑定 127.0.0.1、localhost 或 ::1")
    try:
        port = int(server.get("port", 8787))
    except (TypeError, ValueError) as exc:
        raise ConfigError("server.port 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("server.port 必须在 1-65535 之间")

    seed_file = _path(base_dir, seed, "path")
    if not seed_file.is_file():
        raise ConfigError(f"seed 文件不存在: {seed_file}")
    earliest = _text(repository, "earliest_datestamp")
    try:
        parse_datestamp(earliest)
    except ValueError as exc:
        raise ConfigError("repository.earliest_datestamp 非法") from exc

    return MockConfig(
        host=host,
        port=port,
        seed_file=seed_file,
        repository=MockRepositoryInfo(
            name=_text(repository, "name"),
            base_url=_validate_base_url(_text(repository, "base_url")),
            admin_email=_text(repository, "admin_email"),
            earliest_datestamp=earliest,
            description=_text(repository, "description", "离线合成 OAI-PMH 仓储"),
        ),
        page_size=_positive_int(pagination, "page_size", 20),
        token_ttl_seconds=_positive_int(pagination, "token_ttl_seconds", 3600),
        token_secret=_text(pagination, "token_secret", "demo-only-local-token-secret"),
    )
