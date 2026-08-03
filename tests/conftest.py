from __future__ import annotations

import sys
import time
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
        hero_size=(768, 512),
        reference_mode="auto",
        text_api_base_url="",
        text_api_key="",
        text_model="",
        max_concurrency=2,
        max_retries=2,
        retry_backoff_base_s=1,
        request_timeout_s=30,
        qa_min_side=256,
        auto_regen_max=0,  # deterministic tests; production defaults to 1
        max_upload_mb=2,
        disable_provider_watermark=True,
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


def approve_all_via_api(client, *, expect: int | None = None) -> list[dict]:
    """Approve every successful task through the API, waiting for QA to land.

    Export and publish are default-deny, so any test that wants an asset to ship
    has to approve it the way an operator would. Waiting on qa_state (rather than
    just status) is the whole point of the state column: `success` no longer
    implies a QA verdict exists.
    """
    deadline = time.time() + 30.0
    while time.time() < deadline:
        tasks = [t for t in client.get("/api/tasks").json()["tasks"]
                 if t["status"] == "success"]
        if tasks and all(t["qa_state"] not in ("pending", "running") for t in tasks):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("QA did not settle for successful tasks")

    approved = []
    for task in tasks:
        response = client.post(f"/api/tasks/{task['task_id']}/review",
                               json={"approved": True, "note": "test approval"})
        assert response.status_code == 200, response.text
        approved.append(response.json())
    if expect is not None:
        assert len(approved) == expect, f"approved {len(approved)}, expected {expect}"
    return approved


def write_test_image(path: Path, size=(64, 64), color=(200, 170, 150)) -> Path:
    """A real decodable image on disk, for dependency-satisfaction fixtures."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def satisfy_matrix_dependencies(db, config, service, style_id: str,
                                tones=("light", "medium", "tan", "deep"),
                                views=("p2_open_hands", "p3_right_hand",
                                       "p4_thumb_visible", "p5_left_hand")) -> None:
    """Give a style the inputs a matrix cell hard-requires: a design authority
    (plan image) and a hand model per tone+view. Matrix cells now block instead
    of degrading to a text-only render, so tests of the success path must supply
    these explicitly.
    """
    from lunelle.db import transaction, utcnow

    plan = write_test_image(config.upload_dir / f"plan-{style_id}.png", (256, 256))
    service.set_plan_image(style_id, plan)
    conn = db.conn()
    with transaction(conn):
        for tone in tones:
            for view in views:
                hand = write_test_image(
                    config.upload_dir / f"hand-{tone}-{view}.png", (256, 256))
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (f"hand_model_{tone}_{view}", str(hand), utcnow()),
                )
