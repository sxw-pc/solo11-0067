from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from metaharvest.config import (
    HarvestConfig,
    StorageConfig,
    load_harvest_config,
    load_mock_config,
)
from metaharvest.mock_data import load_seed
from metaharvest.mock_repository import create_app
from metaharvest.mock_runtime import RunningMock, serve_mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "configs"
MOCK_CONFIG_PATH = CONFIG_DIR / "mock_repo.yml"
HARVEST_CONFIG_PATH = CONFIG_DIR / "harvest_demo.yml"
SEED_PATH = PROJECT_ROOT / "fixtures" / "oai_seed.yml"


@pytest.fixture(scope="session", autouse=True)
def quiet_werkzeug() -> None:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


@pytest.fixture(scope="session")
def mock_config():
    return load_mock_config(MOCK_CONFIG_PATH)


@pytest.fixture(scope="session")
def seed(mock_config):
    return load_seed(mock_config.seed_file)


@pytest.fixture(scope="session")
def live_mock(mock_config) -> RunningMock:
    instance = serve_mock(mock_config)
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture()
def api_client(mock_config, seed):
    app = create_app(mock_config, seed)
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture()
def harvest_config_factory(
    tmp_path: Path, live_mock: RunningMock
) -> Callable[..., HarvestConfig]:
    template = load_harvest_config(HARVEST_CONFIG_PATH)
    storage = StorageConfig(
        db_path=tmp_path / "state" / "harvest.sqlite3",
        archive_dir=tmp_path / "state" / "archive",
        report_dir=tmp_path / "state" / "reports",
        audit_log=tmp_path / "state" / "audit" / "requests.jsonl",
    )

    def factory(**repository_overrides) -> HarvestConfig:
        values = {"base_url": live_mock.endpoint}
        values.update(repository_overrides)
        repository = replace(template.repository, **values)
        return replace(template, repository=repository, storage=storage, max_pages=None)

    return factory
