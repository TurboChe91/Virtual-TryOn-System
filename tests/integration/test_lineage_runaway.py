"""The runaway that motivated the lineage budget.

Auto-regeneration and automatic correction used to keep separate depth counters in
task metadata, and neither copied the other's counter onto the child it created.
So a task could go regen -> correct -> regen -> ... indefinitely, each hop
resetting the limit the other path checked, and the advertised worst-case cost was
not a bound at all.

These tests drive the worker's real automatic paths and assert the shared ledger
stops the chain, then confirm the total paid calls stay inside what the estimate
promised.
"""

from __future__ import annotations

import time

from lunelle.budget import lineage_status
from lunelle.providers.mock import MockImageProvider
from lunelle.worker import Worker
from tests.conftest import make_config
from tests.integration.test_worker_flows import make_style


class AlwaysFailingQaProvider(MockImageProvider):
    """Renders an image that heuristic QA rejects, to trigger auto-regeneration.

    A tiny flat image fails the 30KB file-size floor, which is exactly the hard
    check that drives the first-shot-or-reroll policy.
    """

    def generate(self, request):
        result = super().generate(request)
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", request.size, (200, 200, 200)).save(buffer, format="PNG")
        return type(result)(
            image_bytes=buffer.getvalue(),
            external_request_id=result.external_request_id,
            actual_cost_usd=None,
            reference_used=result.reference_used,
            response_meta=result.response_meta,
        )


def _drain(service, timeout=45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = service.list_tasks(limit=500)
        if tasks and all(t["status"] in ("success", "failed", "cancelled")
                         for t in tasks):
            # Give the QA/auto-regen hook a moment to queue any follow-up.
            time.sleep(0.4)
            tasks = service.list_tasks(limit=500)
            if all(t["status"] in ("success", "failed", "cancelled") for t in tasks):
                return
        time.sleep(0.1)
    raise AssertionError("tasks did not settle")


class TestAutomaticWorkIsBounded:
    def test_regeneration_chain_stops_at_the_shared_budget(self, tmp_path):
        """QA fails every time, so auto-regen would loop forever if unbounded."""
        config = make_config(tmp_path, auto_regen_max=1, max_lineage_descendants=2,
                             max_retries=0, max_concurrency=1)
        from lunelle.db import Database, migrate
        from lunelle.tasks import TaskService

        db = Database(config.db_path)
        migrate(db.conn())
        service = TaskService(db, config)
        try:
            style = make_style(service)
            plan = service.create_generation(style["style_id"], ["grid"])
            root = plan.created[0]["task_id"]

            provider = AlwaysFailingQaProvider(allowed=True)
            worker = Worker(config, db, service, provider)
            worker.start()
            try:
                _drain(service)
            finally:
                worker.stop()

            tasks = service.list_tasks(limit=500)
            status = lineage_status(db, root)
            # 1 root + at most max_lineage_descendants automatic children.
            assert len(tasks) <= 1 + config.max_lineage_descendants, (
                f"{len(tasks)} tasks created; the shared budget allows "
                f"{1 + config.max_lineage_descendants}")
            assert status["descendant_count"] <= config.max_lineage_descendants
            # Every task belongs to the same lineage.
            assert {t.get("root_task_id") for t in tasks} == {root}
            # Paid calls never exceeded what the ledger allowed.
            assert provider.calls <= 1 + config.max_lineage_descendants
        finally:
            db.close_all()

    def test_zero_descendants_means_exactly_one_generation(self, tmp_path):
        config = make_config(tmp_path, auto_regen_max=1, max_lineage_descendants=0,
                             max_retries=0, max_concurrency=1)
        from lunelle.db import Database, migrate
        from lunelle.tasks import TaskService

        db = Database(config.db_path)
        migrate(db.conn())
        service = TaskService(db, config)
        try:
            style = make_style(service)
            service.create_generation(style["style_id"], ["grid"])
            provider = AlwaysFailingQaProvider(allowed=True)
            worker = Worker(config, db, service, provider)
            worker.start()
            try:
                _drain(service)
            finally:
                worker.stop()
            assert len(service.list_tasks(limit=500)) == 1
            assert provider.calls == 1
        finally:
            db.close_all()

    def test_kill_switch_disables_automatic_work_entirely(self, tmp_path):
        """LUNELLE_AUTO_REGEN_MAX=0 keeps its documented meaning."""
        config = make_config(tmp_path, auto_regen_max=0, max_lineage_descendants=5,
                             max_retries=0, max_concurrency=1)
        from lunelle.db import Database, migrate
        from lunelle.tasks import TaskService

        db = Database(config.db_path)
        migrate(db.conn())
        service = TaskService(db, config)
        try:
            style = make_style(service)
            service.create_generation(style["style_id"], ["grid"])
            provider = AlwaysFailingQaProvider(allowed=True)
            worker = Worker(config, db, service, provider)
            worker.start()
            try:
                _drain(service)
            finally:
                worker.stop()
            assert len(service.list_tasks(limit=500)) == 1
            assert provider.calls == 1
        finally:
            db.close_all()

    def test_descendants_inherit_the_root_rather_than_starting_a_new_lineage(
        self, config, db, service
    ):
        """The mechanism behind the old runaway: a child that opened its own
        lineage would receive a brand-new allowance."""
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        root = plan.created[0]["task_id"]

        child_plan = service.create_generation(
            style["style_id"], ["grid"], force=True, root_override=root)
        child = child_plan.created[0]["task_id"]
        assert service.get_task(child, with_details=False)["root_task_id"] == root

        # One ledger, shared.
        assert lineage_status(db, root)["tracked"] is True
        rows = db.conn().execute(
            "SELECT COUNT(*) AS n FROM generation_lineages").fetchone()["n"]
        assert rows == 1, "the descendant must not have opened its own lineage"

    def test_correction_stays_in_the_source_lineage(self, config, db, service):
        from lunelle.providers.mock import MockImageProvider as Mock
        from tests.integration.test_worker_flows import run_worker_until_settled

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        root = plan.created[0]["task_id"]
        run_worker_until_settled(config, db, service, Mock(allowed=True))

        corrected = service.create_correction(root, correction_text="fix nail 3")
        assert corrected["root_task_id"] == root
        rows = db.conn().execute(
            "SELECT COUNT(*) AS n FROM generation_lineages").fetchone()["n"]
        assert rows == 1

    def test_lineage_endpoint_explains_why_automatic_work_stopped(self, tmp_path):
        from fastapi.testclient import TestClient

        from lunelle.server import create_app

        config = make_config(tmp_path, max_lineage_descendants=1)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Lineage View", "description": "coral almond glossy nails",
            }).json()["style"]
            queued = client.post(f"/api/styles/{style['style_id']}/generate",
                                 json={"output_types": ["grid"]}).json()
            task_id = queued["created"][0]["task_id"]
            body = client.get(f"/api/tasks/{task_id}/lineage").json()
            assert body["tracked"] is True
            assert body["max_descendants"] == 1
            assert body["descendant_count"] == 0
            assert body["exhausted"] is False
