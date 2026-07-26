from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lunelle.config import DEFAULT_PRICING_USD, Config  # noqa: E402
from lunelle.db import Database, migrate  # noqa: E402
from lunelle.tasks import TaskService  # noqa: E402


def make_config(tmp_path: Path, **overrides) -> Config:
    data = tmp_path / "data"
    defaults = dict(
        env="test",
        debug=False,
        log_level="WARNING",
        host="127.0.0.1",
        port=8399,
        admin_token="",
        data_dir=data,
        db_path=data / "lunelle.db",
        output_dir=data / "outputs",
        upload_dir=data / "uploads",
        export_dir=data / "exports",
        log_dir=data / "logs",
        image_api_base_url="https://example.invalid/v1",
        image_api_key="sk-test-not-real",
        image_model="mock-model",
        image_provider="mock",
        grid_size=(512, 512),
        wearing_size=(512, 512),
        reference_mode="auto",
        text_api_base_url="",
        text_api_key="",
        text_model="",
        max_concurrency=2,
        max_retries=2,
        retry_backoff_base_s=1,
        request_timeout_s=30,
        qa_min_side=256,
        max_upload_mb=2,
        pricing_usd=dict(DEFAULT_PRICING_USD),
    )
    defaults.update(overrides)
    config = Config(**defaults)
    config.ensure_dirs()
    return config


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def db(config):
    database = Database(config.db_path)
    migrate(database.conn())
    yield database
    database.close_all()


@pytest.fixture
def service(db, config):
    return TaskService(db, config)
