"""Spend accounting and the budget circuit breaker."""

from __future__ import annotations

import pytest

from lunelle.budget import (
    BudgetExceeded,
    LineageBudgetExceeded,
    check_can_queue,
    claim_lineage_descendant,
    estimate_plan,
    lineage_status,
    open_lineage_locked,
    recover_orphan_reservations,
    release_spend,
    reserve_spend,
    settle_spend,
    spend_snapshot,
    sync_lineage_spend,
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

    def test_worst_case_covers_retries_and_the_lineage_budget(self):
        """The figure an operator approves must be the most they can be charged."""
        config = make_config_for_estimate(max_retries=2, max_lineage_descendants=1)
        out = estimate_plan(config, image_count=10, price_per_image=0.10)
        # (1 + 2 retries) attempts x (1 + 1 lineage descendant) generations = 6x
        assert out["worst_case_usd"] == pytest.approx(6.0)
        assert out["estimated_usd"] == pytest.approx(1.0)

    def test_confirm_max_usd_is_the_worst_case_not_the_estimate(self):
        """Authorizing the expected cost while exposed to the worst case is not
        consent — the ceiling is what a caller must echo back."""
        config = make_config_for_estimate(max_retries=2, max_lineage_descendants=2)
        out = estimate_plan(config, image_count=16, price_per_image=0.04)
        assert out["confirm_max_usd"] == out["worst_case_usd"]
        assert out["confirm_max_usd"] > out["estimated_usd"]

    def test_confirmation_gated_on_worst_case(self):
        """A batch whose expected cost is under the threshold but whose ceiling is
        over it must still prompt."""
        config = make_config_for_estimate(confirm_cost_usd=1.0, max_retries=2,
                                          max_lineage_descendants=2)
        out = estimate_plan(config, image_count=16, price_per_image=0.04)
        assert out["estimated_usd"] == pytest.approx(0.64)   # under the threshold
        assert out["worst_case_usd"] == pytest.approx(5.76)  # over it
        assert out["requires_confirmation"] is True

    def test_single_cheap_image_needs_no_confirmation(self):
        config = make_config_for_estimate(confirm_cost_usd=1.0, max_retries=2,
                                          max_lineage_descendants=2)
        out = estimate_plan(config, image_count=1, price_per_image=0.04)
        assert out["worst_case_usd"] == pytest.approx(0.36)
        assert out["requires_confirmation"] is False

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

    def test_settled_reservations_are_realized(self, db, config):
        first = _insert_task(db, status="success", estimated=0.05)
        second = _insert_task(db, status="success", estimated=0.05)
        reserve_spend(db, config, task_id=first, attempt_no=1,
                      estimated_usd=0.05, root_task_id=first)
        settle_spend(db, task_id=first, attempt_no=1, actual_usd=0.06)
        reserve_spend(db, config, task_id=second, attempt_no=1,
                      estimated_usd=0.05, root_task_id=second)
        # No reported cost: the estimate stands, because the money was still spent.
        settle_spend(db, task_id=second, attempt_no=1, actual_usd=None)
        snapshot = spend_snapshot(db, config)
        assert snapshot.realized_usd == pytest.approx(0.11)
        assert snapshot.reserved_usd == 0.0
        assert snapshot.images_generated == 2

    def test_in_flight_reservations_are_counted(self, db, config):
        task_id = _insert_task(db, status="running", estimated=0.07)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.07, root_task_id=task_id)
        snapshot = spend_snapshot(db, config)
        assert snapshot.reserved_usd == pytest.approx(0.07)
        assert snapshot.spent_usd == pytest.approx(0.07)

    def test_reserved_task_is_not_also_counted_as_committed(self, db, config):
        """Double counting would make the breaker trip early."""
        task_id = _insert_task(db, status="running", estimated=0.07)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.07, root_task_id=task_id)
        snapshot = spend_snapshot(db, config)
        assert snapshot.committed_usd == 0.0
        assert snapshot.total_usd == pytest.approx(0.07)

    def test_queued_work_counts_as_committed_not_realized(self, db, config):
        for status in ("pending", "running", "retrying"):
            _insert_task(db, status=status, estimated=0.05)
        snapshot = spend_snapshot(db, config)
        assert snapshot.realized_usd == 0.0
        assert snapshot.committed_usd == pytest.approx(0.15)
        assert snapshot.total_usd == pytest.approx(0.15)

    def test_released_reservation_costs_nothing(self, db, config):
        task_id = _insert_task(db, status="failed", estimated=0.05)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.05, root_task_id=task_id)
        release_spend(db, task_id=task_id, attempt_no=1)
        snapshot = spend_snapshot(db, config)
        assert snapshot.realized_usd == 0.0
        assert snapshot.reserved_usd == 0.0

    def test_failure_with_a_known_charge_is_settled(self, db, config):
        """A provider that billed before erroring must still be accounted for."""
        task_id = _insert_task(db, status="failed", estimated=0.05)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.05, root_task_id=task_id)
        release_spend(db, task_id=task_id, attempt_no=1, known_cost_usd=0.04)
        assert spend_snapshot(db, config).realized_usd == pytest.approx(0.04)

    def test_spend_outside_the_window_is_excluded(self, db, config):
        task_id = _insert_task(db, status="success", estimated=0.05)
        conn = db.conn()
        with transaction(conn):
            conn.execute(
                "INSERT INTO spend_reservations (task_id, attempt_no, root_task_id,"
                " estimated_usd, actual_usd, state, created_at, settled_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (task_id, 1, task_id, 0.05, 0.05, "settled",
                 "2020-01-01T00:00:00+0000", "2020-01-01T00:00:00+0000"),
            )
        assert spend_snapshot(db, config).realized_usd == 0.0

    def test_remaining_is_infinite_without_a_cap(self, db, config):
        assert spend_snapshot(db, config).remaining_usd == float("inf")
        assert spend_snapshot(db, config).as_dict()["remaining_usd"] is None

    def test_orphan_reservation_recovery_settles_at_estimate(self, db, config):
        """A crash between reserve and settle must not claim budget forever."""
        task_id = _insert_task(db, status="failed", estimated=0.05)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.05, root_task_id=task_id)
        assert spend_snapshot(db, config).reserved_usd == pytest.approx(0.05)
        assert recover_orphan_reservations(db) == 1
        snapshot = spend_snapshot(db, config)
        assert snapshot.reserved_usd == 0.0
        # Settled, not released: the call may well have reached the provider.
        assert snapshot.realized_usd == pytest.approx(0.05)

    def test_recovery_leaves_live_running_tasks_alone(self, db, config):
        task_id = _insert_task(db, status="running", estimated=0.05)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.05, root_task_id=task_id)
        assert recover_orphan_reservations(db) == 0
        assert spend_snapshot(db, config).reserved_usd == pytest.approx(0.05)


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


