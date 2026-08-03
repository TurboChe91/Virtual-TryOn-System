"""The publish/export gate: default-deny, and SQL == Python.

The gate is written twice (a SQL fragment for queries, a Python predicate for
per-row decisions). That duplication is the risk, so these tests drive both with
the same rows and assert they never disagree.
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from lunelle.gating import GATE_SQL, block_reason, row_is_publishable
from lunelle.models import (
    FAILED,
    PUBLISHABLE_REVIEW_STATES,
    QA_STATES,
    REVIEW_STATES,
    SUCCESS,
)


def _approved_row(**overrides) -> dict:
    row = {"status": SUCCESS, "qa_state": "done", "review_state": "approved",
           "qa_id": 1, "qa_passed": 1, "qa_needs_review": 0}
    row.update(overrides)
    return row


class TestDefaultDeny:
    def test_fully_approved_row_passes(self):
        assert row_is_publishable(_approved_row()) is True
        assert block_reason(_approved_row()) is None

    def test_empty_row_is_denied(self):
        """Nothing known about a row means "not allowed", never "allowed"."""
        assert row_is_publishable({}) is False
        assert block_reason({}) is not None

    def test_missing_qa_row_is_denied(self):
        """The original defect: a LEFT JOIN with no QA row yields NULLs, and
        `if task["qa_needs_review"]` read NULL as falsy, i.e. "no review needed"."""
        row = _approved_row(qa_id=None, qa_passed=None, qa_needs_review=None)
        assert row_is_publishable(row) is False
        assert "no QA result" in block_reason(row)

    def test_null_needs_review_counts_as_needing_review(self):
        row = _approved_row(qa_needs_review=None)
        assert row_is_publishable(row) is False

    def test_failing_qa_is_denied(self):
        row = _approved_row(qa_passed=0)
        assert row_is_publishable(row) is False
        assert block_reason(row) == "automatic QA did not pass"

    def test_unfinished_qa_is_denied(self):
        for state in ("pending", "running"):
            row = _approved_row(qa_state=state)
            assert row_is_publishable(row) is False
            assert "not finished" in block_reason(row)

    def test_errored_qa_is_denied(self):
        row = _approved_row(qa_state="error")
        assert row_is_publishable(row) is False
        assert "no verdict" in block_reason(row)

    def test_unsuccessful_task_is_denied(self):
        assert row_is_publishable(_approved_row(status=FAILED)) is False

    def test_rejected_is_reported_as_rejected(self):
        row = _approved_row(review_state="rejected", qa_needs_review=1)
        assert row_is_publishable(row) is False
        assert block_reason(row) == "rejected by human review"

    @pytest.mark.parametrize("state", sorted(set(REVIEW_STATES) - PUBLISHABLE_REVIEW_STATES))
    def test_non_approved_review_states_are_denied(self, state):
        assert row_is_publishable(_approved_row(review_state=state)) is False

    @pytest.mark.parametrize("state", sorted(PUBLISHABLE_REVIEW_STATES))
    def test_publishable_review_states_pass(self, state):
        assert row_is_publishable(_approved_row(review_state=state)) is True

    def test_automatic_qa_pass_alone_is_not_approval(self):
        """The rule that matters commercially: QA passing is a recommendation."""
        row = _approved_row(review_state="waiting_human_review", qa_needs_review=1)
        assert row_is_publishable(row) is False


class TestSqlMatchesPython:
    """Drive GATE_SQL and row_is_publishable with identical rows."""

    @staticmethod
    def _conn():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (task_id TEXT, status TEXT, qa_state TEXT,"
                     " review_state TEXT)")
        conn.execute("CREATE TABLE q (qa_id INT, task_id TEXT, passed INT,"
                     " needs_human_review INT)")
        return conn

    def test_every_combination_agrees(self):
        conn = self._conn()
        rows = []
        combos = itertools.product(
            (SUCCESS, FAILED),                    # status
            QA_STATES,                            # qa_state
            REVIEW_STATES,                        # review_state
            (None, 0, 1),                         # qa_passed (None = no qa row)
            (None, 0, 1),                         # qa_needs_review
        )
        for index, (status, qa_state, review_state, passed, needs) in enumerate(combos):
            task_id = f"tk{index}"
            conn.execute("INSERT INTO t VALUES (?,?,?,?)",
                         (task_id, status, qa_state, review_state))
            has_qa = passed is not None or needs is not None
            if has_qa:
                conn.execute("INSERT INTO q VALUES (?,?,?,?)",
                             (index, task_id, passed, needs))
            rows.append({
                "task_id": task_id, "status": status, "qa_state": qa_state,
                "review_state": review_state,
                "qa_id": index if has_qa else None,
                "qa_passed": passed, "qa_needs_review": needs,
            })
        assert len(rows) > 500, "combination matrix unexpectedly small"

        allowed_sql = {
            r["task_id"] for r in conn.execute(
                "SELECT t.task_id FROM t"
                " LEFT JOIN q ON q.task_id = t.task_id"
                " WHERE" + GATE_SQL
            )
        }
        allowed_py = {r["task_id"] for r in rows if row_is_publishable(r)}
        assert allowed_sql == allowed_py, (
            "SQL and Python gates disagree on: "
            f"{sorted(allowed_sql ^ allowed_py)[:10]}"
        )
        assert allowed_py, "sanity: some combination must be publishable"

    def test_block_reason_set_is_exactly_the_denied_set(self):
        rows = [
            _approved_row(),
            _approved_row(qa_passed=0),
            _approved_row(qa_id=None, qa_passed=None, qa_needs_review=None),
            _approved_row(review_state="waiting_human_review", qa_needs_review=1),
            _approved_row(status=FAILED),
        ]
        for row in rows:
            assert (block_reason(row) is None) == row_is_publishable(row)
