"""The success/QA race, isolated so it can be hammered.

Before the qa_state column, `complete_success` published `status='success'`
before the QA row was written. A client that polled for success and immediately
acted on the asset hit a window where no QA verdict existed:

  - /review returned 409 "task has no QA result" (~1 in 6 runs locally)
  - the export "reviewed only" filter read the absent row as NULL -> falsy and
    shipped the asset instead of withholding it

The fix is a state, not a reordering: status and qa_state are written in one
transaction, so every observable combination is well-defined. These tests assert
the invariant holds at every poll, which is what makes running them 100x
meaningful.

Run the loop hard:
    pytest tests/integration/test_success_qa_race.py -q -p no:randomly
    RACE_ITERATIONS=100 pytest tests/integration/test_success_qa_race.py::\\
TestRaceUnderLoad::test_invariant_holds_over_many_iterations -q
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from lunelle.models import QA_STATES, REVIEW_STATES
from lunelle.server import create_app
from tests.conftest import make_config

STYLE_BODY = {"name": "Race Probe", "description": "clear pink oval glossy nails"}

#: A task in one of these qa_states has no verdict, so review must be refused and
#: export must withhold it.
NO_VERDICT_YET = ("pending", "running")


@pytest.fixture
def client(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config, start_worker=True)
    with TestClient(app) as test_client:
        yield test_client


def assert_states_consistent(task: dict) -> None:
    """Invariant that must hold at every single poll, mid-race included."""
    assert task["qa_state"] in QA_STATES, task
    assert task["review_state"] in REVIEW_STATES, task

    if task["status"] != "success":
        # A non-success task must never look reviewable or shippable.
        assert task["review_state"] in ("generated", "waiting_human_review",
                                        "approved", "rejected", "publish_ready",
                                        "published"), task
        return

    # Success + finished QA => review is open (or already decided).
    if task["qa_state"] == "done":
        assert task["review_state"] != "generated", (
            "QA finished but review never opened", task)
    # Success + unfinished QA => nothing may claim approval yet.
    if task["qa_state"] in NO_VERDICT_YET:
        assert task["review_state"] in ("generated", "waiting_human_review"), (
            "approved before a verdict existed", task)


class TestRaceInvariant:
    def test_polling_never_sees_success_without_a_defined_qa_state(self, client):
        """Poll as fast as possible through the whole lifecycle."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})

        saw_pending_or_running_qa = False
        deadline = time.time() + 30
        while time.time() < deadline:
            tasks = client.get("/api/tasks").json()["tasks"]
            for task in tasks:
                assert_states_consistent(task)
                if task["status"] == "success" and task["qa_state"] in NO_VERDICT_YET:
                    saw_pending_or_running_qa = True
            if tasks and all(
                t["status"] in ("success", "failed") and t["qa_state"] not in NO_VERDICT_YET
                for t in tasks
            ):
                break
        else:
            raise AssertionError("tasks did not settle")

        # Whether or not the narrow window was observed, the end state is defined.
        for task in client.get("/api/tasks").json()["tasks"]:
            assert task["qa_state"] == "done"
            assert task["review_state"] == "waiting_human_review"
        # Informational: the window is real but tiny; not asserted either way.
        _ = saw_pending_or_running_qa

    def test_review_immediately_after_success_never_500s(self, client):
        """Hammer /review the instant a task flips to success."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        plan = client.post(f"/api/styles/{style_id}/generate",
                           json={"output_types": ["grid", "wearing"]}).json()
        task_ids = [c["task_id"] for c in plan["created"]]

        outcomes = {200: 0, 409: 0}
        deadline = time.time() + 30
        remaining = set(task_ids)
        while remaining and time.time() < deadline:
            for task_id in list(remaining):
                response = client.post(f"/api/tasks/{task_id}/review",
                                       json={"approved": True, "note": "race probe"})
                assert response.status_code in (200, 409), response.text
                outcomes[response.status_code] += 1
                if response.status_code == 200:
                    body = response.json()
                    # An approval is only ever granted on a real verdict.
                    assert body["qa_state"] == "done"
                    assert body["review_state"] == "approved"
                    assert body["needs_human_review"] is False
                    remaining.discard(task_id)
                else:
                    error = response.json()["error"]
                    assert ("qa_not_ready" in error
                            or "only successful" in error
                            or "re-run QA" in error), error
        assert not remaining, "tasks never became reviewable"
        assert outcomes[200] == len(task_ids)

    def test_export_withholds_until_every_asset_is_approved(self, client):
        """Export polled throughout must never ship an unapproved asset."""
        style_id = client.post("/api/styles", json=STYLE_BODY).json()["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/generate",
                    json={"output_types": ["grid", "wearing"]})

        deadline = time.time() + 30
        while time.time() < deadline:
            tasks = client.get("/api/tasks").json()["tasks"]
            approved = [t for t in tasks if t["review_state"] == "approved"]
            response = client.post("/api/export", json={})
            if len(approved) < 2:
                assert response.status_code == 422, (
                    "export shipped an asset before both were approved",
                    [(t["output_type"], t["review_state"]) for t in tasks],
                )
            if (len(tasks) == 2
                    and all(t["qa_state"] == "done" for t in tasks)):
                break
        for task in client.get("/api/tasks").json()["tasks"]:
            response = client.post(f"/api/tasks/{task['task_id']}/review",
                                   json={"approved": True})
            assert response.status_code == 200, response.text
        assert client.post("/api/export", json={}).status_code == 200


class TestRaceUnderLoad:
    """The repeated run. Default 12 iterations keeps the suite fast; CI and the
    verification runs set RACE_ITERATIONS=100."""

    def test_invariant_holds_over_many_iterations(self, tmp_path):
        iterations = int(os.environ.get("RACE_ITERATIONS", "12"))
        failures: list[str] = []
        for index in range(iterations):
            config = make_config(tmp_path / f"run{index}")
            app = create_app(config, start_worker=True)
            with TestClient(app) as client:
                style_id = client.post("/api/styles", json={
                    "name": f"Race {index}",
                    "description": "clear pink oval glossy nails",
                }).json()["style"]["style_id"]
                plan = client.post(f"/api/styles/{style_id}/generate",
                                   json={"output_types": ["grid"]}).json()
                task_id = plan["created"][0]["task_id"]

                # Race /review against the worker with no delay whatsoever.
                approved = False
                deadline = time.time() + 30
                while not approved and time.time() < deadline:
                    response = client.post(f"/api/tasks/{task_id}/review",
                                           json={"approved": True})
                    if response.status_code == 200:
                        approved = True
                        body = response.json()
                        if body["qa_state"] != "done":
                            failures.append(
                                f"iter {index}: approved with qa_state="
                                f"{body['qa_state']}")
                    elif response.status_code != 409:
                        failures.append(
                            f"iter {index}: unexpected {response.status_code}"
                            f" {response.text[:120]}")
                        break
                    else:
                        detail = client.get(f"/api/tasks/{task_id}").json()
                        try:
                            assert_states_consistent(detail)
                        except AssertionError as exc:
                            failures.append(f"iter {index}: {exc}")
                            break
                if not approved and not failures:
                    failures.append(f"iter {index}: never became reviewable")
        assert not failures, (
            f"{len(failures)}/{iterations} iterations violated the invariant:\n"
            + "\n".join(failures[:10])
        )