class TestReservationBreaker:
    def test_excludes_queued_work(self, db, tmp_path):
        """A batch must not trip the breaker on the strength of its own queue —
        that would deadlock any batch larger than the remaining budget."""
        config = make_config(tmp_path, daily_budget_usd=1.0)
        for _ in range(15):
            _insert_task(db, status="pending", estimated=0.06)  # 0.90 committed
        # Queue-time authorization refuses; reserving the next call must not.
        with pytest.raises(BudgetExceeded):
            check_can_queue(db, config, additional_usd=0.2)
        task_id = _insert_task(db, status="running", estimated=0.06)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.06, root_task_id=task_id)

    def test_refuses_once_spend_fills_the_cap(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=0.20)
        for _ in range(4):
            task_id = _insert_task(db, status="success", estimated=0.05)
            reserve_spend(db, config, task_id=task_id, attempt_no=1,
                          estimated_usd=0.05, root_task_id=task_id)
            settle_spend(db, task_id=task_id, attempt_no=1, actual_usd=0.05)
        blocked = _insert_task(db, status="running", estimated=0.05)
        with pytest.raises(BudgetExceeded, match="budget_exceeded"):
            reserve_spend(db, config, task_id=blocked, attempt_no=1,
                          estimated_usd=0.05, root_task_id=blocked)

    def test_in_flight_reservations_block_a_new_one(self, db, tmp_path):
        """The whole point of reserving: two workers cannot both pass the check.
        Worker A's live reservation is visible to worker B."""
        config = make_config(tmp_path, daily_budget_usd=0.10)
        first = _insert_task(db, status="running", estimated=0.08)
        reserve_spend(db, config, task_id=first, attempt_no=1,
                      estimated_usd=0.08, root_task_id=first)
        second = _insert_task(db, status="running", estimated=0.08)
        with pytest.raises(BudgetExceeded, match="in flight"):
            reserve_spend(db, config, task_id=second, attempt_no=1,
                          estimated_usd=0.08, root_task_id=second)

    def test_allows_the_call_that_exactly_fits(self, db, tmp_path):
        config = make_config(tmp_path, daily_budget_usd=0.20)
        first = _insert_task(db, status="success", estimated=0.15)
        reserve_spend(db, config, task_id=first, attempt_no=1,
                      estimated_usd=0.15, root_task_id=first)
        settle_spend(db, task_id=first, attempt_no=1, actual_usd=0.15)
        second = _insert_task(db, status="running", estimated=0.05)
        reserve_spend(db, config, task_id=second, attempt_no=1,
                      estimated_usd=0.05, root_task_id=second)  # exactly at the cap

    def test_float_summation_does_not_refuse_a_call_that_fits(self, db, tmp_path):
        """0.05 + 0.05 + 0.05 == 0.15000000000000002 in binary floating point, so
        a naive `> limit` refused the third call against a $0.15 cap. Money is
        compared with a tolerance; this is the boundary that caught it."""
        config = make_config(tmp_path, daily_budget_usd=0.15)
        for index in range(3):
            task_id = _insert_task(db, status="running", estimated=0.05)
            reserve_spend(db, config, task_id=task_id, attempt_no=1,
                          estimated_usd=0.05, root_task_id=task_id)
            settle_spend(db, task_id=task_id, attempt_no=1, actual_usd=0.05)
            assert index >= 0  # all three must be granted
        # The fourth is genuinely over and must be refused.
        extra = _insert_task(db, status="running", estimated=0.05)
        with pytest.raises(BudgetExceeded):
            reserve_spend(db, config, task_id=extra, attempt_no=1,
                          estimated_usd=0.05, root_task_id=extra)

    def test_retried_attempt_replaces_its_released_reservation(self, db, tmp_path):
        """A retry reuses the attempt number after a release; reserving again must
        not fail on the unique constraint."""
        config = make_config(tmp_path, daily_budget_usd=1.0)
        task_id = _insert_task(db, status="running", estimated=0.05)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.05, root_task_id=task_id)
        release_spend(db, task_id=task_id, attempt_no=1)
        reserve_spend(db, config, task_id=task_id, attempt_no=1,
                      estimated_usd=0.05, root_task_id=task_id)
        assert spend_snapshot(db, config).reserved_usd == pytest.approx(0.05)


