"""Concurrency tests for the spend ledger and the lineage budget.

The point of reserving spend inside a transaction is that a plain
read-then-spend check lets N worker threads all observe "under budget" and all
spend. These tests run real threads against a real SQLite database and assert the
cap holds, which a single-threaded test cannot show.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from lunelle.budget import (
    BudgetExceeded,
    LineageBudgetExceeded,
    claim_lineage_descendant,
    open_lineage_locked,
    reserve_spend,
    settle_spend,
    spend_snapshot,
)
from lunelle.db import transaction, utcnow
from lunelle.models import new_task_id
from lunelle.providers.mock import MockImageProvider
from lunelle.server import create_app
from lunelle.worker import Worker
from tests.conftest import make_config, satisfy_matrix_dependencies


def _style(db) -> str:
    conn = db.conn()
    row = conn.execute("SELECT style_id FROM styles LIMIT 1").fetchone()
    if row:
        return row["style_id"]
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO styles (style_id, sku, name, spec_json, source_type,"
            " source_input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("st_conc", "sku-conc", "Concurrency", "{}", "structured", "{}", now, now),
        )
    return "st_conc"


def _task(db, *, estimated: float, status: str = "running") -> str:
    style_id = _style(db)
    task_id = new_task_id()
    now = utcnow()
    conn = db.conn()
    with transaction(conn):
        conn.execute(
            "INSERT INTO tasks (task_id, style_id, sku, output_type, prompt,"
            " prompt_version, provider, model, status, estimated_cost_usd,"
            " created_at, updated_at, root_task_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, style_id, "sku-conc", "grid", "p", "v", "mock", "m", status,
             estimated, now, now, task_id),
        )
    return task_id


class TestReservationIsAtomic:
    def test_parallel_reservations_never_exceed_the_cap(self, db, tmp_path):
        """20 threads race for a budget that fits exactly 5 calls."""
        config = make_config(tmp_path, daily_budget_usd=0.50)
        per_call = 0.10
        task_ids = [_task(db, estimated=per_call) for _ in range(20)]

        granted: list[str] = []
        refused: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(len(task_ids))

        def attempt(task_id: str) -> None:
            start.wait(timeout=10)
            try:
                reserve_spend(db, config, task_id=task_id, attempt_no=1,
                              estimated_usd=per_call, root_task_id=task_id)
            except BudgetExceeded:
                with lock:
                    refused.append(task_id)
            else:
                with lock:
                    granted.append(task_id)

        threads = [threading.Thread(target=attempt, args=(t,)) for t in task_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        # Exactly the number the cap affords — no more, and not zero.
        assert len(granted) == 5, f"granted {len(granted)}, refused {len(refused)}"
        assert len(refused) == 15
        snapshot = spend_snapshot(db, config)
        assert snapshot.reserved_usd == pytest.approx(0.50)
        assert snapshot.spent_usd <= snapshot.limit_usd

    def test_settlement_frees_headroom_for_the_next_call(self, db, tmp_path):
        """A cheaper actual cost than estimated must release the difference."""
        config = make_config(tmp_path, daily_budget_usd=0.10)
        first = _task(db, estimated=0.10)
        reserve_spend(db, config, task_id=first, attempt_no=1,
                      estimated_usd=0.10, root_task_id=first)
        settle_spend(db, task_id=first, attempt_no=1, actual_usd=0.02)
        second = _task(db, estimated=0.05)
        reserve_spend(db, config, task_id=second, attempt_no=1,
                      estimated_usd=0.05, root_task_id=second)
        assert spend_snapshot(db, config).spent_usd == pytest.approx(0.07)


class TestLineageClaimIsAtomic:
    def test_parallel_claims_never_exceed_the_shared_allowance(self, db, tmp_path):
        """12 threads race for 3 descendant slots, mixing both automatic kinds."""
        config = make_config(tmp_path, max_lineage_descendants=3)
        root = _task(db, estimated=0.05, status="success")
        conn = db.conn()
        with transaction(conn):
            open_lineage_locked(conn, config, root_task_id=root,
                                style_id=_style(db), output_type="grid",
                                price_per_image=0.05)

        granted: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(12)

        def attempt(index: int) -> None:
            kind = "regeneration" if index % 2 == 0 else "correction"
            start.wait(timeout=10)
            try:
                claim_lineage_descendant(db, root_task_id=root, estimated_usd=0.0,
                                        kind=kind)
            except LineageBudgetExceeded:
                return
            with lock:
                granted.append(kind)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        assert len(granted) == 3, f"granted {granted}"
        row = db.conn().execute(
            "SELECT descendant_count FROM generation_lineages WHERE root_task_id = ?",
            (root,),
        ).fetchone()
        assert row["descendant_count"] == 3


class TestQueueAuthorizationIsAtomic:
    def test_parallel_matrix_submissions_do_not_overrun_the_cap(self, tmp_path):
        """Queue authorization runs inside the write transaction, so two callers
        cannot both pass the check and then both insert."""
        config = make_config(tmp_path, daily_budget_usd=0.30, confirm_cost_usd=0.0)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style_ids = []
            for index in range(6):
                style = client.post("/api/styles", json={
                    "name": f"Race {index}",
                    "description": f"style {index} red almond glossy nails",
                }).json()["style"]
                satisfy_matrix_dependencies(app.state.db, config,
                                            app.state.service, style["style_id"])
                style_ids.append(style["style_id"])

            accepted: list[int] = []
            lock = threading.Lock()
            start = threading.Barrier(len(style_ids))

            def submit(style_id: str) -> None:
                start.wait(timeout=10)
                response = client.post(
                    f"/api/styles/{style_id}/matrix",
                    json={"tones": ["light"], "views": ["p2_open_hands"]})
                with lock:
                    accepted.append(response.status_code)

            threads = [threading.Thread(target=submit, args=(s,)) for s in style_ids]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
                assert not thread.is_alive()

            queued = [code for code in accepted if code == 202]
            refused = [code for code in accepted if code == 429]
            assert len(queued) + len(refused) == len(style_ids), accepted
            # 0.30 cap at 0.05/image affords 6 cells, so all six fit; the point is
            # that the committed total never exceeds the cap.
            budget = client.get("/api/budget").json()
            assert budget["total_usd"] <= budget["limit_usd"] + 1e-9, budget

    def test_tight_cap_refuses_the_excess_submissions(self, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=0.10, confirm_cost_usd=0.0)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style_ids = []
            for index in range(8):
                style = client.post("/api/styles", json={
                    "name": f"Tight {index}",
                    "description": f"style {index} teal coffin matte nails",
                }).json()["style"]
                satisfy_matrix_dependencies(app.state.db, config,
                                            app.state.service, style["style_id"])
                style_ids.append(style["style_id"])

            codes: list[int] = []
            lock = threading.Lock()
            start = threading.Barrier(len(style_ids))

            def submit(style_id: str) -> None:
                start.wait(timeout=10)
                response = client.post(
                    f"/api/styles/{style_id}/matrix",
                    json={"tones": ["light"], "views": ["p2_open_hands"]})
                with lock:
                    codes.append(response.status_code)

            threads = [threading.Thread(target=submit, args=(s,)) for s in style_ids]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
                assert not thread.is_alive()

            assert sorted(set(codes)) in ([202], [202, 429]), codes
            # 0.10 / 0.05 = 2 cells affordable; the rest must be refused.
            assert codes.count(202) == 2, codes
            budget = client.get("/api/budget").json()
            assert budget["total_usd"] <= budget["limit_usd"] + 1e-9, budget


class TestWorkerConcurrencyRespectsTheCap:
    def test_multiple_worker_threads_share_one_budget(self, tmp_path):
        """The scenario the reservation exists for: several workers, one cap.

        Authorize 4 cells under a generous cap, then tighten it to afford only 3
        and run 4 worker threads. Without an atomic reservation, several threads
        would each read "under budget" and each spend; with it, the number of paid
        calls matches the cap exactly.
        """
        config = make_config(tmp_path, daily_budget_usd=5.0, confirm_cost_usd=0.0,
                             max_concurrency=4, max_retries=0, auto_regen_max=0)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Worker Race",
                "description": "ivory oval glossy nails",
            }).json()["style"]
            satisfy_matrix_dependencies(app.state.db, config, app.state.service,
                                        style["style_id"])
            queued = client.post(f"/api/styles/{style['style_id']}/matrix",
                                 json={"tones": ["light", "medium", "tan", "deep"],
                                       "views": ["p2_open_hands"]})
            assert queued.status_code == 202
            assert len(queued.json()["created"]) == 4  # 0.20 of work

            # Budget tightened after authorization: affords 3 of the 4 calls.
            tight = make_config(tmp_path / "tight", daily_budget_usd=0.15,
                                max_concurrency=4, max_retries=0, auto_regen_max=0)
            provider = MockImageProvider(allowed=True)
            worker = Worker(tight, app.state.db, app.state.service, provider)
            worker.start()
            try:
                import time
                deadline = time.time() + 40
                while time.time() < deadline:
                    tasks = client.get("/api/tasks").json()["tasks"]
                    if tasks and all(t["status"] in ("success", "failed")
                                     for t in tasks):
                        break
                    time.sleep(0.1)
            finally:
                worker.stop()

            tasks = client.get("/api/tasks").json()["tasks"]
            succeeded = [t for t in tasks if t["status"] == "success"]
            budget_failed = [t for t in tasks
                             if t["error_code"] == "budget_exceeded"]
            # 0.15 at 0.05/image affords exactly 3 calls.
            assert provider.calls == 3, (
                f"provider made {provider.calls} calls; cap affords 3")
            assert len(succeeded) == 3
            assert len(budget_failed) == 1
            snapshot = spend_snapshot(app.state.db, tight)
            assert snapshot.spent_usd <= snapshot.limit_usd + 1e-9
