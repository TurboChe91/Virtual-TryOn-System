"""Integration tests of the task pipeline: worker, retries, recovery, persistence."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from lunelle.db import Database, migrate, transaction
from lunelle.providers.base import ProviderError
from lunelle.providers.mock import MockImageProvider
from lunelle.schemas import StyleCreateRequest
from lunelle.styles import build_style_spec
from lunelle.tasks import ConflictError, TaskService
from lunelle.worker import Worker


def make_style(service, name="Pearl French", description="milky white almond pearl glossy"):
    outcome = build_style_spec(
        StyleCreateRequest(name=name, description=description), service.taken_skus()
    )
    return service.create_style(
        outcome.spec, source_type=outcome.source_type, source_input={},
        parser=outcome.parser, warnings=outcome.warnings,
    )


def wait_until(predicate, timeout=30.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def run_worker_until_settled(config, db, service, provider, timeout=30.0):
    worker = Worker(config, db, service, provider)
    worker.start()
    try:
        settled = wait_until(
            lambda: all(
                t["status"] in ("success", "failed", "cancelled")
                for t in service.list_tasks()
            ),
            timeout=timeout,
        )
        assert settled, f"tasks did not settle: {[ (t['task_id'], t['status']) for t in service.list_tasks() ]}"
    finally:
        worker.stop()


class TestHappyPath:
    def test_grid_and_wearing_succeed_with_reference(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "wearing"])
        assert len(plan.created) == 2

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        tasks = {t["output_type"]: service.get_task(t["task_id"]) for t in service.list_tasks()}
        for _output_type, task in tasks.items():
            assert task["status"] == "success"
            assert Path(task["output_path"]).is_file()
            assert task["qa"] is not None
            assert task["attempts"][-1]["outcome"] == "success"
        assert tasks["wearing"]["metadata"]["reference_used"] is True
        # DB path must equal the real file on disk
        assert tasks["grid"]["output_path"].endswith(".png")

    def test_concurrent_styles_no_collisions(self, config, db, service):
        for i in range(3):
            style = make_style(service, name=f"Style {i}", description=f"red square nails v{i}")
            service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        tasks = service.list_tasks()
        assert len(tasks) == 3
        paths = {t["output_path"] for t in tasks}
        assert len(paths) == 3  # no overwrites
        assert all(t["status"] == "success" for t in tasks)


class TestRetries:
    def test_retryable_error_retries_then_succeeds(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        provider = MockImageProvider(
            allowed=True,
            fail_with=ProviderError("timeout", "simulated timeout", retryable=True),
            fail_times=1,
        )
        run_worker_until_settled(config, db, service, provider, timeout=40)
        task = service.get_task(service.list_tasks()[0]["task_id"])
        assert task["status"] == "success"
        assert task["retry_count"] == 1
        outcomes = [a["outcome"] for a in task["attempts"]]
        assert outcomes == ["error", "success"]

    def test_non_retryable_error_fails_immediately(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        provider = MockImageProvider(
            allowed=True,
            fail_with=ProviderError("auth_invalid", "bad key", retryable=False),
        )
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(service.list_tasks()[0]["task_id"])
        assert task["status"] == "failed"
        assert task["retry_count"] == 0
        assert task["error_code"] == "auth_invalid"

    def test_retry_budget_exhausted(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        provider = MockImageProvider(
            allowed=True,
            fail_with=ProviderError("server_error", "boom", retryable=True),
        )
        run_worker_until_settled(config, db, service, provider, timeout=60)
        task = service.get_task(service.list_tasks()[0]["task_id"])
        assert task["status"] == "failed"
        assert task["retry_count"] == config.max_retries
        assert len(task["attempts"]) == config.max_retries + 1  # no infinite retry

    def test_manual_retry_after_failure(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        failing = MockImageProvider(
            allowed=True, fail_with=ProviderError("bad_request", "nope", retryable=False)
        )
        run_worker_until_settled(config, db, service, failing)
        task_id = service.list_tasks()[0]["task_id"]
        assert service.get_task(task_id, with_details=False)["status"] == "failed"

        service.manual_retry(task_id, note="fix and retry")
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        task = service.get_task(task_id)
        assert task["status"] == "success"
        assert task["metadata"]["manual_retries"][0]["note"] == "fix and retry"

    def test_manual_retry_refused_for_success(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        task_id = service.list_tasks()[0]["task_id"]
        with pytest.raises(ConflictError, match="only failed or cancelled"):
            service.manual_retry(task_id)

    def test_grid_failure_does_not_break_wearing(self, config, db, service):
        """Wearing proceeds text-only when grid failed (independence rule)."""
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid", "wearing"])

        calls = {"n": 0}
        good = MockImageProvider(allowed=True)

        class GridFailsProvider(MockImageProvider):
            def generate(self, request):
                calls["n"] += 1
                if "2 rows and 5 columns" in request.prompt:
                    raise ProviderError("content_policy", "grid rejected", retryable=False)
                return good.generate(request)

        run_worker_until_settled(config, db, service, GridFailsProvider(allowed=True))
        tasks = {t["output_type"]: service.get_task(t["task_id"]) for t in service.list_tasks()}
        assert tasks["grid"]["status"] == "failed"
        assert tasks["wearing"]["status"] == "success"
        assert tasks["wearing"]["metadata"]["reference_used"] is False


class TestIdempotency:
    def test_duplicate_generation_reuses(self, config, db, service):
        style = make_style(service)
        p1 = service.create_generation(style["style_id"], ["grid", "wearing"])
        p2 = service.create_generation(style["style_id"], ["grid", "wearing"])
        assert len(p1.created) == 2 and not p2.created
        assert {t["task_id"] for t in p2.reused} == {t["task_id"] for t in p1.created}

    def test_success_not_regenerated_without_force(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        plan = service.create_generation(style["style_id"], ["grid"])
        assert not plan.created and len(plan.skipped) == 1
        assert "force" in plan.skipped[0]["reason"]

    def test_force_creates_new_version(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        first = service.list_tasks()[0]
        plan = service.create_generation(style["style_id"], ["grid"], force=True)
        assert len(plan.created) == 1
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        second = service.get_task(plan.created[0]["task_id"], with_details=False)
        assert second["status"] == "success"
        assert second["output_path"] != first["output_path"]  # old asset untouched
        assert Path(first["output_path"]).is_file()


class TestRecoveryAndPersistence:
    def test_interrupted_running_task_is_recovered(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        task_id = plan.created[0]["task_id"]
        # Simulate a crash: task left in running with no live worker.
        conn = db.conn()
        with transaction(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE task_id=?", (task_id,))

        recovered = service.recover_interrupted()
        assert recovered == 1
        task = service.get_task(task_id, with_details=False)
        assert task["status"] == "retrying"
        assert task["error_code"] == "interrupted"

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        assert service.get_task(task_id, with_details=False)["status"] == "success"

    def test_data_survives_reopen(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        snapshot = service.get_task(service.list_tasks()[0]["task_id"])
        db.close_all()

        # Fresh connections against the same files = service restart.
        db2 = Database(config.db_path)
        migrate(db2.conn())
        service2 = TaskService(db2, config)
        try:
            restored = service2.get_task(snapshot["task_id"])
            assert restored["status"] == "success"
            assert restored["prompt"] == snapshot["prompt"]
            assert restored["qa"] is not None
            assert Path(restored["output_path"]).is_file()
            assert service2.get_style(style["style_id"])["sku"] == style["sku"]
        finally:
            db2.close_all()


class TestCancel:
    def test_cancel_pending_then_requeue(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        task_id = plan.created[0]["task_id"]
        assert service.cancel(task_id)["status"] == "cancelled"
        assert service.manual_retry(task_id)["status"] == "pending"

    def test_cancel_success_refused(self, config, db, service):
        style = make_style(service)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        with pytest.raises(ConflictError):
            service.cancel(service.list_tasks()[0]["task_id"])