class TestLineageBudget:
    """One shared allowance for auto-regeneration AND auto-correction.

    Before this, each path tracked its own depth in task metadata and neither
    copied the other's counter to the child it created, so a task could alternate
    regen -> correct -> regen forever and the advertised worst case was not a bound.
    """

    def _root(self, db, config, *, price=0.05):
        root = _insert_task(db, status="success", estimated=price)
        conn = db.conn()
        with transaction(conn):
            open_lineage_locked(conn, config, root_task_id=root, style_id="st_budget",
                                output_type="grid", price_per_image=price)
        return root

    def test_regen_and_correct_share_one_count(self, db, tmp_path):
        config = make_config(tmp_path, max_lineage_descendants=2)
        root = self._root(db, config)
        claim_lineage_descendant(db, root_task_id=root, estimated_usd=0.0,
                                kind="regeneration")
        claim_lineage_descendant(db, root_task_id=root, estimated_usd=0.0,
                                kind="correction")
        # Two descendants used, regardless of which path used them.
        with pytest.raises(LineageBudgetExceeded, match="lineage_budget_exhausted"):
            claim_lineage_descendant(db, root_task_id=root, estimated_usd=0.0,
                                    kind="regeneration")

    def test_alternating_paths_cannot_reset_each_other(self, db, tmp_path):
        """The exact defect: alternate the two kinds and the budget still binds."""
        config = make_config(tmp_path, max_lineage_descendants=3)
        root = self._root(db, config)
        kinds = ["regeneration", "correction", "regeneration", "correction",
                 "regeneration", "correction"]
        granted = 0
        for kind in kinds:
            try:
                claim_lineage_descendant(db, root_task_id=root, estimated_usd=0.0,
                                        kind=kind)
                granted += 1
            except LineageBudgetExceeded:
                break
        assert granted == 3, "alternating kinds must not extend the shared budget"

    def test_dollar_ceiling_also_binds(self, db, tmp_path):
        config = make_config(tmp_path, max_lineage_descendants=100, max_retries=0)
        root = self._root(db, config, price=0.05)
        status = lineage_status(db, root)
        # max_descendants is large, so only the dollar cap can stop this.
        assert status["lineage_max_usd"] == pytest.approx(0.05 * 101)
        with pytest.raises(LineageBudgetExceeded, match="would exceed"):
            claim_lineage_descendant(db, root_task_id=root, estimated_usd=999.0,
                                    kind="regeneration")

    def test_untracked_root_is_refused(self, db, tmp_path):
        """Automatic work that cannot be bounded must not run at all."""
        config = make_config(tmp_path)
        orphan = _insert_task(db, status="success", estimated=0.05)
        with pytest.raises(LineageBudgetExceeded, match="lineage_untracked"):
            claim_lineage_descendant(db, root_task_id=orphan, estimated_usd=0.0,
                                    kind="regeneration")
        assert config is not None  # config unused beyond construction

    def test_status_reports_shared_spend(self, db, tmp_path):
        config = make_config(tmp_path, max_lineage_descendants=2)
        root = self._root(db, config)
        reserve_spend(db, config, task_id=root, attempt_no=1,
                      estimated_usd=0.05, root_task_id=root)
        settle_spend(db, task_id=root, attempt_no=1, actual_usd=0.05)
        sync_lineage_spend(db, root)
        status = lineage_status(db, root)
        assert status["lineage_spent_usd"] == pytest.approx(0.05)
        assert status["provider_calls"] == 1
        assert status["descendant_count"] == 0
        assert status["exhausted"] is False

    def test_zero_descendants_disables_automatic_work(self, db, tmp_path):
        config = make_config(tmp_path, max_lineage_descendants=0)
        root = self._root(db, config)
        with pytest.raises(LineageBudgetExceeded):
            claim_lineage_descendant(db, root_task_id=root, estimated_usd=0.0,
                                    kind="regeneration")
