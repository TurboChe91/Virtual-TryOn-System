"""Opt-in end-to-end test against the REAL paid image API.

Run explicitly (costs ~2 images):
    RUN_REAL_E2E=1 .venv/bin/python -m pytest tests/e2e -m real_api

Requires a filled .env (or environment) with real provider credentials.
Uses an isolated temp data dir — production data is never touched.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from lunelle.config import load_config
from lunelle.server import create_app
from tests.conftest import make_config

pytestmark = pytest.mark.real_api

RUN = os.environ.get("RUN_REAL_E2E") == "1"


@pytest.mark.skipif(not RUN, reason="set RUN_REAL_E2E=1 to run the paid real-API e2e test")
def test_full_real_generation_cycle(tmp_path):
    real = load_config()  # reads .env for credentials
    problems = [p for p in real.validate_for_serve()]
    assert not problems, f"real credentials not configured: {problems}"

    config = make_config(
        tmp_path,
        image_provider="openai-compat",
        image_api_base_url=real.image_api_base_url,
        image_api_key=real.image_api_key,
        image_model=real.image_model,
        grid_size=(2048, 2048),
        wearing_size=(2048, 2048),
        qa_min_side=1024,
        request_timeout_s=300,
    )
    app = create_app(config, start_worker=True)
    with TestClient(app) as client:
        style = client.post("/api/styles", json={
            "name": "E2E Smoke",
            "description": "milky white almond nails with one thin gold line, glossy, medium",
        }).json()["style"]
        plan = client.post(f"/api/styles/{style['style_id']}/generate",
                           json={"output_types": ["grid", "wearing"]}).json()
        assert len(plan["created"]) == 2

        deadline = time.time() + 300
        while time.time() < deadline:
            tasks = client.get("/api/tasks").json()["tasks"]
            if all(t["status"] in ("success", "failed") for t in tasks):
                break
            time.sleep(5)

        tasks = {t["output_type"]: t for t in client.get("/api/tasks").json()["tasks"]}
        assert tasks["grid"]["status"] == "success", tasks["grid"]
        assert tasks["wearing"]["status"] == "success", tasks["wearing"]
        for task in tasks.values():
            image = client.get(f"/api/tasks/{task['task_id']}/image")
            assert image.status_code == 200
            assert len(image.content) > 30 * 1024

    # restart: new app over the same data dir must still see everything
    app2 = create_app(make_config(
        tmp_path,
        image_provider="openai-compat",
        image_api_base_url=real.image_api_base_url,
        image_api_key=real.image_api_key,
        image_model=real.image_model,
    ), start_worker=False)
    with TestClient(app2) as client2:
        tasks = client2.get("/api/tasks").json()["tasks"]
        assert len(tasks) == 2 and all(t["status"] == "success" for t in tasks)
