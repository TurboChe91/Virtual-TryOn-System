"""Spend accounting and the budget circuit breaker."""

from __future__ import annotations

import pytest

from lunelle.budget import (
    BudgetExceeded,
    check_can_queue,
    check_can_spend,
    estimate_plan,
    spend_snapshot,
)
from lunelle.db import transaction, utcnow
from tests.conftest import make_config


def _ensure_style(db) -> str:
    """tasks.style_id is a real foreign key, so a style must exist first."""
    conn = db.conn()
    row = conn.execute("SELECT style_id FROM styles LIMIT 1").fetchone()
    if row is not None:
        return row["style_id"]
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO styles (style_id, sku, name, spec_json, source_type,"
            " source_input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("st_budget", "sku-budget", "Budget Fixture", "{}", "structured", "{}",
             now, now),
        )
    return "st_budget"


def _insert_task(db, *, status, estimated=0.05, actual=None, completed=None):
    conn = db.conn()
    from lunelle.models import new_task_id
    style_id = _ensure_style(db)
    task_id = new_task_id()
    now = utcnow()
    with transaction(conn):
        conn.execute(
            "INSERT INTO tasks (task_id, style_id, sku, output_type, prompt,"
            " prompt_version, provider, model, status, estimated_cost_usd,"
            " actual_cost_usd, created_at, updated_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, style_id, "sku-budget", "grid", "p", "v", "mock", "m", status,
             estimated, actual, now, now,
             completed or (now if status == "success" else None)),
        )
    return task_id


class TestEstimate:
    def test_prices_a_batch(self):
        config = make_config_for_estimate()
        out = estimate_plan(config, image_count=16, price_per_image=0.04)
        assert out["image_count"] == 16
        assert out["estimated_usd"] == pytest.approx(0.64)

    def test_worst_case_covers_retries_and_regeneration(self):
        """The figure an operator approves must be the most they can be charged."""
        config = make_config_for_estimate(max_retries=2, auto_regen_max=1)
        out = estimate_plan(config, image_count=10, price_per_image=0.10)
        # (1 + 1 regen) * (1 + 2 retries) = 6x
        assert out["worst_case_usd"] == pytest.approx(6.0)
        assert out["estimated_usd"] == pytest.approx(1.0)

    def test_confirmation_required_above_threshold(self):
        config = make_config_for_estimate(confirm_cost_usd=0.5)
        assert estimate_plan(config, image_count=16, price_per_image=0.04)["requires_confirmation"]
        assert not estimate_plan(config, image_count=1, price_per_image=0.04)["requires_confirmation"]

    def test_zero_threshold_never_requires_confirmation(self):
        config = make_config_for_estimate(confirm_cost_usd=0.0)
        out = estimate_plan(config, image_count=1000, price_per_image=1.0)
        assert out["requires_confirmation"] is False


def make_config_for_estimate(**overrides):
    import tempfile
    from pathlib import Path
    return make_config(Path(tempfile.mkdtemp()), **overrides)


