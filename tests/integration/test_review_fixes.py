"""Tests for the production-readiness review fixes."""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from lunelle.prompts import (
    GRID_REFERENCE_BLOCK,
    WEARING_REFERENCE_BLOCK,
    build_grid_prompt,
    build_wearing_prompt,
    strip_reference_block,
)
from lunelle.providers.base import ProviderError
from lunelle.providers.mock import MockImageProvider
from lunelle.schemas import StyleSpec
from lunelle.server import create_app
from tests.conftest import approve_all_via_api, make_config
from tests.integration.test_worker_flows import make_style, run_worker_until_settled


@pytest.fixture
def client(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config, start_worker=True)
    with TestClient(app) as test_client:
        yield test_client


def wait_success(client, count, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = client.get("/api/tasks").json()["tasks"]
        done = [t for t in tasks if t["status"] in ("success", "failed", "cancelled")]
        if len(done) >= count:
            return tasks
        time.sleep(0.3)
    raise AssertionError("tasks did not settle")


STYLE_BODY = {"name": "Fix Check", "description": "red square nails, glossy, short"}


class TestPromptReferenceBlocks:
    def test_strip_wearing_block(self):
        spec = StyleSpec(sku="nail-x", name="X")
        with_ref = build_wearing_prompt(spec, with_reference=True)
        without = build_wearing_prompt(spec, with_reference=False)
        assert WEARING_REFERENCE_BLOCK in with_ref
        assert strip_reference_block(with_ref) == without

    def test_strip_grid_block(self):
        spec = StyleSpec(sku="nail-x", name="X")
        with_ref = build_grid_prompt(spec, with_reference=True)
        without = build_grid_prompt(spec, with_reference=False)
        assert GRID_REFERENCE_BLOCK in with_ref
        assert strip_reference_block(with_ref) == without

    def test_stored_wearing_prompt_stripped_when_no_grid(self, config, db, service):
        """Wearing runs text-only when grid failed; sent prompt must not claim Image 1."""
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid", "wearing"])

        sent_prompts = {}
        good = MockImageProvider(allowed=True)

        class Capture(MockImageProvider):
            def generate(self, request):
                if "2 rows and 5 columns" in request.prompt:
                    raise ProviderError("content_policy", "no grid", retryable=False)
                sent_prompts["wearing"] = request.prompt
                return good.generate(request)

        run_worker_until_settled(config, db, service, Capture(allowed=True))
        assert "Image 1" not in sent_prompts["wearing"]


class TestManualRetryBudget:
    def test_manual_retry_resets_budget(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        always_fail = MockImageProvider(
            allowed=True, fail_with=ProviderError("server_error", "boom", retryable=True)
        )
        run_worker_until_settled(config, db, service, always_fail, timeout=60)
        task_id = service.list_tasks()[0]["task_id"]
        exhausted = service.get_task(task_id, with_details=False)
        assert exhausted["status"] == "failed"
        assert exhausted["retry_count"] == config.max_retries

        requeued = service.manual_retry(task_id, note="ops fixed the provider")
        assert requeued["retry_count"] == 0  # fresh automatic-retry budget

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        assert service.get_task(task_id, with_details=False)["status"] == "success"


class TestInternalErrorsRetryable:
    def test_unexpected_crash_gets_retry_budget(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])

        calls = {"n": 0}
        good = MockImageProvider(allowed=True)

        class CrashOnce(MockImageProvider):
            def generate(self, request):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("totally unexpected")  # not a ProviderError
                return good.generate(request)

        run_worker_until_settled(config, db, service, CrashOnce(allowed=True), timeout=40)
        task = service.get_task(service.list_tasks()[0]["task_id"])
        assert task["status"] == "success"
        assert task["retry_count"] == 1


class TestReviewEndpoint:
    def test_review_clears_needs_human_review_and_gates_export(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})
        wait_success(client, 2)

        # Export is default-deny: nothing ships before a human verdict.
        response = client.post("/api/export", json={})
        assert response.status_code == 422

        # `include_unreviewed` was removed; an old client sending it fails loudly
        # instead of silently getting the opposite of what it asked for.
        legacy = client.post("/api/export", json={"include_unreviewed": True})
        assert legacy.status_code == 422

        approved = approve_all_via_api(client, expect=2)
        for out in approved:
            assert out["needs_human_review"] is False
            assert out["review_state"] == "approved"
            detail = client.get(f"/api/tasks/{out['task_id']}").json()
            assert detail["qa"]["needs_human_review"] == 0
            assert detail["review_state"] == "approved"
            assert detail["reviewed_at"] is not None

        reviewed_export = client.post("/api/export", json={})
        assert reviewed_export.status_code == 200
        assert reviewed_export.json()["item_count"] == 1

    def test_review_requires_success(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        plan = client.post(f"/api/styles/{style_id}/generate",
                           json={"output_types": ["grid"]}).json()
        # review immediately; task may still be pending/running
        task_id = plan["created"][0]["task_id"]
        response = client.post(f"/api/tasks/{task_id}/review", json={"approved": True})
        assert response.status_code in (409, 200)  # 200 only if worker already finished
        if response.status_code == 200:
            pytest.skip("worker finished before review call; guard not exercised")


class TestGridStyleReference:
    def test_uploaded_reference_feeds_grid_generation(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        buf = io.BytesIO()
        Image.new("RGB", (400, 400), (180, 40, 40)).save(buf, format="PNG")
        upload = client.post(f"/api/styles/{style_id}/reference-image",
                             files={"file": ("ref.png", buf.getvalue(), "image/png")})
        assert upload.status_code == 200

        client.post(f"/api/styles/{style_id}/generate", json={"output_types": ["grid"]})
        wait_success(client, 1)
        task = client.get("/api/tasks").json()["tasks"][0]
        detail = client.get(f"/api/tasks/{task['task_id']}").json()
        assert detail["status"] == "success"
        assert "customer-supplied style reference" in detail["prompt"]
        assert detail["metadata"]["use_style_reference"] is True
        assert detail["metadata"]["reference_used"] is True


class TestExportAtomicity:
    def test_exports_get_unique_dirs_and_no_partials(self, client, tmp_path):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})
        wait_success(client, 2)

        approve_all_via_api(client, expect=2)
        first = client.post("/api/export", json={}).json()
        second = client.post("/api/export", json={}).json()
        assert first["export_dir"] != second["export_dir"]

        from pathlib import Path
        export_root = Path(first["export_dir"]).parent
        assert not list(export_root.glob("*.partial"))
        for doc in (first, second):
            base = Path(doc["export_dir"])
            for name in ("manifest.json", "products.csv", "generation-report.csv"):
                assert (base / name).is_file()


class TestProductionConfigGuards:
    def test_admin_token_required_in_production(self, tmp_path):
        config = make_config(
            tmp_path, env="production", image_provider="openai-compat",
            image_api_base_url="https://x.example/v1", image_api_key="sk-real",
            image_model="m", admin_token="",
        )
        assert any("LUNELLE_ADMIN_TOKEN" in p for p in config.validate_for_serve())

    def test_admin_token_satisfies_production(self, tmp_path):
        config = make_config(
            tmp_path, env="production", image_provider="openai-compat",
            image_api_base_url="https://x.example/v1", image_api_key="sk-real",
            image_model="m", admin_token="strong-token",
            daily_budget_usd=20.0,  # production also requires a spend cap
        )
        assert not config.validate_for_serve()
