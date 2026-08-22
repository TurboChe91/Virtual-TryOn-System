"""Dependency failure propagation and repair lineage.

Two defects, both in how tasks relate to each other:

1. claim_next treated a dependency as satisfied when it was merely no longer
   ACTIVE, so a FAILED grid released the wearing shot waiting on it, which then
   ran and produced a paid, silently degraded text-only asset.
2. root_task_id said which lineage a task belonged to but not where in it, so
   "what is this a repair of" was a scan of metadata JSON.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lunelle.providers.base import ProviderError
from lunelle.providers.mock import MockImageProvider
from lunelle.server import create_app
from lunelle.tasks import ConflictError
from tests.conftest import make_config
from tests.integration.test_worker_flows import make_style, run_worker_until_settled


class GridFails(MockImageProvider):
    """Fails the grid, would happily render anything else."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.wearing_calls = 0

    def generate(self, request):
        if "2 rows and 5 columns" in request.prompt:
            raise ProviderError("content_policy", "grid rejected", retryable=False)
        self.wearing_calls += 1
        return super().generate(request)


class TestDependencyPropagation:
    def test_failed_dependency_fails_the_dependent_without_spending(
        self, config, db, service
    ):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid", "wearing"])
        provider = GridFails(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        tasks = {t["output_type"]: service.get_task(t["task_id"])
                 for t in service.list_tasks()}
        assert tasks["grid"]["status"] == "failed"
        assert tasks["wearing"]["status"] == "failed"
        assert tasks["wearing"]["error_code"] == "dependency_failed"
        assert "refusing to generate a degraded asset" in tasks["wearing"]["error_message"]
        # The whole point: no paid call for the dependent.
        assert provider.wearing_calls == 0

    def test_dependency_failure_is_not_auto_retried(self, config, db, service):
        """Retrying cannot help until the dependency itself succeeds."""
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid", "wearing"])
        run_worker_until_settled(config, db, service, GridFails(allowed=True))
        wearing = next(t for t in service.list_tasks() if t["output_type"] == "wearing")
        assert wearing["retry_count"] == 0

    def test_cancelling_a_dependency_also_propagates(self, config, db, service):
        """A cancelled task is never going to run either."""
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "wearing"])
        grid = next(c for c in plan.created if c["output_type"] == "grid")
        wearing = next(c for c in plan.created if c["output_type"] == "wearing")

        service.cancel(grid["task_id"])
        dependent = service.get_task(wearing["task_id"], with_details=False)
        assert dependent["status"] == "failed"
        assert dependent["error_code"] == "dependency_failed"

    def test_propagation_is_transitive(self, config, db, service):
        """grid -> wearing -> (a task waiting on wearing) must all fail."""
        from lunelle.db import transaction

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "wearing"])
        grid = next(c for c in plan.created if c["output_type"] == "grid")
        wearing = next(c for c in plan.created if c["output_type"] == "wearing")
        # Add a third task waiting on the wearing shot.
        third = service.create_generation(style["style_id"], ["hero"], force=True)
        third_id = third.created[0]["task_id"]
        conn = db.conn()
        with transaction(conn):
            conn.execute("UPDATE tasks SET wait_for_task_id = ? WHERE task_id = ?",
                         (wearing["task_id"], third_id))

        service.cancel(grid["task_id"])
        for task_id in (wearing["task_id"], third_id):
            task = service.get_task(task_id, with_details=False)
            assert task["status"] == "failed", task_id
            assert task["error_code"] == "dependency_failed", task_id

    def test_successful_dependency_revives_its_dependent(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid", "wearing"])

        class GridFailsOnce(MockImageProvider):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.grid_calls = 0

            def generate(self, request):
                if "2 rows and 5 columns" in request.prompt:
                    self.grid_calls += 1
                    if self.grid_calls == 1:
                        raise ProviderError("content_policy", "no", retryable=False)
                return MockImageProvider.generate(self, request)

        provider = GridFailsOnce(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        grid = next(t for t in service.list_tasks() if t["output_type"] == "grid")
        wearing = next(t for t in service.list_tasks() if t["output_type"] == "wearing")
        assert wearing["status"] == "failed"

        service.manual_retry(grid["task_id"])
        run_worker_until_settled(config, db, service, provider, timeout=60)
        after = service.get_task(wearing["task_id"], with_details=False)
        assert after["status"] == "success"
        assert after["error_code"] is None

    def test_revival_leaves_unrelated_failures_alone(self, config, db, service):
        """A dependent that failed for its OWN reason must stay failed: the
        dependency succeeding says nothing about it."""
        from lunelle.db import transaction

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "wearing"])
        grid = next(c for c in plan.created if c["output_type"] == "grid")
        wearing = next(c for c in plan.created if c["output_type"] == "wearing")
        conn = db.conn()
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET status = 'failed', error_code = 'content_policy',"
                " error_message = 'its own problem' WHERE task_id = ?",
                (wearing["task_id"],),
            )
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        assert service.get_task(grid["task_id"], with_details=False)["status"] == "success"
        dependent = service.get_task(wearing["task_id"], with_details=False)
        assert dependent["status"] == "failed"
        assert dependent["error_code"] == "content_policy"

    def test_blocked_dependent_is_not_publishable(self, config, db, service):
        from lunelle.export import ExportError, run_export

        style = make_style(service)
        service.create_generation(style["style_id"], ["grid", "wearing"])
        run_worker_until_settled(config, db, service, GridFails(allowed=True))
        try:
            run_export(db, config)
        except ExportError as exc:
            assert "missing" in str(exc) or "review gate" in str(exc)
        else:
            raise AssertionError("export must refuse a style with no usable pair")

    def test_claim_requires_success_not_merely_inactive(self, config, db, service):
        """Direct test of the claim_next condition that was wrong.

        A dependency parked in `failed` (with propagation bypassed) must still not
        release its dependent — that is the backstop for the window before
        propagation runs.
        """
        from lunelle.db import transaction

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "wearing"])
        grid = next(c for c in plan.created if c["output_type"] == "grid")
        conn = db.conn()
        # Fail the grid WITHOUT propagation, simulating that window.
        with transaction(conn):
            conn.execute("UPDATE tasks SET status = 'failed', error_code = 'x'"
                         " WHERE task_id = ?", (grid["task_id"],))
        claimed = []
        while True:
            task = service.claim_next()
            if task is None:
                break
            claimed.append(task["output_type"])
            with transaction(conn):
                conn.execute("UPDATE tasks SET status = 'success' WHERE task_id = ?",
                             (task["task_id"],))
        assert "wearing" not in claimed, "a failed dependency released its dependent"