class TestSnapshot:
    def test_empty_database_has_no_spend(self, db, config):
        snapshot = spend_snapshot(db, config)
        assert snapshot.realized_usd == 0.0
        assert snapshot.committed_usd == 0.0
        assert snapshot.enabled is False  # test config has no cap

    def test_successful_tasks_count_as_realized(self, db, config):
        _insert_task(db, status="success", estimated=0.05, actual=0.06)
        _insert_task(db, status="success", estimated=0.05, actual=None)
        snapshot = spend_snapshot(db, config)
        # actual preferred where present, estimate as fallback
        assert snapshot.realized_usd == pytest.approx(0.11)
        assert snapshot.images_generated == 2

    def test_active_tasks_count_as_committed_not_realized(self, db, config):
        for status in ("pending", "running", "retrying"):
            _insert_task(db, status=status, estimated=0.05)
        snapshot = spend_snapshot(db, config)
        assert snapshot.realized_usd == 0.0
        assert snapshot.committed_usd == pytest.approx(0.15)
        assert snapshot.total_usd == pytest.approx(0.15)

    def test_failed_task_without_billed_attempt_costs_nothing(self, db, config):
        _insert_task(db, status="failed", estimated=0.05)
        assert spend_snapshot(db, config).total_usd == 0.0

    def test_billed_failed_attempt_counts(self, db, config):
        """A provider can charge for a call whose download then failed."""
        task_id = _insert_task(db, status="failed", estimated=0.05)
        conn = db.conn()
        with transaction(conn):
            conn.execute(
                "INSERT INTO attempts (task_id, attempt_no, provider, model,"
                " started_at, outcome, cost_usd) VALUES (?,?,?,?,?,?,?)",
                (task_id, 1, "mock", "m", utcnow(), "error", 0.04),
            )
        assert spend_snapshot(db, config).realized_usd == pytest.approx(0.04)

    def test_spend_outside_the_window_is_excluded(self, db, config):
        _insert_task(db, status="success", estimated=0.05, actual=0.05,
                     completed="2020-01-01T00:00:00+0000")
        assert spend_snapshot(db, config).realized_usd == 0.0

    def test_remaining_is_infinite_without_a_cap(self, db, config):
        assert spend_snapshot(db, config).remaining_usd == float("inf")
        assert spend_snapshot(db, config).as_dict()["remaining_usd"] is None


class TestQueueBreaker:
    def test_disabled_breaker_allows_anything(self, db, config):
        check_can_queue(db, config, additional_usd=10_000)  # no raise

    def test_allows_a_batch_inside_the_cap(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=1.0)
        check_can_queue(db, config, additional_usd=0.5)

    def test_refuses_a_batch_over_the_cap(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=1.0)
        with pytest.raises(BudgetExceeded, match="budget_exceeded"):
            check_can_queue(db, config, additional_usd=1.5)

    def test_already_queued_work_counts_against_the_cap(self, db, tmp_path):
        """Otherwise many separate batches could collectively overrun it."""
        config = make_config(tmp_path, daily_budget_usd=1.0)
        for _ in range(10):
            _insert_task(db, status="pending", estimated=0.09)  # 0.90 committed
        with pytest.raises(BudgetExceeded, match="already queued"):
            check_can_queue(db, config, additional_usd=0.2)

    def test_error_carries_the_snapshot(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=1.0)
        with pytest.raises(BudgetExceeded) as excinfo:
            check_can_queue(db, config, additional_usd=5.0)
        assert excinfo.value.snapshot.limit_usd == 1.0
        assert excinfo.value.snapshot.as_dict()["remaining_usd"] == 1.0


class TestWorkerBreaker:
    def test_excludes_in_flight_work(self, db, tmp_path):
        """A batch must not trip the breaker on the strength of its own queue —
        that would deadlock any batch larger than the remaining budget."""
        config = make_config(tmp_path, daily_budget_usd=1.0)
        for _ in range(15):
            _insert_task(db, status="pending", estimated=0.06)  # 0.90 committed
        # Queue-time check would refuse; the worker check must not.
        with pytest.raises(BudgetExceeded):
            check_can_queue(db, config, additional_usd=0.2)
        check_can_spend(db, config, task_cost_usd=0.06)  # no raise

    def test_refuses_once_realized_spend_fills_the_cap(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=0.20)
        for _ in range(4):
            _insert_task(db, status="success", estimated=0.05, actual=0.05)
        with pytest.raises(BudgetExceeded, match="already spent"):
            check_can_spend(db, config, task_cost_usd=0.05)

    def test_allows_the_task_that_exactly_fits(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=0.20)
        _insert_task(db, status="success", estimated=0.15, actual=0.15)
        check_can_spend(db, config, task_cost_usd=0.05)  # 0.20 total, at the cap
