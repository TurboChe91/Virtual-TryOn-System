"""QA crash recovery.

A crash between `complete_success` and `finish_qa` leaves a task at
qa_state pending/running. The publish/export gate correctly refuses those (no
verdict exists), but nothing would ever move them forward, so the asset was
stranded. Recovery re-runs QA locally — free, no provider call — or marks the
asset unjudgeable when its output file is gone.
"""

from __future__ import annotations

from pathlib import Path

from lunelle.providers.mock import MockImageProvider
from lunelle.worker import Worker
from tests.integration.test_worker_flows import make_style, run_worker_until_settled


def _settled_task(config, db, service, provider=None):
    style = make_style(service)
    plan = service.create_generation(style["style_id"], ["grid"])
    run_worker_until_settled(config, db, service,
                             provider or MockImageProvider(allowed=True))
    return service.get_task(plan.created[0]["task_id"])


def _force_qa_state(db, task_id: str, qa_state: str, review_state: str = "generated"):
    from lunelle.db import transaction
    conn = db.conn()
    with transaction(conn):
        conn.execute("DELETE FROM asset_reviews WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM qa_results WHERE task_id = ?", (task_id,))
        conn.execute(
            "UPDATE tasks SET qa_state = ?, review_state = ? WHERE task_id = ?",
            (qa_state, review_state, task_id),
        )


class TestQaRecovery:
    def test_pending_qa_is_re_run_when_the_output_exists(self, config, db, service):
        task = _settled_task(config, db, service)
        assert task["status"] == "success"
        _force_qa_state(db, task["task_id"], "pending")

        provider = MockImageProvider(allowed=True)
        worker = Worker(config, db, service, provider)
        summary = worker.recover_interrupted_qa()

        assert summary == {"examined": 1, "requeued": 1, "missing_output": 0,
                           "failed": 0}
        recovered = service.get_task(task["task_id"])
        assert recovered["qa_state"] == "done"
        assert recovered["review_state"] == "waiting_human_review"
        assert recovered["qa"] is not None
        # Recovery is local analysis only: no paid call.
        assert provider.calls == 0

    def test_running_qa_is_also_recovered(self, config, db, service):
        """A crash mid-QA leaves `running`, not just `pending`."""
        task = _settled_task(config, db, service)
        _force_qa_state(db, task["task_id"], "running")
        worker = Worker(config, db, service, MockImageProvider(allowed=True))
        summary = worker.recover_interrupted_qa()
        assert summary["requeued"] == 1
        assert service.get_task(task["task_id"])["qa_state"] == "done"

    def test_missing_output_becomes_qa_error(self, config, db, service):
        """An asset whose file is gone cannot be judged, so it must stay blocked."""
        task = _settled_task(config, db, service)
        _force_qa_state(db, task["task_id"], "pending")
        Path(task["output_path"]).unlink()

        worker = Worker(config, db, service, MockImageProvider(allowed=True))
        summary = worker.recover_interrupted_qa()

        assert summary == {"examined": 1, "requeued": 0, "missing_output": 1,
                           "failed": 0}
        recovered = service.get_task(task["task_id"])
        assert recovered["qa_state"] == "error"
        assert recovered["qa"] is None

    def test_recovered_error_asset_stays_ungated(self, config, db, service):
        """qa_state=error must keep failing the publish/export gate."""
        from lunelle.export import ExportError, run_export

        task = _settled_task(config, db, service)
        _force_qa_state(db, task["task_id"], "pending")
        Path(task["output_path"]).unlink()
        Worker(config, db, service, MockImageProvider(allowed=True)) \
            .recover_interrupted_qa()

        try:
            run_export(db, config)
        except ExportError as exc:
            assert "review gate" in str(exc) or "missing" in str(exc)
        else:
            raise AssertionError("export must refuse an unjudgeable asset")

    def test_null_output_path_becomes_qa_error(self, config, db, service):
        from lunelle.db import transaction

        task = _settled_task(config, db, service)
        _force_qa_state(db, task["task_id"], "pending")
        conn = db.conn()
        with transaction(conn):
            conn.execute("UPDATE tasks SET output_path = NULL WHERE task_id = ?",
                         (task["task_id"],))
        worker = Worker(config, db, service, MockImageProvider(allowed=True))
        assert worker.recover_interrupted_qa()["missing_output"] == 1
        assert service.get_task(task["task_id"])["qa_state"] == "error"

    def test_recovery_ignores_tasks_that_already_finished_qa(self, config, db, service):
        task = _settled_task(config, db, service)
        assert service.get_task(task["task_id"])["qa_state"] == "done"
        worker = Worker(config, db, service, MockImageProvider(allowed=True))
        assert worker.recover_interrupted_qa()["examined"] == 0

    def test_recovery_runs_on_worker_start(self, config, db, service):
        """It has to be automatic; an operator will not know to trigger it."""
        task = _settled_task(config, db, service)
        _force_qa_state(db, task["task_id"], "pending")
        worker = Worker(config, db, service, MockImageProvider(allowed=True))
        worker.start()
        try:
            recovered = service.get_task(task["task_id"])
            assert recovered["qa_state"] == "done"
        finally:
            worker.stop()

    def test_recovery_reopens_review_rather_than_preserving_approval(
        self, config, db, service
    ):
        """An approval given against a verdict that no longer exists must not
        survive a re-run."""
        task = _settled_task(config, db, service)
        service.record_review(task["task_id"], approved=True, note="before crash")
        assert service.get_task(task["task_id"])["review_state"] == "approved"

        _force_qa_state(db, task["task_id"], "pending", review_state="approved")
        Worker(config, db, service, MockImageProvider(allowed=True)) \
            .recover_interrupted_qa()
        recovered = service.get_task(task["task_id"])
        assert recovered["qa_state"] == "done"
        assert recovered["review_state"] == "waiting_human_review"
        assert recovered["reviewed_at"] is None