class TestRepairLineage:
    def test_precision_workbench_lists_and_selects_any_successful_version(
        self, config, db, service,
    ):
        style = make_style(service)
        root = service.create_generation(style["style_id"], ["grid"]).created[0]["task_id"]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        child = service.create_correction(root, correction_text="Only correct nail-03.")
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        first = service.select_candidate(root, selected_by="owner", note="best lighting")
        assert first["selected_task_id"] == root
        selected = service.select_candidate(
            child["task_id"], selected_by="owner", note="better nail-03"
        )
        assert selected["selected_task_id"] == child["task_id"]

        workbench = service.lineage_for(root)
        assert workbench["selection"]["selected_task_id"] == child["task_id"]
        assert workbench["selection"]["note"] == "better nail-03"
        assert [item["task_id"] for item in workbench["candidates"]] == [
            root, child["task_id"],
        ]
        assert [item["version"] for item in workbench["candidates"]] == [1, 2]
        assert workbench["candidates"][1]["correction_text"] == "Only correct nail-03."
        # "Best" is an editing choice, never a shortcut through the publish gate.
        assert service.get_task(child["task_id"], with_details=False)["review_state"] \
            == "waiting_human_review"

    def test_pending_or_missing_candidate_cannot_be_selected(self, config, db, service):
        style = make_style(service)
        task_id = service.create_generation(style["style_id"], ["grid"]).created[0]["task_id"]
        with pytest.raises(ConflictError, match="successful candidate"):
            service.select_candidate(task_id, selected_by="owner")

    def test_correction_records_parent_and_depth(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        root = plan.created[0]["task_id"]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        first = service.create_correction(root, correction_text="fix nail 3")
        assert first["parent_task_id"] == root
        assert first["lineage_depth"] == 1
        assert first["lineage_reason"] == "correction"
        assert first["root_task_id"] == root

    def test_chain_depth_increments_down_the_repair_chain(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        root = plan.created[0]["task_id"]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        depths = []
        current = root
        for index in range(3):
            child = service.create_correction(current, correction_text=f"fix {index}")
            depths.append(child["lineage_depth"])
            # Settle it so the next correction has a successful base.
            run_worker_until_settled(config, db, service,
                                     MockImageProvider(allowed=True), timeout=60)
            current = child["task_id"]
        assert depths == [1, 2, 3]

    def test_ancestors_walk_back_to_the_root(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        root = plan.created[0]["task_id"]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        first = service.create_correction(root, correction_text="one")
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True),
                                 timeout=60)
        second = service.create_correction(first["task_id"], correction_text="two")

        ancestors = service.ancestors_of(second["task_id"])
        assert [row["task_id"] for row in ancestors] == [first["task_id"], root]
        # Nearest ancestor first, so a reader sees the immediate predecessor.
        assert ancestors[0]["lineage_depth"] == 1

    def test_descendants_list_everything_derived(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        root = plan.created[0]["task_id"]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        first = service.create_correction(root, correction_text="one")
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True),
                                 timeout=60)
        second = service.create_correction(first["task_id"], correction_text="two")

        descendants = service.descendants_of(root)
        found = {row["task_id"] for row in descendants}
        assert found == {first["task_id"], second["task_id"]}

    def test_auto_regeneration_records_its_parent(self, config, db, service):
        """An automatic re-roll's ancestry has to be reconstructible too."""
        from io import BytesIO

        from PIL import Image

        class AlwaysFailsQa(MockImageProvider):
            def generate(self, request):
                result = super().generate(request)
                buffer = BytesIO()
                Image.new("RGB", request.size, (200, 200, 200)).save(buffer, "PNG")
                return type(result)(
                    image_bytes=buffer.getvalue(),
                    external_request_id=result.external_request_id,
                    actual_cost_usd=None, reference_used=result.reference_used,
                    response_meta=result.response_meta,
                )

        from lunelle.db import Database, migrate
        from lunelle.tasks import TaskService

        cfg = make_config(config.data_dir.parent / "regen", auto_regen_max=1,
                          max_lineage_descendants=1, max_retries=0,
                          max_concurrency=1)
        database = Database(cfg.db_path)
        migrate(database.conn())
        svc = TaskService(database, cfg)
        try:
            style = make_style(svc)
            plan = svc.create_generation(style["style_id"], ["grid"])
            root = plan.created[0]["task_id"]
            run_worker_until_settled(cfg, database, svc, AlwaysFailsQa(allowed=True),
                                     timeout=60)
            children = svc.descendants_of(root)
            assert children, "auto-regeneration produced no tracked descendant"
            assert children[0]["lineage_reason"] == "auto_regeneration"
            assert children[0]["lineage_depth"] == 1
        finally:
            database.close_all()

    def test_lineage_endpoint_returns_the_chain(self, tmp_path):
        config = make_config(tmp_path)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Chain", "description": "rose square glossy nails",
            }).json()["style"]
            queued = client.post(f"/api/styles/{style['style_id']}/generate",
                                 json={"output_types": ["grid"]}).json()
            task_id = queued["created"][0]["task_id"]
            body = client.get(f"/api/tasks/{task_id}/lineage").json()
            assert body["this_task"]["lineage_depth"] == 0
            assert body["this_task"]["parent_task_id"] is None
            assert body["this_task"]["lineage_reason"] == "operator_request"
            assert len(body["candidates"]) == 1
            assert body["selection"] is None
            assert body["ancestors"] == []
            assert body["descendants"] == []

    def test_list_tasks_exposes_lineage_columns(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        row = service.list_tasks()[0]
        for key in ("root_task_id", "parent_task_id", "lineage_depth",
                    "lineage_reason", "input_fingerprint"):
            assert key in row, f"list_tasks is missing {key}"
