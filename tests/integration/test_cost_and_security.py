"""Phase 2 integration tests: cost preview/confirm/breaker, SSRF, settings auth."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lunelle.providers.mock import MockImageProvider
from lunelle.server import create_app
from lunelle.worker import Worker
from tests.conftest import make_config, satisfy_matrix_dependencies

STYLE_BODY = {"name": "Cost Check", "description": "amber almond glossy nails"}


def build(tmp_path, **overrides):
    config = make_config(tmp_path, **overrides)
    app = create_app(config, start_worker=False)
    return config, app


def new_style(client, app, config, body=None):
    style = client.post("/api/styles", json=body or STYLE_BODY).json()["style"]
    satisfy_matrix_dependencies(app.state.db, config, app.state.service,
                               style["style_id"])
    return style["style_id"]


class TestCostPreview:
    def test_estimate_queues_nothing_and_prices_the_batch(self, tmp_path):
        config, app = build(tmp_path, confirm_cost_usd=0.10)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            out = client.post(f"/api/styles/{style_id}/matrix/estimate", json={}).json()
            assert out["cells_requested"] == 16
            assert out["image_count"] == 16
            assert out["estimated_usd"] > 0
            # Worst case must exceed the estimate whenever retries are possible.
            assert out["worst_case_usd"] >= out["estimated_usd"]
            assert out["requires_confirmation"] is True
            assert "budget" in out
            # Nothing was queued by pricing it.
            assert client.get("/api/tasks").json()["tasks"] == []

    def test_estimate_prices_only_the_incremental_cells(self, tmp_path):
        """Re-running a mostly-complete matrix must quote the increment, or the
        operator learns to ignore the figure."""
        config, app = build(tmp_path, confirm_cost_usd=0.0)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            first = client.post(f"/api/styles/{style_id}/matrix",
                                json={"tones": ["light"]})
            assert first.status_code == 202
            queued = len(first.json()["created"])
            assert queued == 4  # 1 tone x 4 views

            again = client.post(f"/api/styles/{style_id}/matrix/estimate",
                                json={"tones": ["light"]}).json()
            assert again["cells_in_progress"] == 4
            assert again["image_count"] == 0
            assert again["estimated_usd"] == 0

    def test_estimate_requires_admin(self, tmp_path):
        config, app = build(tmp_path, admin_token="secret-token")
        with TestClient(app) as client:
            style = client.post("/api/styles", json=STYLE_BODY,
                                headers={"X-Admin-Token": "secret-token"}).json()
            style_id = style["style"]["style_id"]
            assert client.post(f"/api/styles/{style_id}/matrix/estimate",
                               json={}).status_code == 401


class TestCostConfirmation:
    def test_expensive_batch_needs_confirmation(self, tmp_path):
        config, app = build(tmp_path, confirm_cost_usd=0.10)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            response = client.post(f"/api/styles/{style_id}/matrix", json={})
            assert response.status_code == 409
            body = response.json()
            assert body["code"] == "cost_confirmation_required"
            # The estimate rides along so the UI can show what to confirm.
            assert body["estimate"]["image_count"] == 16
            assert client.get("/api/tasks").json()["tasks"] == []

    def test_stale_confirmation_is_refused(self, tmp_path):
        """Guards against the price changing between preview and submit."""
        config, app = build(tmp_path, confirm_cost_usd=0.10)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            response = client.post(f"/api/styles/{style_id}/matrix",
                                   json={"confirm_max_usd": 0.01})
            assert response.status_code == 409
            assert "mismatch" in response.json()["error"]
            assert client.get("/api/tasks").json()["tasks"] == []

    def test_confirming_the_estimate_instead_of_the_ceiling_is_refused(self, tmp_path):
        """The authorized figure is the worst case. Echoing the expected cost —
        what the old confirm_estimated_usd field asked for — must not authorize."""
        config, app = build(tmp_path, confirm_cost_usd=0.10)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            estimate = client.post(f"/api/styles/{style_id}/matrix/estimate",
                                   json={}).json()
            assert estimate["confirm_max_usd"] > estimate["estimated_usd"]
            response = client.post(
                f"/api/styles/{style_id}/matrix",
                json={"confirm_max_usd": estimate["estimated_usd"]})
            assert response.status_code == 409
            assert "ceiling" in response.json()["error"]
            assert client.get("/api/tasks").json()["tasks"] == []

    def test_old_field_name_is_rejected(self, tmp_path):
        """extra="forbid" makes the rename loud rather than silently ignored."""
        config, app = build(tmp_path, confirm_cost_usd=0.10)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            response = client.post(f"/api/styles/{style_id}/matrix",
                                   json={"confirm_estimated_usd": 0.8})
            assert response.status_code == 422

    def test_correct_confirmation_queues_the_batch(self, tmp_path):
        config, app = build(tmp_path, confirm_cost_usd=0.10)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            estimate = client.post(f"/api/styles/{style_id}/matrix/estimate",
                                   json={}).json()
            response = client.post(
                f"/api/styles/{style_id}/matrix",
                json={"confirm_max_usd": estimate["confirm_max_usd"]})
            assert response.status_code == 202
            assert len(response.json()["created"]) == 16

    def test_cheap_batch_needs_no_confirmation(self, tmp_path):
        config, app = build(tmp_path, confirm_cost_usd=10.0)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            response = client.post(f"/api/styles/{style_id}/matrix", json={})
            assert response.status_code == 202


class TestBudgetBreaker:
    def test_queue_time_breaker_refuses_and_queues_nothing(self, tmp_path):
        config, app = build(tmp_path, daily_budget_usd=0.10, confirm_cost_usd=0.0)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            response = client.post(f"/api/styles/{style_id}/matrix", json={})
            assert response.status_code == 429
            body = response.json()
            assert body["code"] == "budget_exceeded"
            assert body["budget"]["limit_usd"] == 0.10
            # Refused whole, never partially queued.
            assert client.get("/api/tasks").json()["tasks"] == []

    def test_budget_endpoint_tracks_commitments(self, tmp_path):
        config, app = build(tmp_path, daily_budget_usd=5.0, confirm_cost_usd=0.0)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            before = client.get("/api/budget").json()
            assert before["committed_usd"] == 0
            client.post(f"/api/styles/{style_id}/matrix", json={"tones": ["light"]})
            after = client.get("/api/budget").json()
            assert after["committed_usd"] > 0
            assert after["remaining_usd"] < before["remaining_usd"]

    def test_budget_endpoint_requires_admin(self, tmp_path):
        """Spend figures reveal business volume."""
        config, app = build(tmp_path, admin_token="secret-token")
        with TestClient(app) as client:
            assert client.get("/api/budget").status_code == 401

    def test_worker_breaker_spends_nothing_when_tripped(self, tmp_path):
        """A batch authorized earlier must not spend money the budget no longer
        allows, and tripping must cost zero provider calls."""
        config, app = build(tmp_path, daily_budget_usd=5.0, confirm_cost_usd=0.0)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            queued = client.post(f"/api/styles/{style_id}/matrix",
                                 json={"tones": ["light"], "views": ["p2_open_hands"]})
            assert queued.status_code == 202
            task_id = queued.json()["created"][0]["task_id"]

            # Budget tightened after authorization.
            tight = make_config(tmp_path / "tight", daily_budget_usd=0.001)
            object.__setattr__(app.state.service, "config", tight)
            provider = MockImageProvider(allowed=True)
            worker = Worker(tight, app.state.db, app.state.service, provider)
            worker.start()
            deadline = time.time() + 20
            try:
                while time.time() < deadline:
                    task = client.get(f"/api/tasks/{task_id}").json()
                    if task["status"] in ("success", "failed"):
                        break
                    time.sleep(0.05)
            finally:
                worker.stop()

            task = client.get(f"/api/tasks/{task_id}").json()
            assert task["status"] == "failed"
            assert task["error_code"] == "budget_exceeded"
            assert provider.calls == 0
            # Non-retryable: retrying would only trip the breaker again.
            assert task["retry_count"] == 0

    def test_no_cap_configured_disables_the_breaker(self, tmp_path):
        config, app = build(tmp_path, daily_budget_usd=0.0, confirm_cost_usd=0.0)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            assert client.post(f"/api/styles/{style_id}/matrix", json={}).status_code == 202
            assert client.get("/api/budget").json()["enabled"] is False


class TestProfileSsrf:
    @pytest.mark.parametrize("url", [
        "http://example.com/v1",
        "https://127.0.0.1:9000/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/v1",
        "https://192.168.1.1/v1",
        "https://[::1]/v1",
        "https://user:pass@example.com/v1",
        "file:///etc/passwd",
    ])
    def test_unsafe_base_urls_are_refused(self, tmp_path, url):
        config, app = build(tmp_path)
        with TestClient(app) as client:
            response = client.post("/api/profiles", json={
                "name": "probe", "base_url": url,
                "api_key": "sk-probe-key-1234", "model": "m", "kind": "image",
            })
            assert response.status_code == 422, f"{url} was accepted"

    def test_update_cannot_smuggle_an_internal_url(self, tmp_path):
        """Creating safely then editing to an internal address must also fail."""
        config, app = build(tmp_path)
        with TestClient(app) as client:
            created = client.post("/api/profiles", json={
                "name": "ok", "base_url": "https://example.com/v1",
                "api_key": "sk-probe-key-1234", "model": "m", "kind": "image",
            })
            assert created.status_code == 201
            profile_id = created.json()["profile_id"]
            response = client.put(f"/api/profiles/{profile_id}",
                                  json={"base_url": "https://169.254.169.254/v1"})
            assert response.status_code == 422

    def test_llm_profiles_are_guarded_too(self, tmp_path):
        config, app = build(tmp_path)
        with TestClient(app) as client:
            response = client.post("/api/profiles", json={
                "name": "llm", "base_url": "https://10.1.2.3/v1",
                "api_key": "sk-probe-key-1234", "model": "m", "kind": "llm",
            })
            assert response.status_code == 422

    def test_escape_hatch_allows_private_but_never_http(self, tmp_path):
        config, app = build(tmp_path, allow_private_api_hosts=True)
        with TestClient(app) as client:
            allowed = client.post("/api/profiles", json={
                "name": "selfhosted", "base_url": "https://192.168.1.50:8000/v1",
                "api_key": "sk-probe-key-1234", "model": "m", "kind": "image",
            })
            assert allowed.status_code == 201
            plain = client.post("/api/profiles", json={
                "name": "plain", "base_url": "http://192.168.1.50:8000/v1",
                "api_key": "sk-probe-key-1234", "model": "m", "kind": "image",
            })
            assert plain.status_code == 422

    def test_escape_hatch_is_refused_in_production(self, tmp_path):
        config = make_config(
            tmp_path, env="production", image_provider="openai-compat",
            image_api_base_url="https://x.example/v1", image_api_key="sk-real",
            image_model="m", admin_token="tok", daily_budget_usd=20.0,
            allow_private_api_hosts=True,
        )
        problems = config.validate_for_serve()
        assert any("ALLOW_PRIVATE_API_HOSTS" in p for p in problems)

    def test_production_requires_a_budget_cap(self, tmp_path):
        config = make_config(
            tmp_path, env="production", image_provider="openai-compat",
            image_api_base_url="https://x.example/v1", image_api_key="sk-real",
            image_model="m", admin_token="tok", daily_budget_usd=0.0,
        )
        problems = config.validate_for_serve()
        assert any("DAILY_BUDGET" in p for p in problems)


class TestCloudflareSettingsAuth:
    CF_BODY = {
        "cf_account_id": "acct-1234567890",
        "cf_api_token": "cf-token-abcdefghij",
        "cf_d1_database_id": "db-1234567890",
        "cf_r2_bucket": "tryon-assets",
    }

    def test_reading_settings_requires_admin(self, tmp_path):
        """Account/database/bucket IDs name the production infrastructure."""
        config, app = build(tmp_path, admin_token="secret-token")
        with TestClient(app) as client:
            assert client.get("/api/settings/cloudflare").status_code == 401
            ok = client.get("/api/settings/cloudflare",
                            headers={"X-Admin-Token": "secret-token"})
            assert ok.status_code == 200

    def test_token_is_never_returned_in_full(self, tmp_path):
        config, app = build(tmp_path)
        with TestClient(app) as client:
            client.post("/api/settings/cloudflare", json=self.CF_BODY)
            body = client.get("/api/settings/cloudflare").json()
            assert body["cf_api_token"]["set"] is True
            assert "fingerprint" in body["cf_api_token"]
            assert self.CF_BODY["cf_api_token"] not in str(body)

    def test_credentials_can_be_cleared(self, tmp_path):
        """save() treats blank as 'keep', so revocation needs its own operation."""
        config, app = build(tmp_path)
        with TestClient(app) as client:
            client.post("/api/settings/cloudflare", json=self.CF_BODY)
            assert client.get("/api/settings/cloudflare").json()["configured"] is True

            # A blank save must NOT clear (that is the documented behaviour).
            client.post("/api/settings/cloudflare", json={})
            assert client.get("/api/settings/cloudflare").json()["configured"] is True

            cleared = client.request("DELETE", "/api/settings/cloudflare")
            assert cleared.status_code == 200
            assert cleared.json()["configured"] is False
            after = client.get("/api/settings/cloudflare").json()
            assert after["configured"] is False
            assert after["cf_api_token"]["set"] is False
            assert after["cf_account_id"] == ""

    def test_clearing_requires_admin(self, tmp_path):
        config, app = build(tmp_path, admin_token="secret-token")
        with TestClient(app) as client:
            assert client.request("DELETE",
                                  "/api/settings/cloudflare").status_code == 401

    def test_publish_refuses_after_credentials_are_cleared(self, tmp_path):
        config, app = build(tmp_path)
        with TestClient(app) as client:
            style_id = new_style(client, app, config)
            client.put(f"/api/styles/{style_id}/tryon-id",
                       json={"tryon_style_id": "007"})
            client.post("/api/settings/cloudflare", json=self.CF_BODY)
            client.request("DELETE", "/api/settings/cloudflare")
            response = client.post(f"/api/styles/{style_id}/publish")
            assert response.status_code == 409
            assert "未配置" in response.json()["detail"]
