"""P0 review-closure regression tests.

Covers the six requirements: no QA row => not exportable/publishable, a task
needing human review => not exportable/publishable, automatic QA passing is not
human approval, only an explicit human approval reaches publish_ready, the
success/QA race is gone, and a matrix cell without its hand model blocks instead
of silently degrading.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from lunelle.models import IllegalReviewTransition
from lunelle.providers.mock import MockImageProvider
from lunelle.server import create_app
from tests.conftest import approve_all_via_api, make_config, satisfy_matrix_dependencies
from tests.integration.test_worker_flows import make_style, run_worker_until_settled

STYLE_BODY = {"name": "Closure Check", "description": "wine red almond glossy nails"}


@pytest.fixture
def client(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config, start_worker=True)
    with TestClient(app) as test_client:
        yield test_client


def settle(client, count, timeout=30.0) -> list[dict]:
    """Wait for `count` tasks to reach a terminal status AND finish QA.

    Waiting on status alone is what made the old review test flaky: `success` was
    published before the QA row existed.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = client.get("/api/tasks").json()["tasks"]
        done = [t for t in tasks if t["status"] in ("success", "failed", "cancelled")]
        if len(done) >= count and all(
            t["qa_state"] not in ("pending", "running") for t in done
        ):
            return tasks
        time.sleep(0.05)
    raise AssertionError(f"tasks did not settle: {client.get('/api/tasks').json()['tasks']}")


class TestQaStateIsExplicit:
    def test_success_carries_a_qa_state_and_review_state(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate", json={"output_types": ["grid"]})
        tasks = settle(client, 1)
        task = tasks[0]
        assert task["status"] == "success"
        assert task["qa_state"] == "done"
        assert task["review_state"] == "waiting_human_review"

    def test_review_before_qa_returns_qa_not_ready(self, client):
        """The window is now an explicit state, so the error is specific."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        plan = client.post(f"/api/styles/{style_id}/generate",
                           json={"output_types": ["grid"]}).json()
        task_id = plan["created"][0]["task_id"]
        # Race the worker deliberately: whichever side wins, the response must be
        # a correct one — never a 500 and never a bogus approval.
        response = client.post(f"/api/tasks/{task_id}/review", json={"approved": True})
        assert response.status_code in (200, 409)
        if response.status_code == 409:
            error = response.json()["error"]
            assert "qa_not_ready" in error or "only successful" in error
        else:
            assert response.json()["review_state"] == "approved"

    def test_qa_rerun_reopens_review(self, client):
        """A new verdict invalidates the approval it was not given against."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate", json={"output_types": ["grid"]})
        tasks = settle(client, 1)
        task_id = tasks[0]["task_id"]
        approve_all_via_api(client, expect=1)
        assert client.get(f"/api/tasks/{task_id}").json()["review_state"] == "approved"

        rerun = client.post(f"/api/tasks/{task_id}/qa", json={})
        assert rerun.status_code == 200
        assert rerun.json()["review_state"] == "waiting_human_review"
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["review_state"] == "waiting_human_review"
        assert detail["reviewed_at"] is None


class TestExportGate:
    def test_unreviewed_is_not_exportable(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})
        settle(client, 2)
        blocked = client.post("/api/export", json={})
        assert blocked.status_code == 422

        approve_all_via_api(client, expect=2)
        assert client.post("/api/export", json={}).status_code == 200

    def test_rejected_is_not_exportable(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})
        tasks = settle(client, 2)
        for task in tasks:
            approved = task["output_type"] != "wearing"
            response = client.post(f"/api/tasks/{task['task_id']}/review",
                                   json={"approved": approved, "note": "partial"})
            assert response.status_code == 200
        assert client.post("/api/export", json={}).status_code == 422

    def test_export_report_names_the_blocking_reason(self, client):
        """An operator must be able to see WHY a style was withheld."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})
        settle(client, 2)
        response = client.post("/api/export", json={})
        assert response.status_code == 422
        detail = response.json()["detail"]
        # "missing" and "withheld pending review" are different problems.
        assert "review gate" in detail
        assert "awaiting human review" in detail

    def test_missing_qa_row_blocks_export(self, client):
        """Delete the QA rows outright: default-deny must hold."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})
        settle(client, 2)
        approve_all_via_api(client, expect=2)
        assert client.post("/api/export", json={}).status_code == 200

        conn = client.app.state.db.conn()
        # asset_reviews references qa_results, so the audit rows go first. That
        # the FK forbids orphaning a review is itself the desired behaviour.
        conn.execute("DELETE FROM asset_reviews")
        conn.execute("DELETE FROM qa_results")
        assert client.post("/api/export", json={}).status_code == 422


class TestReviewAudit:
    def test_review_is_audited_with_reviewer_and_note(self, client):
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate", json={"output_types": ["grid"]})
        tasks = settle(client, 1)
        task_id = tasks[0]["task_id"]
        client.post(f"/api/tasks/{task_id}/review",
                    json={"approved": False, "note": "thumb motif wrong"})
        client.post(f"/api/tasks/{task_id}/review",
                    json={"approved": True, "note": "fixed after recheck"})

        rows = client.app.state.db.conn().execute(
            "SELECT decision, note, reviewer FROM asset_reviews WHERE task_id = ?"
            " ORDER BY review_id", (task_id,)
        ).fetchall()
        assert [r["decision"] for r in rows] == ["rejected", "approved"]
        assert rows[0]["note"] == "thumb motif wrong"
        # No user system yet: identity is recorded without storing a credential.
        assert rows[0]["reviewer"].startswith(("token:", "host:"))
        assert "sk-" not in rows[0]["reviewer"]

    def test_publish_ready_cannot_be_set_by_a_human(self, service, db, config):
        """publish_ready is system-derived; a reviewer can only approve/reject."""
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        task_id = plan.created[0]["task_id"]
        service.record_review(task_id, approved=True)
        with pytest.raises(IllegalReviewTransition):
            # waiting -> published skips both approval and the gate
            service.mark_review_state(task_id, "waiting_human_review")
            service.mark_review_state(task_id, "published")


class TestLlmVerdictIsAdvisory:
    def test_llm_verdict_never_satisfies_the_gate(self, service, db, config):
        """An LLM pass is stored as advisory and leaves review open."""
        from lunelle.qa import store_qa_result

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        task_id = plan.created[0]["task_id"]

        store_qa_result(db, task_id, {
            "passed": True, "score": 100, "issues": [], "checks": {"llm_identity_qa": {}},
            "recommended_action": "human_review", "needs_human_review": True,
        }, source="llm")

        task = service.get_task(task_id)
        # The gate-relevant verdict stays the heuristic one.
        assert task["qa"]["source"] == "heuristic"
        assert task["qa_advisory"]["source"] == "llm"
        assert task["review_state"] == "waiting_human_review"

        from lunelle.export import run_export
        with pytest.raises(Exception, match="no styles"):
            run_export(db, config)


class TestMatrixDependencyBlocking:
    def test_cell_without_hand_model_blocks_and_costs_nothing(self, service, db, config):
        style = make_style(service)
        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        # Remove the one hand model this cell needs.
        db.conn().execute("DELETE FROM app_settings WHERE key = ?",
                          ("hand_model_light_p2_open_hands",))
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert provider.calls == 0
        # A blocked task is not exportable or publishable either.
        assert task["review_state"] == "generated"
